"""DevOpsEngine — The Master Closed-Loop State Machine & Autopilot Driver.

Exit-Code Contract (shared across CLI, CI/CD runners, MCP tools, and REST APIs):
    0  OK          — Stage advanced cleanly
    1  USAGE       — Invalid arguments / illegal state transition
    2  GATE_FAILED — Gate rejected; returned to BUILD with actionable ``fix-brief.md``
    3  WAITING     — Paused for human sign-off or canary traffic soak
    4  ESCALATED   — Convergence budget exhausted or env/oscillation blocker; escalated to human
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..skills.registry import SkillRegistry, get_skill_registry
from . import feedback as fb
from .autofix import (
    RECEPTION_PROTOCOL,
    CodingBackend,
    NeoAgentCodingBackend,
    detect_oscillation,
)
from .blueprint import (
    AcceptanceVerifier,
    BlueprintBackend,
    BlueprintPhaseGate,
    BlueprintWorkspace,
    NeoAgentBlueprintBackend,
    TaskImplementerBackend,
    TaskTDDController,
    build_compaction_snapshot,
    detect_phase,
)
from .config import DevOpsConfig
from .gates import pipeline_gate, review_gate
from .harness import (
    STAGE_BOUNDARIES,
    ContextBudgetMonitor,
    EvolutionManager,
    RepoScaffold,
    SpecPhaseGate,
    route_workflow_weight,
)
from .ledger import DevOpsLedger, IllegalTransition
from .metrics import compute_devops_metrics
from .models import (
    FailureKind,
    Finding,
    FindingCategory,
    FindingStatus,
    LoopRecord,
    Mode,
    RiskTier,
    Stage,
    StepResult,
    findings_to_mr_comments,
    findings_to_sarif,
)
from .pipeline import StepRunner, default_step_runner, execute_pipeline, render_failure_digest
from .release import (
    ReleaseHooks,
    ReleaseState,
    build_command_release_hooks,
    run_progressive_release,
    validate_release_version,
)
from .review import (
    ReviewProtocol,
    Verdict,
    build_decision_brief,
    catalog_summary,
    decide_verdict,
    offending_code_unchanged,
)
from .review.record import round_summary, write_record
from .reviewers import BugPatternReviewer, Reviewer, apply_density_cap
from .risk import classify_change_risk, missing_approvals
from .standards import LANGUAGE_STANDARDS, QualityStackBundle, QualityStackCompiler
from .vcs import count_changed_lines, diff_against

OK, USAGE, GATE_FAILED, WAITING, ESCALATED = 0, 1, 2, 3, 4


class DevOpsEngine:
    """End-to-end orchestrator from feature intake to post-release learning."""

    def __init__(
        self,
        root: Path | str,
        config: DevOpsConfig,
        *,
        ledger: DevOpsLedger | None = None,
        skill_registry: SkillRegistry | None = None,
        step_runner: StepRunner = default_step_runner,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root).resolve()
        self.cfg = config
        self.clock = clock
        self.ledger = ledger or DevOpsLedger(self.root, now=clock)
        self.skills = skill_registry or get_skill_registry()
        self.step_runner = step_runner
        self.evolution = EvolutionManager(self.root)
        self.context_monitor = ContextBudgetMonitor()

    # ── Stage 1: INTAKE & SPEC ────────────────────────────────────────────────

    def new_loop(
        self,
        title: str,
        *,
        files: list[str] | None = None,
        changed_lines: int = 0,
        diff_text: str = "",
        mode: Mode = Mode.ATTENDED,
        spec_ref: str = "",
        base: str | None = None,
        parent_id: str | None = None,
        origin: str = "engineer",
        tier: RiskTier | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> LoopRecord:
        file_list = list(files or [])
        auto_tier, reasons = classify_change_risk(
            file_list, changed_lines, self.cfg.risk, diff_text=diff_text
        )
        final_tier = tier or auto_tier
        # Monotonic safety floor: humans can raise risk, NEVER downgrade auto-detected risk
        if final_tier.rank < auto_tier.rank:
            reasons.append(
                f"Requested tier '{final_tier.value}' < auto-detected '{auto_tier.value}'; "
                f"enforcing '{auto_tier.value}'"
            )
            final_tier = auto_tier

        matched_skills: list[str] = []
        if self.cfg.skills.auto_recommend:
            matched_skills = [
                s.name
                for s in self.skills.recommend(
                    stage="intake", files=file_list, query=title, limit=6
                )
            ]

        loop = self.ledger.create(
            title,
            final_tier,
            mode,
            parent_id=parent_id,
            origin=origin,
            spec_ref=spec_ref,
            base=base or self.cfg.base_branch,
            files=file_list,
            activated_skills=matched_skills,
        )
        loop.evidence["risk_reasons"] = reasons
        loop.evidence["workflow_route"] = route_workflow_weight(
            files=file_list, changed_lines=changed_lines, tier=final_tier, title=title
        )
        loop.evidence["l1_boundary"] = {
            "role": STAGE_BOUNDARIES["intake"].role,
            "non_goals": list(STAGE_BOUNDARIES["intake"].explicit_non_goals),
        }
        if evidence:
            loop.evidence.update(evidence)
        self.ledger.save(loop)

        # Materialize task-brief.md file handoff artifact
        art_dir = self.ledger.artifact_dir(loop.id)
        brief_path = art_dir / "task-brief.md"
        brief_path.write_text(self._render_task_brief(loop), encoding="utf-8")
        return loop

    # ── Stage 1.5: Spec-to-Code Blueprint, Phase Gates & Isolated Subagent TDD ──

    def blueprint_workspace(self, loop_id: str) -> BlueprintWorkspace:
        return BlueprintWorkspace(self.ledger.artifact_dir(loop_id) / "blueprint")

    def blueprint_status(self, loop: LoopRecord) -> dict[str, Any]:
        ws = self.blueprint_workspace(loop.id)
        snap = detect_phase(ws)
        res = snap.to_dict()
        res["artifacts_dir"] = str(ws.dir)
        res["compaction_snapshot"] = build_compaction_snapshot(ws)
        plan = ws.load_plan()
        if plan is not None:
            res["tasks"] = [
                {
                    "id": t.id,
                    "title": t.title,
                    "granularity": t.granularity.value,
                    "status": t.status.value,
                    "depends_on": list(t.depends_on),
                    "covers_reqs": list(t.covers_reqs),
                    "covers_cases": list(t.covers_cases),
                    "model_tier": t.model_tier,
                    "red_session": t.evidence.red_session_id if t.evidence else "",
                    "green_session": t.evidence.green_session_id if t.evidence else "",
                }
                for t in plan.tasks
            ]
        return res

    def compile_quality_stack(
        self,
        *,
        files: list[str] | None = None,
        stage: str = "build",
        max_chars: int = 10_000,
    ) -> QualityStackBundle:
        compiler = QualityStackCompiler(skill_registry=self.skills)
        return compiler.compile(
            repo_root=self.root,
            files=list(files or []),
            stage=stage,
            max_chars=max_chars,
        )

    def blueprint(
        self,
        loop: LoopRecord,
        *,
        requirements: list[str] | None = None,
        requested_lite: bool = False,
        selected_option: str = "A",
        clarifications: dict[str, str] | None = None,
        strict_clarify: bool = False,
        auto_gate: bool = True,
        backend: BlueprintBackend | None = None,
    ) -> StepResult:
        """Synthesize `spec.md`/`lite.md`, `design.md`, `tasks.md`, and `traceability.md`."""
        synth = backend or NeoAgentBlueprintBackend()
        bundle = synth.synthesize(
            loop_id=loop.id,
            title=loop.title,
            requirements_Input=list(requirements or [loop.title]),
            files=list(loop.files),
            tier=loop.tier,
            repo_root=self.root,
            requested_lite=requested_lite,
            selected_option=selected_option,
            clarifications=clarifications,
            strict_clarify=strict_clarify,
        )
        ws = self.blueprint_workspace(loop.id)
        written = ws.save(
            spec=bundle.spec,
            design=bundle.design,
            plan=bundle.plan,
            trace=bundle.traceability,
        )
        loop.spec_ref = written.get("spec", loop.spec_ref)

        gate_summaries: dict[str, Any] = {}
        if auto_gate and not bundle.spec.blocking_questions():
            phases = ["plan"] if bundle.spec.track == "lite" else ["spec", "design", "plan"]
            for ph in phases:
                g_res = self.run_phase_gate(loop, ph)
                gate_summaries[ph] = g_res.data.get("gate")
                if g_res.code != OK:
                    break

        bp_snap = self.blueprint_status(loop)
        loop.evidence["blueprint"] = {
            "track": bundle.spec.track,
            "qualification": bundle.qualification.to_dict(),
            "synthesized_by": bundle.synthesized_by,
            "artifacts": written,
            "phase": bp_snap["phase"],
            "total_tasks": bp_snap["total_tasks"],
        }
        self.ledger.log_event(
            loop,
            "blueprint",
            track=bundle.spec.track,
            phase=bp_snap["phase"],
            tasks=bp_snap["total_tasks"],
            synthesized_by=bundle.synthesized_by,
        )
        self.ledger.save(loop)

        if bundle.spec.blocking_questions():
            return StepResult(
                WAITING,
                loop.stage.value,
                f"Blueprint paused on {len(bundle.spec.blocking_questions())} blocking Open Question(s)",
                {
                    "blueprint": loop.evidence["blueprint"],
                    "open_questions": [q.to_dict() for q in bundle.spec.blocking_questions()],
                    "status": bp_snap,
                },
            )
        return StepResult(
            OK,
            loop.stage.value,
            f"Blueprint synthesized ({bundle.spec.track} track, {len(bundle.plan.tasks)} TDD task(s), phase={bp_snap['phase']})",
            {
                "blueprint": loop.evidence["blueprint"],
                "gates": gate_summaries,
                "status": bp_snap,
            },
        )

    def run_phase_gate(
        self,
        loop: LoopRecord,
        phase: str,
        *,
        force_by: str = "",
        diff_text: str = "",
    ) -> StepResult:
        """Run independent read-only Phase Gate (`spec` | `design` | `plan` | `sdc`)."""
        norm = phase.strip().lower()
        if norm == "req":
            norm = "spec"
        if norm not in ("spec", "design", "plan", "sdc"):
            return StepResult(USAGE, loop.stage.value, f"Unknown blueprint gate '{phase}'")

        ws = self.blueprint_workspace(loop.id)
        spec = ws.load_spec()
        design = ws.load_design()
        plan = ws.load_plan()
        trace = ws.load_traceability()
        if spec is None or plan is None:
            return StepResult(USAGE, loop.stage.value, "Initialize Blueprint first (`neo-devops blueprint <loop>`)")

        round_no = plan.gate_rounds(norm) + 1
        sdc_findings: list[Finding] = []
        if norm == "spec":
            rec = BlueprintPhaseGate.verify_spec(spec, round_no=round_no)
        elif norm == "design":
            if design is None:
                return StepResult(USAGE, loop.stage.value, "No `design.md` present (Lite track skips Design Gate)")
            rec = BlueprintPhaseGate.verify_design(spec, design, round_no=round_no)
        elif norm == "plan":
            rec = BlueprintPhaseGate.verify_plan(spec, design, plan, round_no=round_no)
        else:
            active_diff = diff_text or str(loop.evidence.get("latest_diff", "")) or diff_against(self.root, loop.base)
            rec, sdc_findings = BlueprintPhaseGate.verify_sdc(
                spec, design, plan, trace, diff_text=active_diff, repo_root=self.root, round_no=round_no
            )

        if force_by.strip() and not rec.passed:
            rec.passed = True
            rec.forced_by = force_by.strip()

        plan.gate_records.append(rec)
        if norm == "sdc" and rec.passed:
            plan.review_passed = True
        ws.save(plan=plan)

        snap = self.blueprint_status(loop)
        if isinstance(loop.evidence.get("blueprint"), dict):
            loop.evidence["blueprint"]["phase"] = snap["phase"]
        self.ledger.log_event(
            loop, "phase_gate", gate=norm, round=round_no, passed=rec.passed, forced_by=rec.forced_by
        )
        self.ledger.save(loop)

        payload: dict[str, Any] = {
            "gate": rec.to_dict(),
            "status": snap,
            "sdc_findings": [f.to_dict() for f in sdc_findings],
        }
        if rec.passed:
            return StepResult(OK, loop.stage.value, f"{norm.upper()} Phase Gate PASSED (round {round_no})", payload)
        if round_no >= 3:
            self.ledger.escalate(loop, f"{norm.upper()} Phase Gate failed {round_no} rounds; human ruling required")
            return StepResult(ESCALATED, loop.stage.value, f"{norm.upper()} Phase Gate failed 3 rounds; escalated", payload)
        return StepResult(
            GATE_FAILED,
            loop.stage.value,
            f"{norm.upper()} Phase Gate FAILED ({len(rec.failed_items())} check(s) failed)",
            payload,
        )

    def execute_blueprint_tasks(
        self,
        loop: LoopRecord,
        *,
        implementer: TaskImplementerBackend | None = None,
        max_tasks: int | None = None,
    ) -> StepResult:
        """Execute ready Blueprint tasks via isolated Context Packs + Red/Green TDD."""
        ws = self.blueprint_workspace(loop.id)
        if not ws.exists():
            return StepResult(USAGE, loop.stage.value, "No Blueprint initialized for this loop")

        if loop.stage == Stage.INTAKE:
            self.ledger.transition(loop, Stage.BUILD, "Entered BUILD stage via Blueprint TaskTDDController")

        controller = TaskTDDController(
            implementer=implementer,
            quality_compiler=QualityStackCompiler(skill_registry=self.skills),
        )
        run_sum = controller.run(workspace=ws, repo_root=self.root, max_tasks=max_tasks)
        # Record isolated subagent session IDs on the loop for Generator-Evaluator attestation
        plan = ws.load_plan()
        if plan is not None:
            for t in plan.tasks:
                if t.evidence:
                    for sid in (t.evidence.red_session_id, t.evidence.green_session_id):
                        if sid and sid not in loop.evidence.setdefault("author_sessions", []):
                            loop.evidence["author_sessions"].append(sid)

        snap = self.blueprint_status(loop)
        if isinstance(loop.evidence.get("blueprint"), dict):
            loop.evidence["blueprint"]["phase"] = snap["phase"]
            loop.evidence["blueprint"]["passing_tasks"] = snap["passing_tasks"]
        self.ledger.log_event(
            loop,
            "blueprint_tdd",
            executed=run_sum.executed_tasks,
            passing=run_sum.passing_tasks,
            blocked=run_sum.blocked_task,
        )
        self.ledger.save(loop)

        payload = {"tdd_run": run_sum.to_dict(), "status": snap}
        if run_sum.blocked_task:
            return StepResult(
                ESCALATED,
                loop.stage.value,
                f"Task `{run_sum.blocked_task}` halted: {run_sum.blocker_reason}",
                payload,
            )
        return StepResult(
            OK,
            loop.stage.value,
            f"Executed {len(run_sum.passing_tasks)} task(s); Blueprint phase is now `{snap['phase']}`",
            payload,
        )

    def run_acceptance(
        self,
        loop: LoopRecord,
        *,
        acceptance_cmd: str = "",
        residual_minors: list[str] | None = None,
    ) -> StepResult:
        """Generate black-box `AT-*` cases from `spec.md`, evaluate Go/No-Go, and archive clean specs."""
        ws = self.blueprint_workspace(loop.id)
        if not ws.exists():
            return StepResult(USAGE, loop.stage.value, "No Blueprint initialized for this loop")
        report = AcceptanceVerifier.run_and_archive(
            workspace=ws,
            repo_root=self.root,
            acceptance_cmd=acceptance_cmd,
            residual_minors=residual_minors,
        )
        snap = self.blueprint_status(loop)
        if isinstance(loop.evidence.get("blueprint"), dict):
            loop.evidence["blueprint"]["phase"] = snap["phase"]
            loop.evidence["blueprint"]["acceptance_verdict"] = report.verdict
            loop.evidence["blueprint"]["archived_to"] = report.archived_to
        self.ledger.log_event(
            loop,
            "acceptance",
            verdict=report.verdict,
            coverage=report.requirement_coverage,
            archived_to=report.archived_to,
        )
        self.ledger.save(loop)

        payload = {"acceptance": report.to_dict(), "status": snap}
        if report.verdict == "NO_GO":
            return StepResult(GATE_FAILED, loop.stage.value, "Acceptance verdict NO_GO", payload)
        return StepResult(
            OK,
            loop.stage.value,
            f"Acceptance {report.verdict} ({report.requirement_coverage:.0%} req coverage); archived to `{report.archived_to}`",
            payload,
        )

    # ── Stage 2: BUILD (TDD Coding) ───────────────────────────────────────────

    def start_build(
        self,
        loop: LoopRecord,
        *,
        coder: CodingBackend | None = None,
    ) -> StepResult:
        build_skills = [
            s.name
            for s in self.skills.recommend(
                stage="build", files=loop.files, query=loop.title, limit=6
            )
        ]
        for sk in build_skills:
            if sk not in loop.activated_skills:
                loop.activated_skills.append(sk)

        self.ledger.transition(loop, Stage.BUILD, "Entered BUILD stage (TDD RED->GREEN->REFACTOR)")
        payload: dict[str, Any] = {
            "tier": loop.tier.value,
            "activated_skills": loop.activated_skills,
        }

        if coder is not None:
            brief_path = self.ledger.artifact_dir(loop.id) / "task-brief.md"
            if not brief_path.is_file():
                brief_path.write_text(self._render_task_brief(loop), encoding="utf-8")
            bundle = self.skills.build_context_bundle(build_skills)
            outcome = coder.implement_feature(loop, brief_path, self.root, skill_context=bundle)
            loop.total_cost_usd += outcome.cost_usd
            self._note_author_session(loop, outcome)
            loop.evidence["last_coding"] = {
                "summary": outcome.summary,
                "modified_files": outcome.modified_files,
                "success": outcome.success,
            }
            if outcome.diff_override is not None:
                loop.evidence["latest_diff"] = outcome.diff_override
            self.ledger.save(loop)
            payload["coding"] = loop.evidence["last_coding"]
            if not outcome.success:
                self.ledger.escalate(loop, f"AI coding failed: {outcome.error or outcome.summary}")
                return StepResult(ESCALATED, loop.stage.value, f"Coding failed: {outcome.error}", payload)

        return StepResult(
            OK,
            loop.stage.value,
            "Entered BUILD: write failing tests first (RED), then minimal clean implementation (GREEN)",
            payload,
        )

    # ── Stage 3: REVIEW & Engine-Verified Convergence ─────────────────────────

    def review(
        self,
        loop: LoopRecord,
        diff_text: str,
        reviewers: list[Reviewer],
    ) -> StepResult:
        if loop.stage == Stage.BUILD:
            self.ledger.transition(loop, Stage.REVIEW, "Submitted diff for independent review")
        if loop.stage != Stage.REVIEW:
            raise IllegalTransition(
                f"{loop.id}: cannot review from stage '{loop.stage.value}' (expected build/review)"
            )

        # A review *round* is one execution of the review, so records stay unique even when a
        # pipeline repair sends the loop back through REVIEW without a review-budget increment.
        round_no = len(loop.evidence.get("review_protocol", [])) + 1
        changed_ln = count_changed_lines(diff_text)

        # Governed Review Protocol: collect -> ground -> attribute -> attest (see devops/review).
        protocol = ReviewProtocol(self.cfg.review, self.root)
        outcome = protocol.run(loop, diff_text, reviewers, round_no)
        capped_fresh, suppressed = apply_density_cap(
            outcome.findings, changed_ln, self.cfg.budget.minor_density_lines
        )
        # Suppression is a reporting choice, not evidence: a finding hidden by the density cap
        # must keep its state rather than be read as "fixed".
        vanished_kept = self._merge_findings_on_rereview(
            loop,
            capped_fresh,
            round_no,
            diff_text=diff_text,
            verify_vanished=self.cfg.review.verify_vanished,
            present_fps={f.fingerprint() for f in outcome.findings},
        )

        # Persist SARIF 2.1.0 & inline MR comment artifacts for IDE / GitLab / GitHub integration
        art_dir = self.ledger.artifact_dir(loop.id)
        sarif = findings_to_sarif(loop.findings)
        sarif_path = art_dir / "findings.sarif.json"
        sarif_path.write_text(json.dumps(sarif, ensure_ascii=False, indent=2), encoding="utf-8")

        mr_comments = findings_to_mr_comments(loop.findings)
        mr_path = art_dir / "mr-comments.json"
        mr_path.write_text(json.dumps(mr_comments, ensure_ascii=False, indent=2), encoding="utf-8")

        # Harness L7 self-evolution: record recurring defect classes into repo checklist
        evolved: list[str] = []
        if self.cfg.skills.auto_evolve_checklist and round_no >= 2 and loop.open_findings():
            evolved = BugPatternReviewer(self.cfg.skills.checklist_path).evolve_checklist(
                self.root, loop.open_findings()
            )

        gate = review_gate(loop, self.cfg.budget)
        verdict = decide_verdict(loop, gate.passed)
        summary = round_summary(
            loop,
            round_no,
            outcome,
            gate,
            verdict,
            density_suppressed=suppressed,
            vanished_unverified=vanished_kept,
            changed_lines=changed_ln,
        )
        loop.evidence.setdefault("review_protocol", []).append(
            {k: v for k, v in summary.items() if k not in ("attestation", "grounding")}
        )
        loop.evidence.setdefault("review_attestations", []).append(summary["attestation"])
        self.ledger.log_event(
            loop,
            "review",
            round=round_no,
            passed=gate.passed,
            star_rating=gate.star_rating,
            open_findings=len(loop.open_findings()),
            density_suppressed=suppressed,
            reviewers=[r.name for r in reviewers],
            verdict=verdict.value,
            introduced=summary["introduced"],
            carried_over=summary["carried_over"],
            fixed_by_change=summary["fixed_by_change"],
            isolated=summary["attestation"].get("isolated"),
        )

        data: dict[str, Any] = {
            "gate": gate.to_dict(),
            "open_findings": [f.to_dict() for f in loop.open_findings()],
            "review_rounds": loop.review_rounds,
            "sarif_path": str(sarif_path),
            "mr_comments_path": str(mr_path),
            "density_suppressed": suppressed,
            "evolved_patterns": evolved,
            "verdict": verdict.value,
            "attribution": {
                k: summary[k] for k in ("introduced", "carried_over", "fixed_by_change", "debt")
            },
            "attestation": summary["attestation"],
        }
        if self.cfg.review.write_record:
            data["review_record"] = write_record(art_dir, loop, summary, outcome, gate)

        if gate.passed:
            self.ledger.transition(
                loop, Stage.PIPELINE, f"Review gate passed ({gate.star_rating}★)"
            )
            return StepResult(OK, loop.stage.value, "Review passed; advancing to PIPELINE", data)

        if gate.awaiting_approval:
            self.ledger.save(loop)
            return StepResult(
                WAITING,
                loop.stage.value,
                f"High-risk change passed automated review; awaiting human sign-off "
                f"(neo-devops approve {loop.id} --point review --by <engineer>)",
                data,
            )

        loop.review_rounds += 1
        data["review_rounds"] = loop.review_rounds
        brief_path = self._write_review_fix_brief(loop)
        data["fix_brief"] = str(brief_path)

        # Route by who can decide: a business fact or an upstream (spec/design) defect is not
        # something an AI fixer should guess at — escalate with the questions, not more patches.
        if verdict in (Verdict.NEEDS_HUMAN, Verdict.REDESIGN):
            brief = build_decision_brief(loop, verdict, loop.review_rounds)
            decision_path = art_dir / "decision-brief.md"
            decision_path.write_text(brief, encoding="utf-8")
            data["decision_brief"] = decision_path.name
            self.ledger.escalate(
                loop,
                f"Review verdict {verdict.value.upper()}: blocking findings need an owner's decision "
                f"(see decision-brief.md) — automated repair halted",
            )
            return StepResult(
                ESCALATED,
                loop.stage.value,
                f"Review verdict {verdict.value}; decision brief written, escalated to owner",
                data,
            )

        oscillating = detect_oscillation(loop)
        if oscillating:
            self.ledger.escalate(
                loop,
                f"Fix oscillation detected on finding(s) {oscillating} — escalating to human",
            )
            return StepResult(
                ESCALATED, loop.stage.value, f"Oscillating findings {oscillating}; escalated", data
            )

        if loop.review_rounds >= self.cfg.budget.max_review_rounds:
            self.evolution.record_hashimoto_guardrail(
                loop.id,
                f"3-strike review budget exhausted on {[f.rule for f in loop.open_findings()[:3]]}",
                trigger="three_strike_error",
            )
            self.ledger.escalate(
                loop,
                f"Review-fix budget ({self.cfg.budget.max_review_rounds} rounds) exhausted with "
                f"{len(loop.open_findings())} unresolved finding(s)",
            )
            return StepResult(
                ESCALATED,
                loop.stage.value,
                "Review-fix budget exhausted; escalated to human engineer",
                data,
            )

        self.ledger.transition(
            loop,
            Stage.BUILD,
            f"Review rejected (round {loop.review_rounds}): {len(loop.open_findings())} open finding(s)",
        )
        return StepResult(
            GATE_FAILED,
            loop.stage.value,
            f"Review gate failed (round {loop.review_rounds}/{self.cfg.budget.max_review_rounds}); "
            "repair via fix-brief.md",
            data,
        )

    def _collect_findings(self, reviewers: list[Reviewer], diff_text: str) -> list[Finding]:
        merged: dict[str, Finding] = {}
        for rev in reviewers:
            for f in rev.review(diff_text, repo_root=self.root):
                fp = f.fingerprint()
                existing = merged.get(fp)
                if existing is None or f.severity.rank > existing.severity.rank:
                    merged[fp] = f
        return list(merged.values())

    @staticmethod
    def _merge_findings_on_rereview(
        loop: LoopRecord,
        fresh: list[Finding],
        round_no: int,
        *,
        diff_text: str = "",
        verify_vanished: bool = True,
        present_fps: set[str] | None = None,
    ) -> int:
        """Engine-verified resolution: only absence on re-review marks an OPEN finding FIXED.

        A *generative* finding that merely was not repeated is not proof of a fix — reviewer
        variance would otherwise read as progress. It stays open while its quoted code is still in
        the diff. ``present_fps`` lists every finding the reviewers raised this round, including
        those hidden by the density cap; those keep their state untouched. Returns how many
        findings were kept open for the generative-variance reason.
        """
        fresh_by_fp = {f.fingerprint(): f for f in fresh}
        existing_by_fp = {f.fingerprint(): f for f in loop.findings}
        present = present_fps if present_fps is not None else set(fresh_by_fp)
        kept_open = 0

        for fp, old in existing_by_fp.items():
            if fp not in fresh_by_fp and fp in present:
                continue  # raised this round but suppressed from the report: no evidence either way
            if fp in fresh_by_fp:
                new = fresh_by_fp[fp]
                if old.status == FindingStatus.FIXED:
                    old.status = FindingStatus.OPEN
                    old.reopened_count += 1
                    old.resolution = f"Re-opened in round {round_no} (regression)"
                old.line = new.line or old.line
                old.severity = new.severity
                old.attribution = new.attribution or old.attribution
                old.evidence = new.evidence or old.evidence
                old.standard = new.standard or old.standard
            elif old.status == FindingStatus.OPEN:
                if verify_vanished and diff_text and offending_code_unchanged(old, diff_text):
                    kept_open += 1
                    if not old.resolution.startswith("DISPUTED"):
                        old.resolution = (
                            f"Not re-reported in round {round_no}, but the quoted code is unchanged — "
                            "kept open (reviewer variance is not a fix)"
                        )
                    continue
                old.status = FindingStatus.FIXED
                old.resolution = f"Verified absent in round {round_no} by independent reviewer"

        for fp, f in fresh_by_fp.items():
            if fp not in existing_by_fp:
                f.first_seen_round = round_no
                loop.findings.append(f)
        return kept_open

    # ── Stage 4: PIPELINE (Build, Test, Coverage, 3-Way Failure Triage) ──────

    def run_pipeline(self, loop: LoopRecord) -> StepResult:
        if loop.stage != Stage.PIPELINE:
            raise IllegalTransition(
                f"{loop.id}: pipeline can only run in 'pipeline' stage (current: '{loop.stage.value}')"
            )

        art_dir = self.ledger.artifact_dir(loop.id)
        report = execute_pipeline(self.cfg.pipeline, self.root, art_dir, self.step_runner)
        gate = pipeline_gate(report, self.cfg.pipeline)
        loop.evidence["pipeline"] = report
        data: dict[str, Any] = {"gate": gate.to_dict(), "report": report}
        self.ledger.log_event(
            loop,
            "pipeline",
            passed=gate.passed,
            coverage=report.get("coverage"),
            failure_kind=report.get("failure_kind"),
        )

        if gate.passed:
            self.ledger.transition(loop, Stage.RELEASE, "CI pipeline gate passed")
            return StepResult(OK, loop.stage.value, "Pipeline passed; ready for RELEASE", data)

        # Environment blockers (e.g. unreachable daemon / missing binary) escalate immediately
        # instead of letting an AI fixer corrupt source code!
        if report.get("failure_kind") == FailureKind.ENV_BLOCKER.value:
            digest = render_failure_digest(report)
            self.ledger.escalate(
                loop,
                f"Environment blocker in CI pipeline — halting auto-fix: {'; '.join(gate.reasons)[:160]}",
            )
            data["environment_blocker"] = digest
            return StepResult(
                ESCALATED,
                loop.stage.value,
                "Environment blocker detected in pipeline; escalated without touching source code",
                data,
            )

        loop.pipeline_fixes += 1
        digest = render_failure_digest(report) or "\n".join(gate.reasons)
        brief_path = art_dir / "fix-brief.md"
        brief_path.write_text(
            f"# CI Pipeline Failure Repair Brief ({loop.id}, attempt {loop.pipeline_fixes})\n\n"
            + "\n".join(f"- {r}" for r in gate.reasons)
            + f"\n\n{digest}\n\n"
            "> STRICT ANTI-CHEAT RED LINE: Fix the implementation bug. It is CRITICAL-forbidden "
            "to skip tests, delete assertions, or lower coverage thresholds. All fixes will be "
            "re-inspected by the Review gate.\n",
            encoding="utf-8",
        )
        data["fix_brief"] = str(brief_path)

        if loop.pipeline_fixes >= self.cfg.budget.max_pipeline_fixes:
            self.ledger.escalate(
                loop,
                f"Pipeline repair budget ({self.cfg.budget.max_pipeline_fixes} attempts) exhausted",
            )
            return StepResult(
                ESCALATED, loop.stage.value, "Pipeline repair budget exhausted; escalated", data
            )

        self.ledger.transition(
            loop,
            Stage.BUILD,
            f"Pipeline failed (attempt {loop.pipeline_fixes}): {'; '.join(gate.reasons)[:180]}",
        )
        return StepResult(
            GATE_FAILED,
            loop.stage.value,
            "Pipeline failed; returned to BUILD with fix-brief.md (must re-pass REVIEW after fix)",
            data,
        )

    # ── Stage 5: RELEASE (Canary -> Health Gate -> Rollback / Promote) ───────

    def release(
        self,
        loop: LoopRecord,
        version: str,
        *,
        dry_run: bool = False,
        hooks: ReleaseHooks | None = None,
    ) -> StepResult:
        if loop.stage != Stage.RELEASE:
            raise IllegalTransition(
                f"{loop.id}: release requires 'release' stage (current: '{loop.stage.value}')"
            )
        validate_release_version(version)

        miss = missing_approvals(loop, {"release"})
        if miss:
            return StepResult(
                WAITING,
                loop.stage.value,
                f"Awaiting human release approval (neo-devops approve {loop.id} --point release --by <engineer>)",
                {"awaiting_approval": miss},
            )

        prev_rel = loop.evidence.get("release")
        state = (
            ReleaseState.from_dict(prev_rel)
            if isinstance(prev_rel, dict) and prev_rel.get("version") == version
            else ReleaseState(version)
        )
        actual_hooks = hooks or build_command_release_hooks(
            self.cfg.release, self.root, dry_run=dry_run
        )
        full_approved = not missing_approvals(loop, {"full_rollout"})
        state = run_progressive_release(
            self.cfg.release, state, actual_hooks, full_rollout_approved=full_approved
        )
        loop.evidence["release"] = state.to_dict()
        data = {"release": state.to_dict()}
        self.ledger.log_event(
            loop, "release", version=version, status=state.status, dry_run=dry_run
        )

        if state.status == "promoted":
            self.ledger.transition(
                loop, Stage.OBSERVE, f"{version} promoted to 100%; entering observation window"
            )
            return StepResult(OK, loop.stage.value, f"{version} promoted to 100% (OBSERVE)", data)

        if state.status == "rolled_back":
            self.evolution.record_hashimoto_guardrail(
                loop.id,
                f"Canary release {version} rolled back: {state.reason[:140]}",
                trigger="canary_rollback",
            )
            self.ledger.transition(
                loop, Stage.ROLLED_BACK, f"{version} automatically rolled back: {state.reason[:180]}"
            )
            return StepResult(
                GATE_FAILED,
                loop.stage.value,
                f"Canary unhealthy — automatically rolled back {version}: {state.reason}",
                data,
            )

        self.ledger.save(loop)
        return StepResult(WAITING, loop.stage.value, state.reason, data)

    # ── Stage 6 & 7: OBSERVE -> LEARN (Feedback Triage & Next-Loop Spawn) ────

    def ingest_feedback(
        self, loop: LoopRecord, items: list[fb.FeedbackItem]
    ) -> StepResult:
        if loop.stage not in (Stage.OBSERVE, Stage.ROLLED_BACK):
            raise IllegalTransition(
                f"{loop.id}: feedback triage requires observe/rolled_back stage (current: '{loop.stage.value}')"
            )

        rel_info = loop.evidence.get("release", {})
        version = str(rel_info.get("version", ""))
        released_at = next(
            (float(h["at"]) for h in loop.history if h.get("to") == Stage.OBSERVE.value),
            None,
        )

        all_items = list(items)
        if loop.stage == Stage.ROLLED_BACK:
            all_items.append(
                fb.make_rollback_feedback_item(
                    version or "unknown",
                    str(rel_info.get("reason", "canary SLO violation")),
                    self.clock(),
                )
            )

        clusters = fb.triage_feedback(
            all_items,
            self.cfg.feedback,
            release_ts=released_at,
            released_version=version,
        )
        self.ledger.transition(
            loop,
            Stage.LEARN,
            f"Triaged {len(all_items)} feedback signal(s) into {len(clusters)} theme(s)",
        )

        art_dir = self.ledger.artifact_dir(loop.id)
        report_md = fb.render_triage_report(loop.id, loop.title, clusters)
        report_path = art_dir / "next-iteration.md"
        report_path.write_text(report_md, encoding="utf-8")

        spawned_ids: list[str] = []
        iter_count = 0
        for c in clusters:
            if c.decision == "HOTFIX":
                child = self._spawn_child_loop(loop, c, kind="hotfix")
            elif (
                c.decision == "NEXT_ITERATION"
                and iter_count < self.cfg.feedback.max_spawned_iterations
            ):
                iter_count += 1
                child = self._spawn_child_loop(loop, c, kind="iteration")
            else:
                continue
            spawned_ids.append(child.id)

        loop.evidence["feedback"] = {
            "items_count": len(all_items),
            "clusters": [c.to_dict() for c in clusters],
            "spawned_loops": spawned_ids,
            "hotfix_count": sum(1 for c in clusters if c.decision == "HOTFIX"),
            "report_path": str(report_path),
        }
        self.ledger.transition(
            loop, Stage.DONE, f"Closed loop complete; spawned {len(spawned_ids)} follow-up loop(s)"
        )
        return StepResult(
            OK,
            loop.stage.value,
            f"Feedback triaged -> spawned {len(spawned_ids)} child loop(s): {', '.join(spawned_ids) or 'none'}",
            {
                "clusters": [c.to_dict() for c in clusters],
                "spawned_loops": spawned_ids,
                "report_path": str(report_path),
            },
        )

    def _spawn_child_loop(
        self, parent: LoopRecord, cluster: fb.FeedbackCluster, kind: str
    ) -> LoopRecord:
        prefix = "[hotfix] " if kind == "hotfix" else ""
        origin = "rollback" if any(i.id == "canary-rollback" for i in cluster.items) else "feedback"
        child_tier = (
            parent.tier
            if kind == "iteration"
            else (RiskTier.HIGH if parent.tier == RiskTier.HIGH else RiskTier.MEDIUM)
        )
        child = self.new_loop(
            f"{prefix}{cluster.title}",
            files=list(parent.files),
            mode=Mode.ATTENDED,
            parent_id=parent.id,
            origin=origin,
            tier=child_tier,
            evidence={
                "kind": kind,
                "cluster": cluster.to_dict(),
                "samples": [i.text[:160] for i in cluster.items[:3]],
            },
        )
        # Auto-generate SDD draft requirement spec for the child loop
        srs_path = self.ledger.artifact_dir(child.id) / "srs-draft.md"
        srs_path.write_text(fb.render_srs_draft(child.id, parent.id, cluster), encoding="utf-8")
        child.spec_ref = str(srs_path)
        self.ledger.save(child)
        return child

    # ── Human-on-the-Loop Governance Actions ──────────────────────────────────

    def waive(self, loop: LoopRecord, finding_id: str, by: str, reason: str) -> LoopRecord:
        if not by.strip() or not reason.strip():
            raise ValueError("Waiving a finding requires both human name ('by') and 'reason'")
        for f in loop.findings:
            if f.id == finding_id:
                f.status = FindingStatus.WAIVED
                f.resolution = f"waived by {by}: {reason}"
                self.ledger.log_event(
                    loop, "waived", finding=finding_id, by=by, severity=f.severity.value
                )
                self.ledger.save(loop)
                return loop
        raise ValueError(f"Finding not found: {finding_id}")

    def respond(self, loop: LoopRecord, finding_id: str, by: str, reason: str) -> LoopRecord:
        """Dispute a finding with evidence instead of silently ignoring or blindly 'fixing' it.

        The finding stays OPEN (the author cannot close it) but is re-routed to a human decision,
        and the next review verdict becomes NEEDS_HUMAN with the dispute quoted in the brief.
        """
        if not by.strip() or not reason.strip():
            raise ValueError("Disputing a finding requires both 'by' and an evidence-backed 'reason'")
        for f in loop.findings:
            if f.id == finding_id:
                f.category = FindingCategory.HUMAN_DECISION
                f.resolution = f"DISPUTED by {by}: {reason[:300]}"
                self.ledger.log_event(loop, "disputed", finding=finding_id, by=by)
                self.ledger.save(loop)
                return loop
        raise ValueError(f"Finding not found: {finding_id}")

    def _apply_disputes(self, loop: LoopRecord, disputes: dict[str, str]) -> None:
        for fid, why in disputes.items():
            try:
                self.respond(loop, fid, "fixer-agent", why)
            except ValueError:
                continue  # a hallucinated finding id must not crash the loop

    @staticmethod
    def _note_author_session(loop: LoopRecord, outcome: Any) -> None:
        sid = getattr(outcome, "session_id", "")
        if sid:
            sessions = loop.evidence.setdefault("author_sessions", [])
            if sid not in sessions:
                sessions.append(sid)

    def approve(self, loop: LoopRecord, point: str, by: str, note: str = "") -> StepResult:
        if point not in ("review", "release", "full_rollout"):
            raise ValueError("Approval point must be one of: review | release | full_rollout")
        self.ledger.approve(loop, point, by, note)
        if point == "review" and loop.stage == Stage.REVIEW:
            gate = review_gate(loop, self.cfg.budget)
            if gate.passed:
                self.ledger.transition(
                    loop, Stage.PIPELINE, f"Review gate passed (approved by {by})"
                )
        return StepResult(OK, loop.stage.value, f"Recorded '{point}' approval by {by}")

    def resume(
        self, loop: LoopRecord, by: str, to: Stage | None = None, note: str = ""
    ) -> StepResult:
        self.ledger.resume(loop, by, to, note)
        return StepResult(
            OK,
            loop.stage.value,
            f"Resumed by {by} -> stage '{loop.stage.value}' (convergence budgets reset)",
        )

    def metrics_snapshot(self) -> dict[str, Any]:
        valid, _, _ = self.ledger.verify_audit_chain()
        return compute_devops_metrics(self.ledger.list_all(), chain_verified=valid)

    def _measure_isolation(self) -> dict[str, Any]:
        """Aggregate independence attestations across loops: measured, never assumed."""
        rounds, violations = 0, []
        for lp in self.ledger.list_all():
            for att in lp.evidence.get("review_attestations", []):
                rounds += 1
                if not att.get("isolated", False):
                    violations.append(
                        {
                            "loop": lp.id,
                            "round": att.get("round"),
                            "shared_sessions": att.get("shared_sessions", []),
                            "forbidden_inputs": att.get("forbidden_inputs", []),
                        }
                    )
        isolated: bool | None = None if rounds == 0 else not violations
        return {"isolated": isolated, "rounds": rounds, "violations": violations[:20]}

    def harness_status(self) -> dict[str, Any]:
        """Return comprehensive 7-Layer Harness L1-L7 & DLP Privacy health snapshot."""
        from .privacy import scan_repository_privacy

        repo_diag = RepoScaffold.diagnose(self.root)
        valid_chain, event_count, chain_msg = self.ledger.verify_audit_chain()
        privacy_violations = scan_repository_privacy(self.root, self.cfg.privacy)
        evo = self.evolution.list_proposals()
        skill_list = self.skills.list_all()
        f1_f4_ok = sum(1 for s in skill_list if s.validate_harness().passed)

        isolation = self._measure_isolation()

        spec_checks: list[dict[str, Any]] = []
        for cand_name, ph in (("req.md", "req"), ("srs.md", "req"), ("design.md", "design")):
            cand = self.root / "docs" / cand_name
            if cand.is_file():
                rep = SpecPhaseGate.verify_spec_file(cand, ph)
                spec_checks.append({"file": cand_name, "passed": rep.passed, "missing": rep.missing_sections})

        return {
            "layers": {
                "L1_information_boundary": {
                    "status": "aligned" if repo_diag.mode == "aligned" else "actionable",
                    "repo_scaffold": repo_diag.to_dict(),
                    "defined_stage_boundaries": list(STAGE_BOUNDARIES.keys()),
                },
                "L2_tool_distillation": {
                    "status": "active",
                    "features": ["timeout_retry_guard", "carlini_grep_log", "stdout_distiller"],
                },
                "L3_orchestration_and_subagents": {
                    "status": "active",
                    "mode": "stripe_hybrid_state_machine",
                    "subagent_gate": "3_condition_strict",
                },
                "L4_memory_and_context_40pct": {
                    "status": "active",
                    "smart_ceiling": self.context_monitor.smart_ceiling,
                    "hard_reset_ceiling": self.context_monitor.hard_reset_ceiling,
                    "reasoning_folding": True,
                },
                "L5_independent_evaluation": {
                    "status": "active" if isolation["isolated"] is not False else "violation",
                    # True / False are *measured* from per-round attestations; None = no review yet.
                    "generator_evaluator_isolated": isolation["isolated"],
                    "attested_review_rounds": isolation["rounds"],
                    "isolation_violations": isolation["violations"],
                    "review_protocol": {
                        "attribution": self.cfg.review.attribution,
                        "carried_over_policy": self.cfg.review.carried_over_policy,
                        "ground_llm_findings": self.cfg.review.ground_llm_findings,
                        "verify_vanished": self.cfg.review.verify_vanished,
                        "clean_code": self.cfg.review.clean_code,
                        "sdc_compliance": self.cfg.review.sdc_compliance,
                        "rule_catalog": catalog_summary(),
                    },
                    "quality_stack": {
                        "layers": ["5d-clean-code-floor", "7-rung-minimal-code-ladder", "language-guardrails"],
                        "supported_languages": sorted(LANGUAGE_STANDARDS.keys()),
                    },
                    "spec_checks": spec_checks,
                },
                "L6_mechanical_constraints_and_dlp": {
                    "status": "healthy" if (valid_chain and not privacy_violations) else "violation",
                    "audit_chain_valid": valid_chain,
                    "audit_events_verified": event_count,
                    "audit_message": chain_msg,
                    "privacy_violations_count": len(privacy_violations),
                    "privacy_violations": [v.to_dict() for v in privacy_violations[:20]],
                    "skills_f1_f4_compliant": f"{f1_f4_ok}/{len(skill_list)}",
                },
                "L7_self_evolution_safe": {
                    "status": "active",
                    "pending_proposals": len(evo["pending"]),
                    "applied_proposals": len(evo["applied"]),
                    "proposals": evo,
                },
            }
        }

    # ── Autopilot: One-Command End-to-End Closed-Loop Driver ─────────────────

    def autopilot(
        self,
        loop: LoopRecord,
        *,
        reviewers: list[Reviewer],
        coder: CodingBackend | None = None,
        diff_supplier: Callable[[LoopRecord], str] | None = None,
        version: str = "v0.1.0",
        dry_run_release: bool = True,
        release_hooks: ReleaseHooks | None = None,
        feedback_items: list[fb.FeedbackItem] | None = None,
    ) -> StepResult:
        """Drive a Loop autonomously from its current stage as far as risk policy allows.

        Automatically executes the Review <-> Auto-Fix and Pipeline <-> Auto-Fix
        self-healing loops within configured convergence budgets!
        """
        active_coder = coder or NeoAgentCodingBackend()

        def _current_diff() -> str:
            if diff_supplier is not None:
                return diff_supplier(loop)
            if "latest_diff" in loop.evidence:
                return str(loop.evidence["latest_diff"])
            return diff_against(self.root, loop.base)

        if loop.stage == Stage.INTAKE:
            res = self.start_build(loop, coder=coder)
            if res.code != OK:
                return res

        # Bounded self-healing loop across BUILD -> REVIEW -> PIPELINE
        max_outer_cycles = (
            self.cfg.budget.max_review_rounds + self.cfg.budget.max_pipeline_fixes + 2
        )
        for _ in range(max_outer_cycles):
            if loop.stage == Stage.BUILD:
                rev_res = self.review(loop, _current_diff(), reviewers)
                if rev_res.code in (WAITING, ESCALATED):
                    return rev_res
                if rev_res.code == GATE_FAILED:
                    fix_brief = Path(rev_res.data["fix_brief"])
                    fix_skills = [
                        s.name
                        for s in self.skills.recommend(
                            stage="build", files=loop.files, query=loop.title, limit=4
                        )
                    ]
                    bundle = self.skills.build_context_bundle(fix_skills)
                    fix_out = active_coder.repair_from_brief(
                        loop, fix_brief, self.root, loop.review_rounds, skill_context=bundle
                    )
                    loop.total_cost_usd += fix_out.cost_usd
                    self._note_author_session(loop, fix_out)
                    self._apply_disputes(loop, fix_out.disputes)
                    if fix_out.diff_override is not None:
                        loop.evidence["latest_diff"] = fix_out.diff_override
                    self.ledger.save(loop)
                    if not fix_out.success:
                        self.ledger.escalate(
                            loop, f"Auto-fixer could not repair review findings: {fix_out.error}"
                        )
                        return StepResult(
                            ESCALATED, loop.stage.value, f"Auto-fix failed: {fix_out.error}"
                        )
                    continue

            if loop.stage == Stage.PIPELINE:
                pipe_res = self.run_pipeline(loop)
                if pipe_res.code in (WAITING, ESCALATED):
                    return pipe_res
                if pipe_res.code == GATE_FAILED:
                    fix_brief = Path(pipe_res.data["fix_brief"])
                    fix_skills = [
                        s.name
                        for s in self.skills.recommend(
                            stage="pipeline", files=loop.files, query=loop.title, limit=4
                        )
                    ]
                    bundle = self.skills.build_context_bundle(fix_skills)
                    fix_out = active_coder.repair_from_brief(
                        loop, fix_brief, self.root, loop.pipeline_fixes, skill_context=bundle
                    )
                    loop.total_cost_usd += fix_out.cost_usd
                    self._note_author_session(loop, fix_out)
                    if fix_out.diff_override is not None:
                        loop.evidence["latest_diff"] = fix_out.diff_override
                    self.ledger.save(loop)
                    if not fix_out.success:
                        self.ledger.escalate(
                            loop, f"Auto-fixer could not repair CI failure: {fix_out.error}"
                        )
                        return StepResult(
                            ESCALATED, loop.stage.value, f"Pipeline auto-fix failed: {fix_out.error}"
                        )
                    continue

            if loop.stage == Stage.RELEASE:
                break

        if loop.stage == Stage.RELEASE:
            rel_res = self.release(
                loop, version, dry_run=dry_run_release, hooks=release_hooks
            )
            if rel_res.code == WAITING:
                return rel_res
            # Both OBSERVE (promoted) and ROLLED_BACK can proceed to feedback triage if signals exist
            if feedback_items is not None or loop.stage == Stage.ROLLED_BACK:
                return self.ingest_feedback(loop, feedback_items or [])
            return rel_res

        if loop.stage in (Stage.OBSERVE, Stage.ROLLED_BACK) and feedback_items is not None:
            return self.ingest_feedback(loop, feedback_items)

        return StepResult(OK, loop.stage.value, f"Autopilot parked at stage '{loop.stage.value}'")

    # ── Internal Artifact Templates ───────────────────────────────────────────

    def _render_task_brief(self, loop: LoopRecord) -> str:
        skills_line = ", ".join(loop.activated_skills) or "none"
        files_line = ", ".join(loop.files) or "(to be determined)"
        qstack = self.compile_quality_stack(files=loop.files, stage="build", max_chars=4_500)
        return (
            f"# Task Brief — {loop.id}: {loop.title}\n\n"
            f"- **Risk Tier**: `{loop.tier.value}` | **Mode**: `{loop.mode.value}`\n"
            f"- **Base Branch**: `{loop.base}` | **Spec Ref**: `{loop.spec_ref or 'N/A'}`\n"
            f"- **Touched Files**: {files_line}\n"
            f"- **Activated Skills**: {skills_line}\n"
            f"- **Quality Stack Layers**: {', '.join(qstack.loaded_layers)}\n\n"
            f"## Mandatory Engineering Discipline\n"
            f"1. **TDD**: Write failing test(s) reproducing the requirement/bug BEFORE implementation.\n"
            f"2. **Security**: Enforce input validation, workspace confinement, and zero hardcoded secrets.\n"
            f"3. **Clean Code**: Explicit type annotations, narrow exception handling, immutable defaults.\n\n"
            f"{qstack.prompt_markdown}\n"
        )

    def _write_review_fix_brief(self, loop: LoopRecord) -> Path:
        lines = [
            f"# Review Repair Brief ({loop.id}, Round {loop.review_rounds})",
            "",
            "Repair all findings below ordered by severity. **Do NOT mark findings fixed yourself** — "
            "the independent Reviewer engine will verify their disappearance on the next diff.",
            "",
        ]
        ordered = sorted(loop.open_findings(), key=lambda x: -x.severity.rank)

        def _entry(f: Finding) -> str:
            loc = f"{f.file}:{f.line}" if f.file else "diff"
            ref = f" ({f.standard})" if f.standard else ""
            tag = f" _[{f.attribution}]_" if f.attribution else ""
            ev = f"\n  - Evidence: `{f.evidence[:120]}`" if f.evidence else ""
            sug = f"\n  - Fix advice: {f.suggestion}" if f.suggestion else ""
            return (
                f"- **[{f.severity.value.upper()}]** `{f.id}` `{f.rule}`{ref}{tag} @ `{loc}`: "
                f"{f.message}{ev}{sug}"
            )

        fixable = [f for f in ordered if f.category.auto_fixable]
        human = [f for f in ordered if not f.category.auto_fixable]
        if human:
            lines.append("## Fix these (AI-repairable)")
        lines.extend(_entry(f) for f in fixable)
        if human:
            lines += [
                "",
                "## Do NOT attempt these — they need an owner's decision",
                *(_entry(f) for f in human),
            ]
        lines.extend(
            [
                "",
                RECEPTION_PROTOCOL,
                "",
                "## Hard Anti-Cheat Guardrails",
                "- NEVER delete or weaken assertions, add skip/xfail/noqa, or lower coverage thresholds (CRITICAL block).",
                "- Scope changes strictly to the findings above; do not refactor unrelated code.",
                "- Add a regression test for every logic or security bug repaired.",
            ]
        )
        path = self.ledger.artifact_dir(loop.id) / "fix-brief.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path
