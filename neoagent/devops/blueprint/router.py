"""Artifact-State Phase Router, Topological Task Selector & Compaction Snapshot.

Reconstructs exact Blueprint execution state from disk artifacts alone — zero
reliance on chat memory, surviving crashes, context resets, and multi-day handoffs.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import (
    AcceptanceReport,
    DesignDocument,
    PhaseGateRecord,
    SpecDocument,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    TraceabilityMatrix,
    parse_markdown_gate_verdicts,
    parse_markdown_task_statuses,
)


class BlueprintPhase(str, enum.Enum):
    SPEC = "spec"                  # Missing spec.md / lite.md
    SPEC_GATE = "spec_gate"        # spec.md exists; awaiting/failed spec gate
    DESIGN = "design"              # Full track: spec passed; missing design.md
    DESIGN_GATE = "design_gate"    # design.md exists; awaiting/failed design gate
    PLAN_GATE = "plan_gate"        # tasks.md exists; awaiting/failed plan gate
    DEVELOP = "develop"            # Plan passed; pending/in_progress/failed tasks remain
    SDC_REVIEW = "sdc_review"      # All tasks passing; awaiting S/D/C 3D compliance
    ACCEPTANCE = "acceptance"      # S/D/C passed; awaiting black-box acceptance & archive
    COMPLETE = "complete"          # Acceptance Go & archived
    BLOCKED = "blocked"            # Escalated blocker (>=3 gate failures or blocked task)


@dataclass(frozen=True)
class BlueprintStatusSnapshot:
    phase: BlueprintPhase
    track: str  # full | lite | uninitialized
    reason: str
    next_task_id: str = ""
    total_tasks: int = 0
    passing_tasks: int = 0
    failed_tasks: int = 0
    blocked_tasks: int = 0
    gate_verdicts: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "track": self.track,
            "reason": self.reason,
            "next_task_id": self.next_task_id,
            "total_tasks": self.total_tasks,
            "passing_tasks": self.passing_tasks,
            "failed_tasks": self.failed_tasks,
            "blocked_tasks": self.blocked_tasks,
            "gate_verdicts": dict(self.gate_verdicts or {}),
        }


class BlueprintWorkspace:
    """Manages on-disk Blueprint Markdown artifacts and structured state."""

    STATE_FILE = "blueprint-state.json"

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)

    @property
    def spec_path(self) -> Path:
        return self.dir / "spec.md"

    @property
    def lite_path(self) -> Path:
        return self.dir / "lite.md"

    @property
    def design_path(self) -> Path:
        return self.dir / "design.md"

    @property
    def tasks_path(self) -> Path:
        return self.dir / "tasks.md"

    @property
    def traceability_path(self) -> Path:
        return self.dir / "traceability.md"

    @property
    def acceptance_path(self) -> Path:
        return self.dir / "acceptance-cases.md"

    @property
    def state_path(self) -> Path:
        return self.dir / self.STATE_FILE

    def exists(self) -> bool:
        return self.dir.is_dir() and (
            self.state_path.is_file() or self.spec_path.is_file() or self.lite_path.is_file()
        )

    def save(
        self,
        *,
        spec: SpecDocument | None = None,
        design: DesignDocument | None = None,
        plan: TaskPlan | None = None,
        trace: TraceabilityMatrix | None = None,
        acceptance: AcceptanceReport | None = None,
    ) -> dict[str, str]:
        self.dir.mkdir(parents=True, exist_ok=True)
        cur = self._read_raw_state()

        if spec is not None:
            cur["spec"] = spec.to_dict()
            if spec.track == "lite":
                self.lite_path.write_text(spec.to_lite_markdown(), encoding="utf-8")
            else:
                self.spec_path.write_text(spec.to_markdown(), encoding="utf-8")
        if design is not None:
            cur["design"] = design.to_dict()
            self.design_path.write_text(design.to_markdown(), encoding="utf-8")
        if plan is not None:
            cur["plan"] = plan.to_dict()
            self.tasks_path.write_text(plan.to_markdown(), encoding="utf-8")
        if trace is not None:
            cur["trace"] = trace.to_dict()
            self.traceability_path.write_text(trace.to_markdown(), encoding="utf-8")
        if acceptance is not None:
            cur["acceptance"] = acceptance.to_dict()
            self.acceptance_path.write_text(acceptance.to_markdown(), encoding="utf-8")

        self.state_path.write_text(
            json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        written: dict[str, str] = {}
        for key, p in (
            ("spec", self.lite_path if (spec and spec.track == "lite") else self.spec_path),
            ("design", self.design_path),
            ("tasks", self.tasks_path),
            ("traceability", self.traceability_path),
            ("acceptance", self.acceptance_path),
        ):
            if p.is_file():
                written[key] = str(p)
        return written

    def load_spec(self) -> SpecDocument | None:
        raw = self._read_raw_state().get("spec")
        return SpecDocument.from_dict(raw) if isinstance(raw, dict) else None

    def load_design(self) -> DesignDocument | None:
        raw = self._read_raw_state().get("design")
        return DesignDocument.from_dict(raw) if isinstance(raw, dict) else None

    def load_plan(self) -> TaskPlan | None:
        raw = self._read_raw_state().get("plan")
        if not isinstance(raw, dict):
            return None
        plan = TaskPlan.from_dict(raw)
        # Reconcile any manual edits made directly in `tasks.md`
        if self.tasks_path.is_file():
            md = self.tasks_path.read_text(encoding="utf-8", errors="replace")
            md_statuses = parse_markdown_task_statuses(md)
            for t in plan.tasks:
                if t.id in md_statuses:
                    t.status = md_statuses[t.id]
            md_gates = parse_markdown_gate_verdicts(md)
            for phase, verdict in md_gates.items():
                latest = plan.latest_gate(phase)
                if latest is None or latest.passed != (verdict == "PASS"):
                    plan.gate_records.append(
                        PhaseGateRecord(
                            phase=phase,
                            round_no=plan.gate_rounds(phase) + 1,
                            passed=(verdict == "PASS"),
                            forced_by="tasks.md-sync",
                        )
                    )
        return plan

    def load_traceability(self) -> TraceabilityMatrix | None:
        raw = self._read_raw_state().get("trace")
        return TraceabilityMatrix.from_dict(raw) if isinstance(raw, dict) else None

    def _read_raw_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}


# ── Deterministic Phase Detection & Task Selection ───────────────────────────


MAX_GATE_ROUNDS = 3


def detect_phase(workspace: BlueprintWorkspace) -> BlueprintStatusSnapshot:
    """Infer current Blueprint lifecycle phase purely from on-disk artifacts."""
    spec = workspace.load_spec()
    design = workspace.load_design()
    plan = workspace.load_plan()

    has_lite = workspace.lite_path.is_file() or (spec is not None and spec.track == "lite")
    has_spec = workspace.spec_path.is_file() or has_lite or spec is not None
    track = "lite" if has_lite else ("full" if has_spec else "uninitialized")

    if not has_spec or spec is None:
        return BlueprintStatusSnapshot(
            phase=BlueprintPhase.SPEC,
            track=track,
            reason="No specification artifact (`spec.md` / `lite.md`) found; start at Specification phase.",
        )

    if spec.blocking_questions():
        q_ids = ", ".join(q.id for q in spec.blocking_questions())
        return BlueprintStatusSnapshot(
            phase=BlueprintPhase.BLOCKED,
            track=track,
            reason=f"Specification has unresolved blocking Open Questions: {q_ids}",
        )

    gate_map: dict[str, str] = {}
    if plan is not None:
        for g in plan.gate_records:
            gate_map[g.phase] = "PASS" if g.passed else "FAIL"

    # Full track requires Spec Gate -> Design -> Design Gate
    if track == "full":
        spec_gate = plan.latest_gate("spec") if plan else None
        if spec_gate is None or not spec_gate.passed:
            if plan and plan.gate_rounds("spec") >= MAX_GATE_ROUNDS and spec_gate and not spec_gate.passed:
                return BlueprintStatusSnapshot(
                    phase=BlueprintPhase.BLOCKED,
                    track=track,
                    reason=f"Spec Gate failed {plan.gate_rounds('spec')} rounds (>= {MAX_GATE_ROUNDS}); human decision required.",
                    gate_verdicts=gate_map,
                )
            return BlueprintStatusSnapshot(
                phase=BlueprintPhase.SPEC_GATE,
                track=track,
                reason="Specification ready for independent Spec Phase Gate (`G1`–`G9`).",
                gate_verdicts=gate_map,
            )

        if design is None or not workspace.design_path.is_file():
            return BlueprintStatusSnapshot(
                phase=BlueprintPhase.DESIGN,
                track=track,
                reason="Spec Gate passed; `design.md` not yet generated.",
                gate_verdicts=gate_map,
            )

        design_gate = plan.latest_gate("design") if plan else None
        if design_gate is None or not design_gate.passed:
            if plan and plan.gate_rounds("design") >= MAX_GATE_ROUNDS and design_gate and not design_gate.passed:
                return BlueprintStatusSnapshot(
                    phase=BlueprintPhase.BLOCKED,
                    track=track,
                    reason=f"Design Gate failed {plan.gate_rounds('design')} rounds (>= {MAX_GATE_ROUNDS}); human decision required.",
                    gate_verdicts=gate_map,
                )
            return BlueprintStatusSnapshot(
                phase=BlueprintPhase.DESIGN_GATE,
                track=track,
                reason="`design.md` ready for independent Design Phase Gate (`G1`–`G15`).",
                gate_verdicts=gate_map,
            )

    if plan is None or not plan.tasks:
        return BlueprintStatusSnapshot(
            phase=BlueprintPhase.PLAN_GATE,
            track=track,
            reason="`tasks.md` implementation plan not yet populated.",
            gate_verdicts=gate_map,
        )

    plan_gate = plan.latest_gate("plan")
    if plan_gate is None or not plan_gate.passed:
        return BlueprintStatusSnapshot(
            phase=BlueprintPhase.PLAN_GATE,
            track=track,
            reason="`tasks.md` awaits Plan Gate (`P1`–`P6`: DAG, `TC-*` parity, zero-placeholder check).",
            total_tasks=len(plan.tasks),
            gate_verdicts=gate_map,
        )

    total = len(plan.tasks)
    passing = sum(1 for t in plan.tasks if t.status == TaskStatus.PASSING)
    failed = sum(1 for t in plan.tasks if t.status == TaskStatus.FAILED)
    blocked = sum(1 for t in plan.tasks if t.status == TaskStatus.BLOCKED)

    if blocked > 0:
        b_ids = ", ".join(t.id for t in plan.tasks if t.status == TaskStatus.BLOCKED)
        return BlueprintStatusSnapshot(
            phase=BlueprintPhase.BLOCKED,
            track=track,
            reason=f"Blocked task(s) escalated for upstream clarification: {b_ids}",
            total_tasks=total,
            passing_tasks=passing,
            failed_tasks=failed,
            blocked_tasks=blocked,
            gate_verdicts=gate_map,
        )

    if passing < total:
        nxt, why = select_next_ready_task(plan)
        if nxt is None:
            return BlueprintStatusSnapshot(
                phase=BlueprintPhase.BLOCKED,
                track=track,
                reason=why,
                total_tasks=total,
                passing_tasks=passing,
                failed_tasks=failed,
                gate_verdicts=gate_map,
            )
        return BlueprintStatusSnapshot(
            phase=BlueprintPhase.DEVELOP,
            track=track,
            reason=f"Task execution in progress ({passing}/{total} passing); next ready task is `{nxt.id}`.",
            next_task_id=nxt.id,
            total_tasks=total,
            passing_tasks=passing,
            failed_tasks=failed,
            gate_verdicts=gate_map,
        )

    sdc_gate = plan.latest_gate("sdc")
    if not plan.review_passed and (sdc_gate is None or not sdc_gate.passed):
        return BlueprintStatusSnapshot(
            phase=BlueprintPhase.SDC_REVIEW,
            track=track,
            reason=f"All {total} task(s) passing; ready for 3D S/D/C compliance review.",
            total_tasks=total,
            passing_tasks=passing,
            gate_verdicts=gate_map,
        )

    if not plan.acceptance_passed and not workspace.acceptance_path.is_file():
        return BlueprintStatusSnapshot(
            phase=BlueprintPhase.ACCEPTANCE,
            track=track,
            reason="S/D/C review passed; ready for black-box Acceptance (`AT-*`) & clean archive.",
            total_tasks=total,
            passing_tasks=passing,
            gate_verdicts=gate_map,
        )

    return BlueprintStatusSnapshot(
        phase=BlueprintPhase.COMPLETE,
        track=track,
        reason=f"Blueprint complete ({passing}/{total} tasks passing, S/D/C & Acceptance verified).",
        total_tasks=total,
        passing_tasks=passing,
        gate_verdicts=gate_map,
    )


def select_next_ready_task(plan: TaskPlan) -> tuple[TaskSpec | None, str]:
    """Select the single next executable task respecting topological dependencies."""
    in_prog = [t for t in plan.tasks if t.status == TaskStatus.IN_PROGRESS]
    if in_prog:
        return in_prog[0], f"Resuming `in_progress` task {in_prog[0].id}"

    failed = [t for t in plan.tasks if t.status == TaskStatus.FAILED]
    passing_ids = {t.id for t in plan.tasks if t.status == TaskStatus.PASSING}
    if failed:
        for t in failed:
            if all(dep in passing_ids for dep in t.depends_on):
                return t, f"Retrying failed task {t.id}"

    for t in plan.tasks:
        if t.status == TaskStatus.PENDING and all(dep in passing_ids for dep in t.depends_on):
            return t, f"Selected ready pending task {t.id}"

    remaining = [t.id for t in plan.tasks if t.status != TaskStatus.PASSING]
    if not remaining:
        return None, "All tasks passing"
    return None, f"No dependency-satisfied task available among {remaining}"


def apply_rework_reset(
    workspace: BlueprintWorkspace,
    target_phase: str,
    affected_ids: list[str] | None = None,
) -> BlueprintStatusSnapshot:
    """Reset downstream task & gate state cleanly when rework routes upstream."""
    spec = workspace.load_spec()
    design = workspace.load_design()
    plan = workspace.load_plan()
    if plan is None:
        return detect_phase(workspace)

    affected = set(affected_ids or [])
    if target_phase in ("spec", "req"):
        plan.gate_records = [g for g in plan.gate_records if g.phase not in ("spec", "design", "plan", "sdc")]
        plan.review_passed = False
        plan.acceptance_passed = False
        for t in plan.tasks:
            if not affected or affected.intersection(t.covers_reqs) or t.id in affected:
                t.status = TaskStatus.PENDING
                t.blocker_reason = ""
                t.evidence = None
    elif target_phase == "design":
        plan.gate_records = [g for g in plan.gate_records if g.phase not in ("design", "plan", "sdc")]
        plan.review_passed = False
        plan.acceptance_passed = False
        for t in plan.tasks:
            if not affected or affected.intersection(t.covers_reqs) or t.id in affected:
                t.status = TaskStatus.PENDING
                t.blocker_reason = ""
                t.evidence = None
    elif target_phase == "develop":
        plan.gate_records = [g for g in plan.gate_records if g.phase != "sdc"]
        plan.review_passed = False
        plan.acceptance_passed = False
        for t in plan.tasks:
            if not affected or t.id in affected:
                t.status = TaskStatus.PENDING
                t.blocker_reason = ""

    workspace.save(spec=spec, design=design, plan=plan)
    return detect_phase(workspace)


def build_compaction_snapshot(
    workspace: BlueprintWorkspace, max_chars: int = 2400
) -> str:
    """Extract a <35-line domain-aware summary for Context Compaction / Reset (<40% Smart Zone)."""
    if not workspace.exists():
        return ""
    snap = detect_phase(workspace)
    spec = workspace.load_spec()
    design = workspace.load_design()
    plan = workspace.load_plan()

    lines = [
        f"## Blueprint State Snapshot ({spec.loop_id if spec else workspace.dir.name})",
        f"- **Phase**: `{snap.phase.value}` | **Track**: `{snap.track}` | **Progress**: `{snap.passing_tasks}/{snap.total_tasks}` tasks",
        f"- **Reason**: {snap.reason}",
    ]
    if snap.gate_verdicts:
        badges = ", ".join(f"{k}={v}" for k, v in snap.gate_verdicts.items())
        lines.append(f"- **Gates**: {badges}")
    if design is not None:
        lines.append(
            f"- **Architecture Decision**: Option `{design.selected_option}` ({design.decision_source}) — {design.overview[:140]}"
        )
    if plan is not None and plan.tasks:
        lines.append("- **Task Ledger**:")
        for t in plan.tasks[:12]:
            deps = ",".join(t.depends_on) or "-"
            lines.append(f"  - `{t.id}` [{t.status.value}] ({t.granularity.value}, deps:{deps}): {t.title}")
        nxt, _ = select_next_ready_task(plan)
        if nxt is not None:
            lines.extend(
                [
                    f"- **Active/Next Task (`{nxt.id}`)**:",
                    f"  - Files: {', '.join(nxt.all_files())}",
                    f"  - Consumes: {'; '.join(nxt.consumes) or 'none'}",
                    f"  - Produces: {'; '.join(nxt.produces) or 'internal'}",
                    f"  - Cases: {', '.join(nxt.covers_cases)}",
                ]
            )
    lines.append("- **Resume Rule**: Read artifacts in `.neoagent-devops/artifacts/<loop>/blueprint/`; do not rely on chat history.")
    out = "\n".join(lines)
    return out[: max_chars - 16] + "\n...[truncated]" if len(out) > max_chars else out
