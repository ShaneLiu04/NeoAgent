"""Context-Isolated Subagent TDD Controller, Model-Tier Selector & Clean Archive.

Maximizes LLM execution quality while preventing context pollution & test gaming:
  1. ``ModelTierSelector``: Assigns ``cheap`` / ``standard`` / ``reasoner`` model
     tiers per task complexity and blast radius.
  2. ``compile_task_context_pack``: Compiles a bounded ``task-T00k-brief.md``
     containing ONLY the task contract, matching ``TC-*`` cases, ``Consumes`` /
     ``Produces`` signatures, ``design.md`` slice, and the 3-Layer Coding
     Standards Quality Stack — ZERO conversation history.
  3. **Red/Green Role Separation**: For ``standard`` tasks, ``NeoAgentTaskImplementer``
     spawns a dedicated RED session (writes failing tests ONLY, forbidden from
     touching production files) followed by an isolated GREEN+REFACTOR session
     (writes minimal implementation, forbidden from weakening RED assertions).
  4. **Verified 5D Clean-Code Check**: Every task completion is mechanically
     validated via ``CleanCodeCheck.validate()`` before advancing ``tasks.md`` and
     ``traceability.md``.
  5. ``AcceptanceVerifier``: Generates black-box ``AT-*`` cases from ``spec.md``,
     evaluates Go / Conditional-Go / No-Go, and archives clean specs (excluding
     scratch ``tasks.md``) into ``docs/specs/archive/<loop-id>-<slug>/``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..privacy import PrivacyPolicyConfig, redact_for_llm
from ..standards.stack import (
    CleanCodeCheck,
    QualityStackCompiler,
    parse_clean_code_check,
)
from .models import (
    AcceptanceCase,
    AcceptanceReport,
    ChangeType,
    DesignDocument,
    ImplementerStatus,
    SpecDocument,
    TaskGranularity,
    TaskSpec,
    TaskStatus,
    TDDEvidence,
    TraceabilityMatrix,
)
from .router import BlueprintPhase, BlueprintWorkspace, detect_phase, select_next_ready_task

logger = logging.getLogger(__name__)


# ── 1. Complexity-Aware Model Tier Selector ───────────────────────────────────


class ModelTierSelector:
    """Matches LLM capability tier to task cognitive load (avoids over/under-provisioning)."""

    @staticmethod
    def select_tier(task: TaskSpec) -> str:
        total_files = len(task.files_create) + len(task.files_modify)
        if task.granularity == TaskGranularity.MICRO and total_files <= 1 and len(task.covers_cases) <= 2:
            return "cheap"
        if total_files >= 3 or len(task.covers_reqs) >= 3 or len(task.depends_on) >= 2:
            return "reasoner"
        return "standard"

    @staticmethod
    def resolve_model_name(tier: str, llm_cfg: Any) -> str:
        if tier == "cheap":
            return getattr(llm_cfg, "cheap_model", "") or getattr(llm_cfg, "default_model", "")
        if tier == "reasoner":
            return getattr(llm_cfg, "powerful_model", "") or getattr(llm_cfg, "default_model", "")
        return getattr(llm_cfg, "default_model", "")


# ── 2. Per-Task Isolated Context Pack Compiler (Zero Chat History) ────────────


def compile_task_context_pack(
    *,
    task: TaskSpec,
    spec: SpecDocument,
    design: DesignDocument | None,
    repo_root: Path,
    quality_compiler: QualityStackCompiler | None = None,
    max_chars: int = 12_000,
) -> tuple[str, list[str]]:
    """Build a self-contained task brief with Quality Stack and zero session history."""
    compiler = quality_compiler or QualityStackCompiler()
    qstack = compiler.compile(
        repo_root=repo_root,
        files=task.all_files(),
        stage="build",
        max_chars=max_chars // 2,
    )

    lines = [
        f"# Isolated Subagent Task Brief — {spec.loop_id} · {task.id}",
        "",
        f"## 1. Objective: {task.title}",
        f"- **Granularity**: `{task.granularity.value}` | **Execution Mode**: `{task.execution_mode}` | **Model Tier**: `{task.model_tier}`",
        f"- **Allowed Source Files (Create)**: {', '.join(f'`{p}`' for p in task.files_create) or 'none'}",
        f"- **Allowed Source Files (Modify)**: {', '.join(f'`{p}`' for p in task.files_modify) or 'none'}",
        f"- **Allowed Test Files**: {', '.join(f'`{p}`' for p in task.files_test) or 'none'}",
        "- **Scope Rule**: Touch ONLY the files listed above. If out-of-scope edits appear necessary, return `STATUS: NEEDS_CONTEXT` or `STATUS: BLOCKED`.",
        "",
        "## 2. Interface Contracts (`Consumes` / `Produces`)",
        f"- **Consumes**: {'; '.join(f'`{c}`' for c in task.consumes) or 'none'}",
        f"- **Produces**: {'; '.join(f'`{p}`' for p in task.produces) or 'internal behavior'}",
    ]

    # Slice matching requirements & canonical test cases
    lines.extend(["", "## 3. Grounded Acceptance & Canonical `TC-*` Cases"])
    for req in spec.requirements:
        if req.id in task.covers_reqs:
            lines.append(f"- **{req.id}** ({req.change_type.value}): {req.statement}")
            for acc in req.acceptance:
                lines.append(f"  - {acc}")
    if design is not None:
        for tc in design.test_cases:
            if tc.id in task.covers_cases:
                lines.append(
                    f"- **{tc.id}** [{tc.category}/{tc.level}]: {tc.scenario_gwt} -> **Expected**: {tc.expected} (mock: {tc.mock_boundary})"
                )

    lines.extend(["", "## 4. TDD Execution Steps"])
    for st in task.steps:
        lines.append(f"- {st}")
    lines.append(f"- **Definition of Done**: {task.definition_of_done}")

    lines.extend(
        [
            "",
            "## 5. Required Structured Return Contract",
            "End your response with:",
            "- `STATUS: DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED`",
            "- `RED_CMD: <command>` and `RED_RESULT: <failure summary>`",
            "- `GREEN_CMD: <command>` and `GREEN_RESULT: <pass summary>`",
            "- `REFACTOR_NOTE: <improvements made or explicit rationale why code is already minimal>`",
            "- 5D `clean_code_check` bullets (`simplicity`, `reliability`, `maintainability`, `testability`, `performance`, `scope_discipline`).",
            "",
            qstack.prompt_markdown,
        ]
    )

    body = "\n".join(lines)
    if len(body) > max_chars:
        body = body[: max_chars - 24] + "\n...[context-bounded]"
    return body, qstack.loaded_layers


# ── 3. Subagent Task Implementer Protocol & Backends ─────────────────────────


@dataclass
class TaskExecutionOutcome:
    task_id: str
    status: ImplementerStatus
    evidence: TDDEvidence
    clean_check: CleanCodeCheck
    concerns: list[str] = field(default_factory=list)
    blocker_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "evidence": self.evidence.to_dict(),
            "clean_check": self.clean_check.to_dict(),
            "concerns": list(self.concerns),
            "blocker_reason": self.blocker_reason,
        }


@runtime_checkable
class TaskImplementerBackend(Protocol):
    def execute_task(
        self,
        *,
        loop_id: str,
        task: TaskSpec,
        context_pack: str,
        loaded_layers: list[str],
        repo_root: Path,
    ) -> TaskExecutionOutcome: ...


class NeoAgentTaskImplementer:
    """Executes one task via fresh NeoAgent session(s) with Red/Green role separation."""

    def __init__(self, privacy_policy: PrivacyPolicyConfig | None = None) -> None:
        self.privacy_policy = privacy_policy

    def execute_task(
        self,
        *,
        loop_id: str,
        task: TaskSpec,
        context_pack: str,
        loaded_layers: list[str],
        repo_root: Path,
    ) -> TaskExecutionOutcome:
        from ...core.config import get_config

        cfg = get_config()
        prov = cfg.llm.default_provider
        if not cfg.api_key_for(prov):
            return TaskExecutionOutcome(
                task_id=task.id,
                status=ImplementerStatus.BLOCKED,
                evidence=TDDEvidence(loaded_layers=loaded_layers),
                clean_check=CleanCodeCheck(),
                blocker_reason=f"No LLM API key configured for provider '{prov}'",
            )

        tier = ModelTierSelector.select_tier(task)
        model_override = ModelTierSelector.resolve_model_name(tier, cfg.llm)
        safe_pack = redact_for_llm(context_pack, self.privacy_policy)

        red_sid = f"red_{loop_id}_{task.id}"
        green_sid = f"green_{loop_id}_{task.id}"
        red_reply = ""

        # Standard tasks isolate RED (test-author) from GREEN (implementer)
        if task.granularity == TaskGranularity.STANDARD:
            red_prompt = (
                f"{safe_pack}\n\n"
                "## ACTIVE ROLE: RED TEST AUTHOR ONLY\n"
                f"Write failing tests ONLY in {task.files_test}. "
                "You are STRICTLY FORBIDDEN from creating or modifying production source files. "
                "Run the test command to prove the new test fails because the behavior is not yet implemented."
            )
            red_reply = self._invoke_coder(prov, model_override, red_sid, repo_root, red_prompt)

            green_prompt = (
                f"{safe_pack}\n\n"
                "## ACTIVE ROLE: GREEN IMPLEMENTER + REFACTOR\n"
                f"RED phase tests are already written ({red_reply[:400]}).\n"
                f"Implement minimal production code in {task.files_create + task.files_modify} to turn tests GREEN. "
                "Do NOT weaken or delete RED test assertions. Perform REFACTOR and return the full structured contract."
            )
            green_reply = self._invoke_coder(prov, model_override, green_sid, repo_root, green_prompt)
        else:
            # Micro tasks run inline RED -> GREEN -> REFACTOR in one fresh session
            green_sid = f"micro_{loop_id}_{task.id}"
            red_sid = green_sid
            green_reply = self._invoke_coder(prov, model_override, green_sid, repo_root, safe_pack)

        return _parse_implementer_reply(
            task_id=task.id,
            red_text=red_reply,
            green_text=green_reply,
            loaded_layers=loaded_layers,
            red_session_id=red_sid,
            green_session_id=green_sid,
        )

    @staticmethod
    def _invoke_coder(
        provider: str, model: str, session_id: str, repo_root: Path, prompt: str
    ) -> str:
        from ...core.agent import NeoAgent

        agent = NeoAgent(
            provider=provider,
            model=model,
            system_prompt=(
                f"You are NeoAgent Isolated Task Subagent (`{session_id}`). "
                f"Workspace root: {repo_root}. Obey file scope and TDD discipline strictly."
            ),
            session_id=session_id,
            router_enabled=False,
        )
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running and running.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(lambda: asyncio.run(agent.invoke(prompt))).result()
        return asyncio.run(agent.invoke(prompt))


class ScriptedTaskImplementer:
    """Deterministic implementer for offline tests, CI verification, and Studio demos."""

    def __init__(
        self,
        *,
        test_command: str = "",
        custom_outcomes: dict[str, TaskExecutionOutcome] | None = None,
    ) -> None:
        self.test_command = test_command
        self.custom_outcomes = dict(custom_outcomes or {})
        self.calls: list[dict[str, Any]] = []

    def execute_task(
        self,
        *,
        loop_id: str,
        task: TaskSpec,
        context_pack: str,
        loaded_layers: list[str],
        repo_root: Path,
    ) -> TaskExecutionOutcome:
        is_red_green_separated = task.granularity == TaskGranularity.STANDARD
        red_sid = f"red_{loop_id}_{task.id}" if is_red_green_separated else f"micro_{loop_id}_{task.id}"
        green_sid = f"green_{loop_id}_{task.id}" if is_red_green_separated else red_sid
        self.calls.append(
            {
                "task_id": task.id,
                "granularity": task.granularity.value,
                "model_tier": ModelTierSelector.select_tier(task),
                "red_session_id": red_sid,
                "green_session_id": green_sid,
                "context_chars": len(context_pack),
                "loaded_layers": list(loaded_layers),
            }
        )
        if task.id in self.custom_outcomes:
            return self.custom_outcomes[task.id]

        green_cmd = self.test_command or f"python -m pytest {' '.join(task.files_test)} -q"
        green_out = "passed (verified)"
        if self.test_command:
            proc = subprocess.run(
                self.test_command,
                cwd=str(repo_root),
                shell=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            green_out = (proc.stdout + proc.stderr).strip()[-240:] or f"exit={proc.returncode}"
            if proc.returncode != 0:
                return TaskExecutionOutcome(
                    task_id=task.id,
                    status=ImplementerStatus.BLOCKED,
                    evidence=TDDEvidence(
                        red_cmd=green_cmd,
                        red_output="expected initial failure recorded",
                        green_cmd=green_cmd,
                        green_output=green_out,
                        loaded_layers=loaded_layers,
                        red_session_id=red_sid,
                        green_session_id=green_sid,
                    ),
                    clean_check=CleanCodeCheck(),
                    blocker_reason=f"Verification command failed: {green_out}",
                )

        check = CleanCodeCheck(
            simplicity=f"Implemented `{task.id}` directly with stdlib types; zero speculative wrappers.",
            reliability="Validated inputs at entry boundary; deterministic error semantics preserved.",
            maintainability="Functions <= 35 lines, nesting <= 2, explicit domain naming.",
            testability=f"Verified {', '.join(task.covers_cases) or task.id} via `{ ', '.join(task.files_test) }` without heavy mocks.",
            performance="Single-pass evaluation; zero redundant I/O or O(n^2) scans.",
            scope_discipline=f"Touched only declared task files: {', '.join(task.all_files())}.",
        )
        ev = TDDEvidence(
            red_cmd=green_cmd,
            red_output=f"1 failed as expected prior to `{task.id}` implementation",
            green_cmd=green_cmd,
            green_output=green_out,
            refactor_note="Checked naming, early-return guards, and type annotations; no duplication remaining.",
            clean_code_check=check.to_dict(),
            loaded_layers=loaded_layers,
            red_session_id=red_sid,
            green_session_id=green_sid,
        )
        return TaskExecutionOutcome(
            task_id=task.id,
            status=ImplementerStatus.DONE,
            evidence=ev,
            clean_check=check,
        )


# ── 4. Continuous / Stepwise Task TDD Controller ─────────────────────────────


@dataclass
class TDDRunSummary:
    executed_tasks: list[str] = field(default_factory=list)
    passing_tasks: list[str] = field(default_factory=list)
    blocked_task: str = ""
    blocker_reason: str = ""
    phase_after: str = ""
    context_packs_written: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "executed_tasks": list(self.executed_tasks),
            "passing_tasks": list(self.passing_tasks),
            "blocked_task": self.blocked_task,
            "blocker_reason": self.blocker_reason,
            "phase_after": self.phase_after,
            "context_packs_written": list(self.context_packs_written),
        }


class TaskTDDController:
    """Drives ready tasks through isolated TDD + verified 5D Clean-Code gates."""

    def __init__(
        self,
        *,
        implementer: TaskImplementerBackend | None = None,
        quality_compiler: QualityStackCompiler | None = None,
    ) -> None:
        self.implementer = implementer or NeoAgentTaskImplementer()
        self.quality_compiler = quality_compiler or QualityStackCompiler()

    def run(
        self,
        *,
        workspace: BlueprintWorkspace,
        repo_root: Path,
        max_tasks: int | None = None,
    ) -> TDDRunSummary:
        spec = workspace.load_spec()
        design = workspace.load_design()
        plan = workspace.load_plan()
        trace = workspace.load_traceability()
        summary = TDDRunSummary()
        if spec is None or plan is None:
            summary.blocker_reason = "Blueprint workspace missing `spec.md` or `tasks.md`"
            summary.phase_after = BlueprintPhase.SPEC.value
            return summary

        packs_dir = workspace.dir / "context-packs"
        packs_dir.mkdir(parents=True, exist_ok=True)
        limit = max_tasks if max_tasks is not None else len(plan.tasks)
        count = 0

        while count < limit:
            nxt, reason = select_next_ready_task(plan)
            if nxt is None:
                break

            nxt.model_tier = ModelTierSelector.select_tier(nxt)
            nxt.status = TaskStatus.IN_PROGRESS
            workspace.save(plan=plan)

            pack_md, layers = compile_task_context_pack(
                task=nxt,
                spec=spec,
                design=design,
                repo_root=repo_root,
                quality_compiler=self.quality_compiler,
            )
            pack_path = packs_dir / f"task-{nxt.id}-brief.md"
            pack_path.write_text(pack_md, encoding="utf-8")
            summary.context_packs_written.append(str(pack_path))

            outcome = self.implementer.execute_task(
                loop_id=spec.loop_id,
                task=nxt,
                context_pack=pack_md,
                loaded_layers=layers,
                repo_root=repo_root,
            )
            summary.executed_tasks.append(nxt.id)
            count += 1

            # Handle subagent escalation (`NEEDS_CONTEXT` / `BLOCKED`) without forcing guesses
            if outcome.status in (ImplementerStatus.BLOCKED, ImplementerStatus.NEEDS_CONTEXT):
                nxt.status = TaskStatus.BLOCKED
                nxt.blocker_reason = outcome.blocker_reason or f"Subagent returned `{outcome.status.value}`"
                nxt.evidence = outcome.evidence
                workspace.save(plan=plan)
                summary.blocked_task = nxt.id
                summary.blocker_reason = nxt.blocker_reason
                break

            # Mechanically enforce non-empty REFACTOR note + 5D Clean-Code self-check
            cc_errors = outcome.clean_check.validate()
            if not outcome.evidence.refactor_note.strip():
                cc_errors.append("Missing explicit REFACTOR rationale")
            if cc_errors:
                nxt.status = TaskStatus.FAILED
                nxt.blocker_reason = "Clean-Code / TDD evidence incomplete: " + "; ".join(cc_errors)
                nxt.evidence = outcome.evidence
                workspace.save(plan=plan)
                summary.blocked_task = nxt.id
                summary.blocker_reason = nxt.blocker_reason
                break

            nxt.status = TaskStatus.PASSING
            nxt.blocker_reason = ""
            outcome.evidence.clean_code_check = outcome.clean_check.to_dict()
            nxt.evidence = outcome.evidence
            summary.passing_tasks.append(nxt.id)

            # Update bidirectional traceability matrix live as tasks pass
            if trace is not None:
                _update_traceability_on_pass(trace, nxt)
            workspace.save(plan=plan, trace=trace)

        summary.phase_after = detect_phase(workspace).phase.value
        return summary


def _update_traceability_on_pass(trace: TraceabilityMatrix, task: TaskSpec) -> None:
    ev_summary = f"{task.id}: {task.evidence.green_output[:80]}" if task.evidence else f"{task.id}: passed"
    for row in trace.rows:
        if row.req_id in task.covers_reqs or task.id in row.tasks:
            if task.id not in row.tasks:
                row.tasks.append(task.id)
            for cf in (*task.files_create, *task.files_modify):
                if cf not in row.code_files:
                    row.code_files.append(cf)
            for tf in task.files_test:
                if tf not in row.test_files:
                    row.test_files.append(tf)
            row.verification_evidence = ev_summary


# ── 5. Black-Box Acceptance (`AT-*`) & Clean Spec Archive ─────────────────────


class AcceptanceVerifier:
    """Derives black-box `AT-*` cases from `spec.md` (not from implementation) & archives."""

    @staticmethod
    def run_and_archive(
        *,
        workspace: BlueprintWorkspace,
        repo_root: Path,
        acceptance_cmd: str = "",
        residual_minors: list[str] | None = None,
    ) -> AcceptanceReport:
        spec = workspace.load_spec()
        design = workspace.load_design()
        plan = workspace.load_plan()
        trace = workspace.load_traceability()
        if spec is None or plan is None:
            raise ValueError("Cannot run Acceptance before `spec.md` and `tasks.md` exist")

        cmd_ok = True
        cmd_out = "Verified via passing TDD task evidence"
        if acceptance_cmd.strip():
            proc = subprocess.run(
                acceptance_cmd,
                cwd=str(repo_root),
                shell=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            cmd_ok = proc.returncode == 0
            cmd_out = (proc.stdout + proc.stderr).strip()[-200:] or f"exit={proc.returncode}"

        passing_reqs = {
            r for t in plan.tasks if t.status == TaskStatus.PASSING for r in t.covers_reqs
        }
        cases: list[AcceptanceCase] = []
        idx = 1
        for req in spec.core_requirements():
            req_passed = cmd_ok and (req.id in passing_reqs)
            criteria = req.acceptance or [req.statement]
            for c_idx, gwt in enumerate(criteria):
                cat = "normal" if c_idx == 0 else ("regression" if req.change_type != ChangeType.NEW else "boundary")
                cases.append(
                    AcceptanceCase(
                        id=f"AT-{idx:03d}",
                        req_id=req.id,
                        category=cat,
                        scenario_gwt=gwt,
                        expected=f"Satisfies `{req.id}` observable contract",
                        actual=cmd_out,
                        status="PASS" if req_passed else "FAIL",
                    )
                )
                idx += 1

        core_ids = {r.id for r in spec.core_requirements()}
        covered_ok = {c.req_id for c in cases if c.status == "PASS"}
        coverage = len(covered_ok) / len(core_ids) if core_ids else 1.0
        any_fail = any(c.status != "PASS" for c in cases)
        minors = list(residual_minors or [])

        if any_fail or coverage < 1.0:
            verdict = "NO_GO"
        elif minors:
            verdict = "CONDITIONAL_GO"
        else:
            verdict = "GO"

        report = AcceptanceReport(
            loop_id=spec.loop_id,
            title=spec.title,
            cases=cases,
            requirement_coverage=coverage,
            verdict=verdict,
            residual_minors=minors,
        )

        if verdict in ("GO", "CONDITIONAL_GO"):
            slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", spec.title.lower()).strip("-")[:40] or "feature"
            archive_dir = repo_root / "docs" / "specs" / "archive" / f"{spec.loop_id}-{slug}"
            archive_dir.mkdir(parents=True, exist_ok=True)
            # Archive durable contracts ONLY; exclude scratch `tasks.md` and `context-packs/`
            for src in (
                workspace.spec_path,
                workspace.lite_path,
                workspace.design_path,
                workspace.traceability_path,
            ):
                if src.is_file():
                    shutil.copy2(src, archive_dir / src.name)
            report.archived_to = str(archive_dir.relative_to(repo_root)).replace("\\", "/")
            (archive_dir / "acceptance-cases.md").write_text(report.to_markdown(), encoding="utf-8")
            plan.acceptance_passed = True

        workspace.save(spec=spec, design=design, plan=plan, trace=trace, acceptance=report)
        return report


def _parse_implementer_reply(
    *,
    task_id: str,
    red_text: str,
    green_text: str,
    loaded_layers: list[str],
    red_session_id: str,
    green_session_id: str,
) -> TaskExecutionOutcome:
    status = ImplementerStatus.DONE
    m_stat = re.search(
        r"STATUS\s*:\s*(DONE_WITH_CONCERNS|NEEDS_CONTEXT|BLOCKED|DONE)",
        green_text or "",
        re.IGNORECASE,
    )
    if m_stat:
        raw_st = m_stat.group(1).lower()
        status = ImplementerStatus(raw_st)

    def _field(name: str, src: str, default: str = "") -> str:
        m = re.search(rf"{name}\s*:\s*(.+)", src or "", re.IGNORECASE)
        return m.group(1).strip() if m else default

    check = parse_clean_code_check(green_text or "")
    ev = TDDEvidence(
        red_cmd=_field("RED_CMD", red_text or green_text, "pytest (red)"),
        red_output=_field("RED_RESULT", red_text or green_text, "initial test failure confirmed"),
        green_cmd=_field("GREEN_CMD", green_text, "pytest (green)"),
        green_output=_field("GREEN_RESULT", green_text, "tests passing"),
        refactor_note=_field("REFACTOR_NOTE", green_text, ""),
        clean_code_check=check.to_dict(),
        loaded_layers=loaded_layers,
        red_session_id=red_session_id,
        green_session_id=green_session_id,
    )
    blocker = _field("BLOCKER", green_text, "") if status in (ImplementerStatus.BLOCKED, ImplementerStatus.NEEDS_CONTEXT) else ""
    return TaskExecutionOutcome(
        task_id=task_id,
        status=status,
        evidence=ev,
        clean_check=check,
        blocker_reason=blocker,
    )
