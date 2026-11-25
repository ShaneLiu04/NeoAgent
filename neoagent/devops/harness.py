"""Systematic 7-Layer Harness Engineering Runtime (`L1` through `L7`).

Translates the seven-layer Harness Engineering paradigm into concrete,
deterministic runtime primitives used across ``DevOpsEngine``:

  - **L1 Information Boundary & 4-Layer AI-Native Repo Topology**:
      * Stage-scoped ``InformationBoundary`` (``must_know`` / ``optional_know`` /
        ``forbidden_know`` / ``explicit_non_goals``).
      * Two-phase ``RepoScaffold`` (``schema`` ``AGENTS.md`` -> ``index`` ``docs/AI-Map.md``
        -> ``wiki`` ``docs/{specs,changes,architecture,knowledges}``, raw kept out-of-repo).
      * Lightweight vs Full SDD router (``route_workflow_weight``).
  - **L2 Tool System & Output Distillation (Carlini Pattern)**:
      * ``ToolOutputDistiller`` keeps verbose stdout/JSON out of context and writes
        raw logs to disk in single-line grep-friendly format.
  - **L3 Execution Orchestration & 3-Condition Subagent Dispatch Gate**:
      * ``SubagentDispatchGate`` enforces ``self_contained_input AND structured_output
        AND high_independence`` while keeping decision/orchestration/state/synthesis
        strictly on the main thread.
  - **L4 Memory & State — 40% Context Zone & Anthropic Context Reset**:
      * ``ContextBudgetMonitor`` classifies ``SMART (<=30%)`` / ``WARNING (30-40%)`` /
        ``DUMB (>40%)`` zones and emits ``checkpoint-handoff.md`` on Context Reset.
      * ``enforce_subagent_return_boundary`` prevents raw subagent traces from
        polluting main-thread context.
  - **L5 Evaluation & Observability — Spec Phase Gate & Sprint Checkpoints**:
      * ``SpecPhaseGate`` verifies S/D/C completeness of ``req.md`` / ``design.md``
        independently of the author; ``SprintTracker`` bounds multi-step plans.
  - **L6 Mechanical Constraints & 5-Tier Recovery Ladder**:
      * ``RecoveryPolicy`` maps failure signals deterministically across
        ``retry_transient -> repair_and_retry -> rollback_checkpoint ->
        degrade_gracefully -> escalate_human``.
  - **L7 Self-Evolution — SAFE Loop & Hashimoto Error-Prevention Flywheel**:
      * ``EvolutionManager`` implements ``Sense -> Adapt -> Formalize -> Evaluate``
        with 3-tier change governance (``data`` / ``rule`` / ``structural``) and
        strictly blocks any proposal that weakens L6 hard constraints.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .models import FailureKind, RiskTier, Stage

# ══════════════════════════════════════════════════════════════════════════════
# L1: Information Boundary, SDD Weight Router & 4-Layer AI-Native Repo Scaffold
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class InformationBoundary:
    """L1 role contract defining what an agent MUST know, MAY know, and MUST NOT see/do."""

    stage: str
    role: str
    must_know: tuple[str, ...]
    optional_know: tuple[str, ...]
    forbidden_know: tuple[str, ...]
    explicit_non_goals: tuple[str, ...]


STAGE_BOUNDARIES: dict[str, InformationBoundary] = {
    Stage.INTAKE.value: InformationBoundary(
        stage="intake",
        role="Requirement & Blast-Radius Triage Architect",
        must_know=("feature_intent", "impacted_files", "risk_policy"),
        optional_know=("architecture_index", "historical_cases"),
        forbidden_know=("unrelated_module_source", "raw_meeting_transcripts"),
        explicit_non_goals=("Do NOT write implementation code in INTAKE", "Do NOT downgrade auto-detected risk"),
    ),
    Stage.BUILD.value: InformationBoundary(
        stage="build",
        role="TDD Implementation Generator",
        must_know=("task_or_fix_brief", "target_source_files", "language_coding_standards"),
        optional_know=("design_spec", "reusable_component_index"),
        forbidden_know=("reviewer_internal_chain_of_thought", "production_credentials"),
        explicit_non_goals=(
            "Do NOT self-certify review findings as fixed",
            "Do NOT delete assertions, skip tests, or lower coverage thresholds",
        ),
    ),
    Stage.REVIEW.value: InformationBoundary(
        stage="review",
        role="Independent Multi-Perspective Evaluator",
        must_know=("unified_git_diff", "review_rubric", "bug_pattern_checklist"),
        optional_know=("requirement_spec", "touched_file_ast"),
        forbidden_know=("generator_reasoning_history", "author_excuses"),
        explicit_non_goals=(
            "Do NOT modify source files during REVIEW",
            "Do NOT flood Minor/Info nitpicks above density cap",
        ),
    ),
    Stage.PIPELINE.value: InformationBoundary(
        stage="pipeline",
        role="Deterministic Verification & Failure Triage Runner",
        must_know=("pipeline_steps_config", "distilled_failure_digest"),
        optional_know=("flaky_retry_history", "coverage_report"),
        forbidden_know=("full_unfiltered_debug_logs_in_prompt",),
        explicit_non_goals=("Do NOT auto-edit source code when failure_kind == env_blocker",),
    ),
    Stage.RELEASE.value: InformationBoundary(
        stage="release",
        role="Progressive Canary & Rollback Controller",
        must_know=("canary_stages", "rollback_cmd", "health_slo_thresholds"),
        optional_know=("baseline_telemetry",),
        forbidden_know=("unverified_ad_hoc_shell_commands",),
        explicit_non_goals=("NEVER deploy without a verified rollback command",),
    ),
}


def route_workflow_weight(
    *,
    files: list[str],
    changed_lines: int,
    tier: RiskTier,
    title: str = "",
) -> dict[str, Any]:
    """SDD Router: distinguish lightweight bugfix/UT tasks (`lite`) from full SDD (`full`)."""
    is_low_blast = tier == RiskTier.LOW and len(files) <= 3 and changed_lines <= 80
    lite_keywords = re.search(r"(?i)\b(typo|ut|unit test|lint|small fix|minor bug|doc)\b|小修|补单测", title)
    if is_low_blast and (lite_keywords or changed_lines <= 40):
        return {
            "track": "lite",
            "phases": ["intake", "build", "review", "pipeline", "release"],
            "requires_spec_doc": False,
            "reason": "Low-risk, small-blast-radius task routed to fast TDD Lite track",
        }
    return {
        "track": "full",
        "phases": ["intake", "spec_gate", "build", "review", "pipeline", "release", "observe", "learn"],
        "requires_spec_doc": tier in (RiskTier.MEDIUM, RiskTier.HIGH) or changed_lines > 200,
        "reason": f"Tier={tier.value}, files={len(files)}, lines={changed_lines} routed to full SDD track",
    }


PENDING_FILL_MARKER = "<!-- harness:pending-fill -->"


@dataclass
class RepoDiagnosis:
    """Phase A read-only structural diagnosis for the 4-layer AI-native repository model."""

    mode: str  # scaffold | refactor | aligned
    existing_paths: list[str] = field(default_factory=list)
    missing_paths: list[str] = field(default_factory=list)
    anti_patterns: list[str] = field(default_factory=list)
    planned_actions: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "existing_paths": self.existing_paths,
            "missing_paths": self.missing_paths,
            "anti_patterns": self.anti_patterns,
            "planned_actions": self.planned_actions,
        }


class RepoScaffold:
    """Two-Phase 4-Layer AI-Native Repository Scaffold (`schema / index / wiki / raw`).

    Target canonical layout:
      - ``AGENTS.md``                          (schema: concise router -> ``docs/AI-Map.md``)
      - ``docs/AI-Map.md``                     (index: top-level map of specs/changes/architecture/knowledges)
      - ``docs/specs/features/.gitkeep``       (wiki·baseline: module-level verified specs)
      - ``docs/changes/.gitkeep``              (wiki·incremental: ``YYYYMM/<feature>/`` work-in-progress)
      - ``docs/architecture/.gitkeep``         (wiki·architecture: system design & ADRs)
      - ``docs/knowledges/domains/.gitkeep``   (wiki·domain knowledge)
      - ``docs/knowledges/cases/index.md``     (wiki·historical incident/bug cases)
    """

    REQUIRED_TARGETS: tuple[tuple[str, str], ...] = (
        ("AGENTS.md", "schema"),
        ("docs/AI-Map.md", "index"),
        ("docs/specs/features/.gitkeep", "wiki-baseline"),
        ("docs/changes/.gitkeep", "wiki-incremental"),
        ("docs/architecture/.gitkeep", "wiki-architecture"),
        ("docs/knowledges/domains/.gitkeep", "wiki-domains"),
        ("docs/knowledges/cases/index.md", "wiki-cases"),
    )

    @classmethod
    def diagnose(cls, root: Path) -> RepoDiagnosis:
        """Phase A: strictly read-only scan of repository documentation topology."""
        base = root.resolve()
        existing: list[str] = []
        missing: list[str] = []
        anti_patterns: list[str] = []
        actions: list[dict[str, str]] = []

        # Anti-pattern checks
        if (base / "docs" / "specs" / "AI-Map.md").is_file():
            anti_patterns.append("AI-Map.md misplaced under docs/specs/ (must reside at index layer docs/AI-Map.md)")
        if (base / "docs" / "req.md").is_file() or (base / "docs" / "srs.md").is_file():
            anti_patterns.append("Flat requirement doc directly under docs/ (baseline belongs in docs/specs/features/<module>/)")
        if (base / "docs" / "baseline").is_dir():
            anti_patterns.append("Legacy docs/baseline/ directory detected (migrate to docs/specs/features/<module>/)")
        agents_md = base / "AGENTS.md"
        if agents_md.is_file():
            line_cnt = len(agents_md.read_text(encoding="utf-8", errors="replace").splitlines())
            if line_cnt > 120:
                anti_patterns.append(f"AGENTS.md is {line_cnt} lines (>120 line schema budget; move details into docs/)")

        for rel_path, layer in cls.REQUIRED_TARGETS:
            target = base / rel_path
            if target.is_file() or (rel_path.endswith(".gitkeep") and target.parent.is_dir() and any(target.parent.iterdir())):
                existing.append(rel_path)
            else:
                missing.append(rel_path)
                actions.append({"action": "create", "layer": layer, "path": rel_path})

        if not missing and not anti_patterns:
            mode = "aligned"
        elif anti_patterns and existing:
            mode = "refactor"
        else:
            mode = "scaffold"

        return RepoDiagnosis(
            mode=mode,
            existing_paths=existing,
            missing_paths=missing,
            anti_patterns=anti_patterns,
            planned_actions=actions,
        )

    @classmethod
    def apply(cls, root: Path, diagnosis: RepoDiagnosis | None = None) -> list[str]:
        """Phase B: materialize missing 4-layer skeleton files non-destructively (never overwrites)."""
        base = root.resolve()
        diag = diagnosis or cls.diagnose(base)
        created: list[str] = []

        for item in diag.planned_actions:
            rel = item["path"]
            target = base / rel
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if rel == "AGENTS.md":
                target.write_text(
                    "# AGENTS.md — AI-Native Repository Schema\n\n"
                    f"{PENDING_FILL_MARKER}\n"
                    "- **Documentation Index**: See `docs/AI-Map.md`\n"
                    "- **Context Budget**: Keep prompt context < 40%; use file handoffs.\n"
                    "- **Quality Red Lines**: TDD first; never weaken tests or commit secrets.\n",
                    encoding="utf-8",
                )
            elif rel == "docs/AI-Map.md":
                target.write_text(
                    "---\n"
                    "type: ProjectMap\n"
                    "title: AI-Map\n"
                    "description: Top-level index pointing to specs, changes, architecture, and knowledges.\n"
                    "---\n"
                    f"{PENDING_FILL_MARKER}\n"
                    "# AI-Map\n\n"
                    "## Module Baselines (`docs/specs/features/<module>/`)\n"
                    "- _pending-fill_\n\n"
                    "## Active Changes (`docs/changes/YYYYMM/<feature>/`)\n"
                    "- _pending-fill_\n\n"
                    "## Architecture (`docs/architecture/`)\n"
                    "- `ARCHITECTURE_DEVOPS.md` / ADRs\n\n"
                    "## Knowledge & Incident Cases (`docs/knowledges/`)\n"
                    "- `domains/` & `cases/index.md`\n",
                    encoding="utf-8",
                )
            elif rel == "docs/knowledges/cases/index.md":
                target.write_text("# Incident & Defect Cases\n\n* _pending-fill_\n", encoding="utf-8")
            else:
                target.write_text("", encoding="utf-8")
            created.append(rel)

        return created


# ══════════════════════════════════════════════════════════════════════════════
# L2: Tool System & Output Distillation (Carlini Single-Line Log Pattern)
# ══════════════════════════════════════════════════════════════════════════════


class ToolOutputDistiller:
    """Distills verbose tool/command outputs before context injection (L2)."""

    _ERROR_LINE_RE = re.compile(
        r"(?i)(error|exception|traceback|failed|fatal|assert|panic|denied|critical|warning)"
    )

    @classmethod
    def distill_command_output(
        cls,
        raw_output: str,
        *,
        max_lines: int = 30,
        max_chars: int = 2_400,
        artifact_log_path: Path | None = None,
    ) -> str:
        if artifact_log_path is not None:
            artifact_log_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_log_path.write_text(raw_output, encoding="utf-8")

        if len(raw_output) <= max_chars and raw_output.count("\n") <= max_lines:
            return raw_output.strip()

        lines = [ln.rstrip() for ln in raw_output.splitlines() if ln.strip()]
        salient = [ln for ln in lines if cls._ERROR_LINE_RE.search(ln)]
        tail = lines[-10:]
        merged: list[str] = []
        for ln in salient[: max_lines - 10] + tail:
            if ln not in merged:
                merged.append(ln)

        summary = "\n".join(merged)[:max_chars]
        ref_note = f" (full log archived at {artifact_log_path.name})" if artifact_log_path else ""
        return f"[Distilled {len(lines)} -> {len(merged)} salient lines{ref_note}]\n{summary}"

    @staticmethod
    def distill_json(payload: Any, keep_keys: tuple[str, ...], max_items: int = 10) -> str:
        if isinstance(payload, dict):
            filtered = {k: payload[k] for k in keep_keys if k in payload}
            return json.dumps(filtered, ensure_ascii=False)
        if isinstance(payload, list):
            trimmed = [
                {k: item[k] for k in keep_keys if isinstance(item, dict) and k in item}
                for item in payload[:max_items]
            ]
            return json.dumps(trimmed, ensure_ascii=False)
        return str(payload)[:1_000]

    @staticmethod
    def append_grep_log(log_file: Path, level: str, component: str, message: str) -> None:
        """Carlini pattern: single-line grep-friendly `LEVEL: [component] message` on disk."""
        log_file.parent.mkdir(parents=True, exist_ok=True)
        clean_msg = " ".join(message.splitlines()).strip()
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"{level.upper()}: [{ts}] [{component}] {clean_msg}\n")


# ══════════════════════════════════════════════════════════════════════════════
# L3: Subagent 3-Condition Dispatch Gate (Context Isolation vs Continuity)
# ══════════════════════════════════════════════════════════════════════════════

MAINLINE_ONLY_TASKS = frozenset(
    {
        "decision_reasoning",
        "orchestration_control",
        "state_management",
        "user_interaction",
        "artifact_synthesis",
    }
)


@dataclass(frozen=True)
class DispatchDecision:
    dispatch: bool
    reason: str


class SubagentDispatchGate:
    """Enforces the L3 3-Condition Subagent Dispatch Protocol:
    1. ``self_contained_input``: Prompt carries full input without referencing main chat history.
    2. ``structured_output``: Output obeys an explicit JSON/Markdown schema.
    3. ``high_independence``: Failure has a fallback and does not block main-thread reasoning.
    """

    @staticmethod
    def evaluate(
        task_kind: str,
        *,
        self_contained_input: bool,
        structured_output: bool,
        high_independence: bool,
        estimated_Read_lines: int = 100,
    ) -> DispatchDecision:
        if task_kind in MAINLINE_ONLY_TASKS:
            return DispatchDecision(
                False,
                f"Task '{task_kind}' is reserved for mainline to preserve reasoning & state continuity",
            )
        if estimated_Read_lines < 25:
            return DispatchDecision(
                False,
                f"Trivial scope ({estimated_Read_lines} lines) — subagent dispatch overhead exceeds isolation gain",
            )
        missing: list[str] = []
        if not self_contained_input:
            missing.append("self_contained_input")
        if not structured_output:
            missing.append("structured_output")
        if not high_independence:
            missing.append("high_independence")
        if missing:
            return DispatchDecision(
                False,
                f"Subagent dispatch denied; unmet L3 condition(s): {', '.join(missing)}",
            )
        return DispatchDecision(True, f"Approved isolated subagent dispatch for '{task_kind}'")


# ══════════════════════════════════════════════════════════════════════════════
# L4: 40% Context Zone Monitor, Subagent Return Boundary & Context Reset
# ══════════════════════════════════════════════════════════════════════════════


class ContextZone(str, Enum):
    SMART = "smart"        # 0% - 30%: high focus, minimal noise
    WARNING = "warning"    # 30% - 40%: compress history & summarize
    DUMB = "dumb"          # > 40%: mandatory Context Reset via checkpoint handoff


@dataclass(frozen=True)
class ContextBudgetStatus:
    used_tokens: int
    window_tokens: int
    ratio: float
    zone: ContextZone
    recommended_action: str


class ContextBudgetMonitor:
    """Monitors the 40% context window ceiling (Dex Horthy / Anthropic Context Reset)."""

    def __init__(
        self,
        window_tokens: int = 128_000,
        smart_ceiling: float = 0.30,
        hard_reset_ceiling: float = 0.40,
    ) -> None:
        self.window_tokens = max(1_000, window_tokens)
        self.smart_ceiling = smart_ceiling
        self.hard_reset_ceiling = hard_reset_ceiling

    def check(self, used_tokens: int) -> ContextBudgetStatus:
        ratio = round(max(0, used_tokens) / self.window_tokens, 4)
        if ratio <= self.smart_ceiling:
            return ContextBudgetStatus(
                used_tokens, self.window_tokens, ratio, ContextZone.SMART, "continue_in_smart_zone"
            )
        if ratio <= self.hard_reset_ceiling:
            return ContextBudgetStatus(
                used_tokens,
                self.window_tokens,
                ratio,
                ContextZone.WARNING,
                "compress_intermediate_history",
            )
        return ContextBudgetStatus(
            used_tokens,
            self.window_tokens,
            ratio,
            ContextZone.DUMB,
            "trigger_context_reset_handoff",
        )


def write_context_reset_handoff(
    artifact_dir: Path,
    *,
    goal: str,
    current_stage: str,
    completed_summaries: list[str],
    next_action: str,
    watchouts: list[str] | None = None,
) -> Path:
    """Persist structured checkpoint handoff so a fresh Agent resumes with <5% context load."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    handoff_path = artifact_dir / "checkpoint-handoff.md"
    done_md = "\n".join(f"- {s[:220]}" for s in completed_summaries) or "- (none yet)"
    watch_md = "\n".join(f"- {w[:220]}" for w in (watchouts or [])) or "- Keep context < 40%"
    handoff_path.write_text(
        f"# Context Reset Handoff Document\n\n"
        f"## 1. Original Objective\n{goal}\n\n"
        f"## 2. Current Stage\n`{current_stage}`\n\n"
        f"## 3. Distilled Completed Outputs\n{done_md}\n\n"
        f"## 4. Immediate Next Action\n{next_action}\n\n"
        f"## 5. Watchouts & Constraints\n{watch_md}\n",
        encoding="utf-8",
    )
    return handoff_path


def enforce_subagent_return_boundary(
    subagent_name: str,
    raw_payload: str,
    verdict_line: str,
    artifact_dir: Path,
) -> str:
    """L4 Hard Boundary: write full subagent output to disk; inject <= 4 lines into mainline."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", subagent_name).strip("-") or "subagent"
    out_file = artifact_dir / f"subagent-{safe_name}.out.md"
    out_file.write_text(raw_payload, encoding="utf-8")
    compact_verdict = " ".join(verdict_line.splitlines())[:240]
    return f"[{subagent_name}] {compact_verdict}\nArtifact: {out_file}"


# ══════════════════════════════════════════════════════════════════════════════
# L5: Independent Spec Phase Gate & Sprint Checkpoint Tracker
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class PhaseGateReport:
    phase: str
    passed: bool
    missing_sections: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class SpecPhaseGate:
    """L5 Read-Only Deliverable Gate verifying `req.md`/`srs.md` & `design.md` before coding."""

    REQ_HEADINGS = ("requirement", "acceptance", "scope")
    REQ_HEADINGS_CJK = ("需求", "验收", "范围", "约束")
    DESIGN_HEADINGS = ("architecture", "interface", "test")
    DESIGN_HEADINGS_CJK = ("架构", "接口", "模块", "测试", "方案")

    @classmethod
    def verify_spec_file(cls, spec_path: Path, phase: str = "req") -> PhaseGateReport:
        if not spec_path.is_file():
            return PhaseGateReport(phase, False, [f"Missing deliverable file: {spec_path.name}"])
        raw = spec_path.read_text(encoding="utf-8", errors="replace")
        if PENDING_FILL_MARKER in raw:
            return PhaseGateReport(phase, False, ["Deliverable still contains pending-fill placeholder marker"])
        lowered = raw.lower()
        missing: list[str] = []
        if phase == "req":
            if not (any(k in lowered for k in cls.REQ_HEADINGS) or any(k in raw for k in cls.REQ_HEADINGS_CJK)):
                missing.append("Requirement spec lacks explicit scope / acceptance criteria sections")
        elif phase == "design":
            if not (any(k in lowered for k in cls.DESIGN_HEADINGS) or any(k in raw for k in cls.DESIGN_HEADINGS_CJK)):
                missing.append("Design spec lacks architecture / interface / testability sections")
        if len(raw.strip()) < 60:
            missing.append("Deliverable content is too sparse (<60 chars) to serve as an engineering contract")
        return PhaseGateReport(phase, len(missing) == 0, missing)


@dataclass
class SprintSlice:
    sprint_no: int
    tasks: list[str]
    completed: bool = False


class SprintTracker:
    """Partitions long task lists into 1-3 step Sprints with natural Context Reset boundaries."""

    @staticmethod
    def partition(tasks: list[str], sprint_size: int = 3) -> list[SprintSlice]:
        step = max(1, min(3, sprint_size))
        return [
            SprintSlice(sprint_no=idx + 1, tasks=tasks[i : i + step])
            for idx, i in enumerate(range(0, len(tasks), step))
        ]


# ══════════════════════════════════════════════════════════════════════════════
# L6: 5-Tier Mechanical Recovery Ladder
# ══════════════════════════════════════════════════════════════════════════════


class RecoveryAction(str, Enum):
    RETRY_TRANSIENT = "retry_transient"          # Tier 1: transient I/O or flaky test
    REPAIR_AND_RETRY = "repair_and_retry"        # Tier 2: deterministic feedback -> fix brief
    ROLLBACK_CHECKPOINT = "rollback_checkpoint"  # Tier 3: canary SLO breach / telemetry loss
    DEGRADE_GRACEFULLY = "degrade_gracefully"    # Tier 4: optional LLM unavailable -> static AST
    ESCALATE_HUMAN = "escalate_human"            # Tier 5: budget exhausted / env blocker / oscillation


class RecoveryPolicy:
    """Deterministic L6 failure-to-recovery resolver (never guesses, never loops infinitely)."""

    @staticmethod
    def decide(
        *,
        failure_kind: FailureKind | None = None,
        attempt: int = 0,
        max_attempts: int = 3,
        oscillating: bool = False,
        canary_breached: bool = False,
        optional_backend_missing: bool = False,
        transient: bool = False,
    ) -> RecoveryAction:
        if canary_breached:
            return RecoveryAction.ROLLBACK_CHECKPOINT
        if optional_backend_missing:
            return RecoveryAction.DEGRADE_GRACEFULLY
        if oscillating or failure_kind == FailureKind.ENV_BLOCKER or attempt >= max_attempts:
            return RecoveryAction.ESCALATE_HUMAN
        if transient and attempt < max_attempts:
            return RecoveryAction.RETRY_TRANSIENT
        return RecoveryAction.REPAIR_AND_RETRY


# ══════════════════════════════════════════════════════════════════════════════
# L7: SAFE Self-Evolution Engine & Hashimoto Error-Prevention Flywheel
# ══════════════════════════════════════════════════════════════════════════════


class EvolutionScope(str, Enum):
    DATA = "data"              # Low risk: routing/mapping entry -> auto-applicable
    RULE = "rule"              # Medium risk: checklist / guardrail rule -> auditable
    STRUCTURAL = "structural"  # High risk: state machine / stage change -> requires human approval


@dataclass
class EvolutionProposal:
    id: str
    scope: EvolutionScope
    trigger_signal: str        # recurring_defect | three_strike_error | canary_rollback | unmapped_ext
    target_artifact: str       # e.g. ".bug-patterns/checklist.md" or "AGENTS.md"
    addition_summary: str
    rollback_hint: str
    weakens_l6_constraint: bool = False
    deletes_existing: bool = False
    single_point_change: bool = True
    approved_by: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope.value,
            "trigger_signal": self.trigger_signal,
            "target_artifact": self.target_artifact,
            "addition_summary": self.addition_summary,
            "rollback_hint": self.rollback_hint,
            "weakens_l6_constraint": self.weakens_l6_constraint,
            "deletes_existing": self.deletes_existing,
            "single_point_change": self.single_point_change,
            "approved_by": self.approved_by,
            "created_at": self.created_at,
        }


class EvolutionConstraintError(Exception):
    """Raised when an L7 evolution proposal violates L6/L7 hard safety invariants."""


class EvolutionManager:
    """Implements the L7 ``SAFE`` (`Sense -> Adapt -> Formalize -> Evaluate`) flywheel
    and Hashimoto 3-strike / rollback error-prevention loop under ``.neoagent-devops/evolution/``.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.evo_dir = self.root / ".neoagent-devops" / "evolution"
        self.pending_dir = self.evo_dir / "pending"
        self.applied_dir = self.evo_dir / "applied"
        self.changelog_path = self.evo_dir / "changelog.md"

    def _ensure_dirs(self) -> None:
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.applied_dir.mkdir(parents=True, exist_ok=True)
        if not self.changelog_path.is_file():
            self.changelog_path.write_text("# Harness L7 Self-Evolution Changelog\n\n", encoding="utf-8")

    def formalize(
        self,
        *,
        scope: EvolutionScope,
        trigger_signal: str,
        target_artifact: str,
        addition_summary: str,
        rollback_hint: str,
        weakens_l6_constraint: bool = False,
        deletes_existing: bool = False,
        single_point_change: bool = True,
    ) -> EvolutionProposal:
        """SAFE Step F (`Formalize`) + Step E (`Evaluate` against 5 L7 Hard Constraints)."""
        self._ensure_dirs()
        seq = 1 + len(list(self.pending_dir.glob("EVO-*.json"))) + len(list(self.applied_dir.glob("EVO-*.json")))
        prop = EvolutionProposal(
            id=f"EVO-{seq:04d}",
            scope=scope,
            trigger_signal=trigger_signal,
            target_artifact=target_artifact,
            addition_summary=addition_summary,
            rollback_hint=rollback_hint,
            weakens_l6_constraint=weakens_l6_constraint,
            deletes_existing=deletes_existing,
            single_point_change=single_point_change,
        )
        self.evaluate_constraints(prop)

        # Low-risk DATA extensions can apply immediately; RULE/STRUCTURAL stage in pending/
        if scope == EvolutionScope.DATA:
            return self.apply_proposal(prop, approved_by="auto-l7-data")

        (self.pending_dir / f"{prop.id}.json").write_text(
            json.dumps(prop.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._append_changelog(prop, status="PENDING_CONFIRMATION")
        return prop

    @staticmethod
    def evaluate_constraints(prop: EvolutionProposal) -> None:
        """Enforce the 5 non-negotiable L7 Hard Constraints."""
        if prop.weakens_l6_constraint:
            raise EvolutionConstraintError(
                f"{prop.id} rejected: L7 self-evolution is strictly forbidden from softening L6 hard constraints"
            )
        if prop.deletes_existing:
            raise EvolutionConstraintError(
                f"{prop.id} rejected: L7 self-evolution is append-only and cannot delete existing capabilities"
            )
        if not prop.single_point_change:
            raise EvolutionConstraintError(
                f"{prop.id} rejected: L7 requires atomic single-point evolution (no big-bang mutations)"
            )
        if not prop.rollback_hint.strip():
            raise EvolutionConstraintError(
                f"{prop.id} rejected: every L7 evolution proposal must declare an explicit rollback_hint"
            )

    def apply_proposal(self, prop: EvolutionProposal, *, approved_by: str) -> EvolutionProposal:
        self.evaluate_constraints(prop)
        if prop.scope == EvolutionScope.STRUCTURAL and (
            not approved_by.strip() or approved_by.startswith("auto")
        ):
            raise EvolutionConstraintError(
                f"{prop.id} rejected: STRUCTURAL evolution requires explicit human approval"
            )
        self._ensure_dirs()
        prop.approved_by = approved_by
        pending_file = self.pending_dir / f"{prop.id}.json"
        if pending_file.is_file():
            pending_file.unlink()
        (self.applied_dir / f"{prop.id}.json").write_text(
            json.dumps(prop.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._append_changelog(prop, status=f"APPLIED (by {approved_by})")
        return prop

    def record_hashimoto_guardrail(self, loop_id: str, reason: str, trigger: str) -> EvolutionProposal:
        """Hashimoto Error-Prevention Loop: on 3-strike defect or canary rollback,
        formalize a prevention rule proposal targeting `AGENTS.md` & `.bug-patterns/checklist.md`.
        """
        return self.formalize(
            scope=EvolutionScope.RULE,
            trigger_signal=trigger,
            target_artifact="AGENTS.md",
            addition_summary=f"[{loop_id}] Prevent recurrence: {reason[:200]}",
            rollback_hint="Revert the appended guardrail bullet in AGENTS.md via git checkout",
        )

    def list_proposals(self) -> dict[str, list[dict[str, Any]]]:
        self._ensure_dirs()
        pending = [
            json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(self.pending_dir.glob("EVO-*.json"))
        ]
        applied = [
            json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(self.applied_dir.glob("EVO-*.json"))
        ]
        return {"pending": pending, "applied": applied}

    def _append_changelog(self, prop: EvolutionProposal, status: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(prop.created_at))
        line = (
            f"- **{prop.id}** [{ts}] `{prop.scope.value}` | `{status}` | "
            f"signal=`{prop.trigger_signal}` -> `{prop.target_artifact}`: {prop.addition_summary}\n"
        )
        with self.changelog_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
