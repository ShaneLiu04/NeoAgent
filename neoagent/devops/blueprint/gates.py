"""Independent Read-Only Phase Gates, Lite Qualification & Granularity Splitter.

Enforces deterministic engineering rubrics at every contract boundary BEFORE
errors cascade downstream into code:
  - ``LiteEligibilityGate``: Checks explicit lightweight qualification vs
    6 Hard Escalation Red Lines.
  - ``GranularityAnalyzer``: Detects oversized requirements (`G1`–`G6`) and splits
    compound items into atomic testable units.
  - ``BlueprintPhaseGate``:
      * ``verify_spec``   (`G1`–`G9`)  — EARS clarity, Given/When/Then, vague-word ban,
                                         Change-Type baselines, QAS thresholds, Open Questions.
      * ``verify_design`` (`G1`–`G15`) — Spec coverage, single-sentence module boundaries,
                                         6-part interface contracts, error state guarantees,
                                         `G11`–`G15` test case validity & traceability.
      * ``verify_plan``   (`P1`–`P6`)  — Kahn DAG cycle check, 100% `TC-*` set equality,
                                         Zero-Placeholder scan, `Consumes`/`Produces` coherence.
      * ``verify_sdc``    (`S/D/C`)    — 3D Spec / Design / Component compliance producing
                                         grounded ``Finding`` records for ``ReviewProtocol``.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import Finding, FindingCategory, RiskTier, Severity
from ..review.catalog import resolve_rule
from ..reviewers import is_test_file, parse_diff
from .models import (
    ChangeType,
    DesignDocument,
    GateCheckItem,
    PhaseGateRecord,
    RequirementItem,
    RequirementType,
    SpecDocument,
    TaskPlan,
    TaskStatus,
    TraceabilityMatrix,
)

# Vague, unquantified words forbidden in acceptance criteria & NFR measures unless paired with numbers
_VAGUE_WORDS_RE = re.compile(
    r"\b(?:fast|quickly|soon|asap|good\s+performance|user[- ]friendly|reasonable|sufficiently|seamless)\b"
    r"|足够快|性能好|体验好|尽快|适当|合理即可",
    re.IGNORECASE,
)
_NUMERIC_THRESHOLD_RE = re.compile(
    r"(?:<=|>=|<|>|=|\b\d+(?:\.\d+)?\s*(?:ms|s|sec|mb|kb|gb|%|rps|qps|ops|items|lines|retries|bytes)\b|\bO\([^)]+\)|\b\d+\b)",
    re.IGNORECASE,
)
_EARS_SIGNAL_RE = re.compile(
    r"\b(?:shall|must)\b|必须|应当",
    re.IGNORECASE,
)
_GWT_SIGNAL_RE = re.compile(
    r"(?:\bgiven\b.*\bwhen\b.*\bthen\b)|(?:前置.*触发.*预期)|(?:当.*时.*返回)",
    re.IGNORECASE | re.DOTALL,
)
_PLACEHOLDER_RE = re.compile(
    r"\b(?:TBD|TODO|FIXME|implement\s+later|fill\s+in\s+details|similar\s+to\s+task\s+\w+|same\s+as\s+above)\b"
    r"|同上|见前文|待补充|以后实现",
    re.IGNORECASE,
)
_CRUD_BUNDLE_RE = re.compile(
    r"(?:\bcreate\b.*\bread\b.*\bupdate\b.*\bdelete\b)|(?:增删改查)|(?:创建.*查询.*修改.*删除)",
    re.IGNORECASE,
)
_MULTI_RESPONSIBILITY_RE = re.compile(
    r"(?:\band\s+also\b|\bas\s+well\s+as\b|，并且负责|以及负责)",
    re.IGNORECASE,
)
_CONTRACT_BREAK_KEYWORDS_RE = re.compile(
    r"\b(?:public\s+api|breaking|protocol|schema|migration|database|ddl|auth|crypto|concurrency)\b"
    r"|公开接口|协议变更|数据库迁移|鉴权|架构调整",
    re.IGNORECASE,
)


# ── 1. Lite Track Qualification & Hard Upgrade Red Lines ─────────────────────


@dataclass(frozen=True)
class LiteQualification:
    eligible: bool
    requested_lite: bool
    red_lines_triggered: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "requested_lite": self.requested_lite,
            "red_lines_triggered": list(self.red_lines_triggered),
            "reason": self.reason,
        }


class LiteEligibilityGate:
    """Evaluates whether an explicit lightweight request passes all 6 Upgrade Red Lines."""

    @staticmethod
    def evaluate(
        *,
        requested_lite: bool,
        title: str,
        files: list[str],
        changed_lines: int = 0,
        tier: RiskTier = RiskTier.LOW,
        has_public_contract_change: bool = False,
        has_blocking_ambiguity: bool = False,
    ) -> LiteQualification:
        if not requested_lite:
            return LiteQualification(
                eligible=False,
                requested_lite=False,
                reason="Default workflow uses full Spec-to-Code Blueprint unless Lite is explicitly requested",
            )

        red_lines: list[str] = []
        if tier != RiskTier.LOW:
            red_lines.append(f"Risk tier is '{tier.value}' (security/payment/infra/API risk requires full Blueprint)")
        if len(files) > 3 or changed_lines > 80:
            red_lines.append(
                f"Blast radius ({len(files)} files, {changed_lines} lines) exceeds Lite ceiling (<=3 files, <=80 lines)"
            )
        if has_public_contract_change or _CONTRACT_BREAK_KEYWORDS_RE.search(title or ""):
            red_lines.append("Touches public API, schema, protocol, or security/concurrency boundary")
        if has_blocking_ambiguity:
            red_lines.append("Contains unresolved blocking requirement ambiguity")

        top_dirs = {Path(f).parts[0] for f in files if Path(f).parts and not is_test_file(f)}
        if len(top_dirs) > 1:
            red_lines.append(f"Cross-module impact across {sorted(top_dirs)} requires full Design contract")

        if red_lines:
            return LiteQualification(
                eligible=False,
                requested_lite=True,
                red_lines_triggered=red_lines,
                reason="Escalated to full Blueprint: " + "; ".join(red_lines),
            )
        return LiteQualification(
            eligible=True,
            requested_lite=True,
            reason="Explicit low-risk, single-module change qualifies for Lite specification (`lite.md` + `tasks.md`)",
        )


# ── 2. Oversized Requirement Granularity Detector (`G1`–`G6`) ────────────────


@dataclass(frozen=True)
class GranularitySignal:
    code: str  # G1..G6
    req_id: str
    description: str
    split_advice: str


class GranularityAnalyzer:
    """Detects compound/oversized requirement items before they cause test explosion."""

    @staticmethod
    def inspect(req: RequirementItem) -> list[GranularitySignal]:
        signals: list[GranularitySignal] = []
        txt = f"{req.title} {req.statement}"
        if _CRUD_BUNDLE_RE.search(txt):
            signals.append(
                GranularitySignal(
                    "G2-crud-bundle",
                    req.id,
                    f"{req.id} bundles Create/Read/Update/Delete into one requirement",
                    "Split into independent behavioral requirements (create, query, mutate, delete) with distinct acceptance criteria.",
                )
            )
        if len(req.acceptance) >= 5:
            signals.append(
                GranularitySignal(
                    "G3-scenario-explosion",
                    req.id,
                    f"{req.id} requires {len(req.acceptance)} acceptance scenarios (>=5)",
                    "Split primary behavior from secondary state/mode branches.",
                )
            )
        if re.search(r"\b(?:immediately|synchronously)\b.*\b(?:later|asynchronously|background|cron)\b|同步.*异步|立即.*后台", txt, re.I):
            signals.append(
                GranularitySignal(
                    "G6-temporal-coupling",
                    req.id,
                    f"{req.id} couples immediate synchronous response with asynchronous background processing",
                    "Split immediate acknowledgment semantics from background processing guarantees.",
                )
            )
        return signals

    @staticmethod
    def split_crud_requirement(req: RequirementItem) -> list[RequirementItem]:
        """Mechanically split a bundled CRUD requirement into atomic EARS items preserving `source_ref`."""
        if not _CRUD_BUNDLE_RE.search(f"{req.title} {req.statement}"):
            return [req]
        verbs = [
            ("Create", "create and validate a new resource record"),
            ("Query", "retrieve matching resource records deterministically"),
            ("Update", "modify an existing resource while preserving invariants"),
            ("Delete", "remove a target resource and reject subsequent lookups"),
        ]
        children: list[RequirementItem] = []
        for idx, (label, phrase) in enumerate(verbs, start=1):
            sub_id = f"{req.id}.{idx}"
            children.append(
                RequirementItem(
                    id=sub_id,
                    title=f"{req.title} — {label}",
                    statement=f"When a {label.lower()} request is received, the module shall {phrase}.",
                    req_type=req.req_type,
                    ears_pattern=req.ears_pattern,
                    acceptance=[
                        f"Given valid {label.lower()} input, When {label.lower()} is invoked, Then the expected outcome is persisted and returned.",
                        f"Given invalid {label.lower()} input, When {label.lower()} is invoked, Then a typed validation error is raised without corrupting state.",
                    ],
                    priority=req.priority,
                    change_type=req.change_type,
                    existing_baseline=req.existing_baseline,
                    source_ref=req.source_ref,
                )
            )
        return children


# ── 3. Independent Phase Gates (`spec` G1-G9, `design` G1-G15, `plan` P1-P6, `sdc`) ──


class BlueprintPhaseGate:
    """Stateless, read-only deliverable verifier across Blueprint lifecycle phases."""

    @classmethod
    def verify_spec(cls, spec: SpecDocument, round_no: int = 1) -> PhaseGateRecord:
        items: list[GateCheckItem] = []
        core = spec.core_requirements()
        frs = [r for r in core if r.req_type in (RequirementType.FR, RequirementType.IFR)]
        nfrs = [r for r in core if r.req_type == RequirementType.NFR]

        # G1: Complete sections & non-empty requirements
        has_sections = bool(spec.background.strip()) and bool(spec.in_scope) and len(frs) > 0
        items.append(
            GateCheckItem(
                "G1",
                "Specification structure & functional items present",
                "YES" if has_sections else "NO",
                f"background={bool(spec.background.strip())}, in_scope={len(spec.in_scope)}, FR/IFR={len(frs)}",
                severity="critical",
                fix_hint="Populate background, in_scope, and at least one FR/IFR requirement.",
            )
        )

        # G2: Every FR/IFR has Given/When/Then acceptance criteria
        missing_acc = [r.id for r in frs if not r.acceptance or not all(_GWT_SIGNAL_RE.search(a) for a in r.acceptance)]
        items.append(
            GateCheckItem(
                "G2",
                "Every FR/IFR carries Given/When/Then acceptance criteria",
                "YES" if not missing_acc else "NO",
                "All FR/IFR items have Given/When/Then criteria" if not missing_acc else f"Missing/malformed on {missing_acc}",
                severity="critical",
                fix_hint="Write testable `Given ..., When ..., Then ...` criteria for every FR/IFR.",
            )
        )

        # G3: EARS statement discipline & zero unquantified vague terms
        non_ears = [r.id for r in frs if not _EARS_SIGNAL_RE.search(r.statement)]
        vague_hits: list[str] = []
        for r in core:
            for acc in r.acceptance:
                if _VAGUE_WORDS_RE.search(acc) and not _NUMERIC_THRESHOLD_RE.search(acc):
                    vague_hits.append(r.id)
                    break
        g3_ok = not non_ears and not vague_hits
        items.append(
            GateCheckItem(
                "G3",
                "EARS statement clarity & zero unquantified vague criteria",
                "YES" if g3_ok else "NO",
                "Clean EARS & quantifiable criteria" if g3_ok else f"non_ears={non_ears}, vague={vague_hits}",
                severity="major",
                fix_hint="Use explicit `<subject> shall ...` EARS phrasing and replace vague adjectives with numeric thresholds.",
            )
        )

        # G4: Explicit scope & exclusions
        g4_ok = bool(spec.in_scope) and bool(spec.out_of_scope)
        items.append(
            GateCheckItem(
                "G4",
                "Explicit In-Scope and Out-of-Scope boundaries",
                "YES" if g4_ok else "NO",
                f"in_scope={len(spec.in_scope)}, out_of_scope={len(spec.out_of_scope)}",
                severity="major",
                fix_hint="Declare both In-Scope targets and explicit Out-of-Scope exclusions.",
            )
        )

        # G5: Modify/Remove requirements declare Existing Baseline
        missing_base = [
            r.id
            for r in core
            if r.change_type in (ChangeType.MODIFY, ChangeType.REMOVE)
            and (not r.existing_baseline.strip() or r.existing_baseline.strip().upper() == "N/A")
        ]
        items.append(
            GateCheckItem(
                "G5",
                "Modify/Remove items document Existing Baseline",
                "YES" if not missing_base else "NO",
                "All modify/remove baselines documented" if not missing_base else f"Missing baseline on {missing_base}",
                severity="critical",
                fix_hint="Document `existing_baseline` whenever `change_type` is `modify` or `remove`.",
            )
        )

        # G6: Requirement granularity (no CRUD bundling or scenario explosion)
        gran_signals = [sig for r in frs for sig in GranularityAnalyzer.inspect(r)]
        items.append(
            GateCheckItem(
                "G6",
                "Atomic requirement granularity (G1-G6 split check)",
                "YES" if not gran_signals else "NO",
                "All requirements atomic" if not gran_signals else "; ".join(f"{s.req_id}:{s.code}" for s in gran_signals),
                severity="major",
                fix_hint="Split oversized/bundled requirements into atomic items with independent acceptance criteria.",
            )
        )

        # G7: Positive + failure/boundary acceptance on FRs
        single_path = [r.id for r in frs if len(r.acceptance) < 2 and spec.track == "full"]
        items.append(
            GateCheckItem(
                "G7",
                "FR acceptance covers both happy path and error/boundary path",
                "YES" if not single_path else "WARN",
                "Happy + negative/boundary paths present" if not single_path else f"Single scenario on {single_path}",
                severity="minor",
                fix_hint="Add at least one error/boundary Given/When/Then criterion per FR.",
            )
        )

        # G8: NFRs expressed as quantifiable QAS scenarios
        bad_nfr = [
            r.id
            for r in nfrs
            if r.qas is None or not _NUMERIC_THRESHOLD_RE.search(r.qas.measure) or _VAGUE_WORDS_RE.search(r.qas.measure)
        ]
        items.append(
            GateCheckItem(
                "G8",
                "Non-functional requirements carry quantifiable QAS thresholds",
                "YES" if not bad_nfr else "NO",
                f"Verified {len(nfrs)} NFR item(s)" if not bad_nfr else f"Unquantified NFR QAS on {bad_nfr}",
                severity="major",
                fix_hint="Provide a 5-part QAS with a concrete numeric `measure` threshold for every NFR.",
            )
        )

        # G9: Zero unresolved blocking Open Questions
        blocking_q = [q.id for q in spec.blocking_questions()]
        items.append(
            GateCheckItem(
                "G9",
                "Zero unresolved blocking Open Questions",
                "YES" if not blocking_q else "NO",
                "No blocking ambiguities open" if not blocking_q else f"Unresolved blocking questions: {blocking_q}",
                severity="critical",
                fix_hint="Answer all `blocking` Open Questions with the owner before advancing to Design.",
            )
        )

        passed = all(i.verdict != "NO" for i in items)
        return PhaseGateRecord(phase="spec", round_no=round_no, passed=passed, items=items)

    @classmethod
    def verify_design(
        cls, spec: SpecDocument, design: DesignDocument, round_no: int = 1
    ) -> PhaseGateRecord:
        items: list[GateCheckItem] = []
        req_ids = {r.id for r in spec.core_requirements()}
        covered_by_tc = {tc.covers_req for tc in design.test_cases}

        # G1: Core design sections populated
        g1_ok = bool(design.overview.strip()) and bool(design.module_responsibilities) and bool(design.interfaces) and bool(design.test_cases)
        items.append(
            GateCheckItem(
                "G1",
                "Complete design structure (overview, modules, interfaces, test cases)",
                "YES" if g1_ok else "NO",
                f"modules={len(design.module_responsibilities)}, interfaces={len(design.interfaces)}, test_cases={len(design.test_cases)}",
                severity="critical",
                fix_hint="Populate overview, module responsibilities, interface contracts, and `TC-*` test design table.",
            )
        )

        # G2: 100% coverage of spec core requirements
        uncovered = sorted(req_ids - covered_by_tc)
        items.append(
            GateCheckItem(
                "G2",
                "Design & test table cover 100% of specification requirements",
                "YES" if not uncovered else "NO",
                f"Covered {len(req_ids - set(uncovered))}/{len(req_ids)} requirements"
                if not uncovered
                else f"Uncovered requirements: {uncovered}",
                severity="critical",
                fix_hint="Ensure every requirement in `spec.md` maps to design elements and `TC-*` cases.",
            )
        )

        # G3: 6-part observable interface contracts complete
        incomplete_ifaces = [
            i.name
            for i in design.interfaces
            if not (i.signature.strip() and i.inputs_preconditions.strip() and i.outputs_postconditions.strip() and i.error_semantics.strip())
        ]
        items.append(
            GateCheckItem(
                "G3",
                "6-part observable interface contracts complete",
                "YES" if not incomplete_ifaces else "NO",
                f"Verified {len(design.interfaces)} interface contract(s)" if not incomplete_ifaces else f"Incomplete contracts: {incomplete_ifaces}",
                severity="critical",
                fix_hint="Fill signature, preconditions, postconditions, and error semantics on every interface.",
            )
        )

        # G4 & G5: Error model with explicit failure-path state guarantees
        bad_err = [
            e.trigger
            for e in design.error_model
            if not e.handling.strip() or not e.state_guarantee.strip() or "undefined" in e.state_guarantee.lower()
        ]
        g5_ok = len(design.error_model) > 0 and not bad_err
        items.append(
            GateCheckItem(
                "G5",
                "Error model defines handling and failure-path state guarantees",
                "YES" if g5_ok else "NO",
                f"error_entries={len(design.error_model)}" if g5_ok else f"Missing/undefined error guarantees: {bad_err or 'empty error_model'}",
                severity="major",
                fix_hint="Specify trigger, boundary handling, and explicit failure-path state guarantee (e.g. state unchanged / atomic rollback).",
            )
        )

        # G7: Architectural option trade-off analysis & rationale
        opt_ids = {o.id for o in design.options}
        g7_ok = len(design.options) >= 1 and design.selected_option in opt_ids and any(o.rationale.strip() for o in design.options)
        items.append(
            GateCheckItem(
                "G7",
                "Design options analyzed with explicit selection rationale",
                "YES" if g7_ok else "NO",
                f"options={len(design.options)}, selected={design.selected_option}, source={design.decision_source}",
                severity="major",
                fix_hint="Document architectural option trade-offs and explicit rationale for the selected option.",
            )
        )

        # G9: Single-sentence module responsibility cohesion check
        tangled_mods = [
            mod for mod, desc in design.module_responsibilities.items() if _MULTI_RESPONSIBILITY_RE.search(desc)
        ]
        items.append(
            GateCheckItem(
                "G9",
                "Single-responsibility module boundaries",
                "YES" if not tangled_mods else "WARN",
                "All modules state one cohesive responsibility" if not tangled_mods else f"Multi-responsibility phrasing in {tangled_mods}",
                severity="minor",
                fix_hint="Split modules whose responsibility description combines unrelated concerns.",
            )
        )

        # G11: Test case validity (observable expected outcome, unique Case IDs)
        tc_ids = [tc.id for tc in design.test_cases]
        dup_tcs = sorted({x for x in tc_ids if tc_ids.count(x) > 1})
        invalid_tcs = [
            tc.id
            for tc in design.test_cases
            if not tc.expected.strip() or not tc.scenario_gwt.strip() or _VAGUE_WORDS_RE.search(tc.expected)
        ]
        g11_ok = not dup_tcs and not invalid_tcs
        items.append(
            GateCheckItem(
                "G11",
                "Test cases specify observable, assertable outcomes",
                "YES" if g11_ok else "NO",
                f"Validated {len(tc_ids)} test case(s)" if g11_ok else f"duplicates={dup_tcs}, invalid={invalid_tcs}",
                severity="critical",
                fix_hint="Give every `TC-*` a unique ID, concrete scenario, and assertable expected outcome.",
            )
        )

        # G13: Boundary / error test path coverage
        categories = {tc.category for tc in design.test_cases}
        has_neg = bool(categories & {"boundary", "error"})
        items.append(
            GateCheckItem(
                "G13",
                "Test design covers boundary and error/failure scenarios",
                "YES" if has_neg else "NO",
                f"categories={sorted(categories)}",
                severity="major",
                fix_hint="Include `boundary` and/or `error` test cases in `design.md` §7.",
            )
        )

        # G14: Modify/Remove requirements carry regression test cases
        mod_reqs = {r.id for r in spec.core_requirements() if r.change_type in (ChangeType.MODIFY, ChangeType.REMOVE)}
        reg_covered = {tc.covers_req for tc in design.test_cases if tc.category == "regression"}
        missing_reg = sorted(mod_reqs - reg_covered)
        items.append(
            GateCheckItem(
                "G14",
                "Modify/Remove requirements include regression test cases",
                "YES" if not missing_reg else "NO",
                "Regression coverage complete" if not missing_reg else f"Missing `regression` TC for {missing_reg}",
                severity="major",
                fix_hint="Add a `regression` test case for every `modify` or `remove` requirement.",
            )
        )

        passed = all(i.verdict != "NO" for i in items)
        return PhaseGateRecord(phase="design", round_no=round_no, passed=passed, items=items)

    @classmethod
    def verify_plan(
        cls,
        spec: SpecDocument,
        design: DesignDocument | None,
        plan: TaskPlan,
        round_no: int = 1,
    ) -> PhaseGateRecord:
        items: list[GateCheckItem] = []
        task_ids = {t.id for t in plan.tasks}

        # P1: Non-empty task list & valid topological DAG (zero dependency cycles)
        cycle_err = cls._detect_dag_issues(plan)
        items.append(
            GateCheckItem(
                "P1",
                "Task dependency graph is a valid acyclic DAG",
                "YES" if (plan.tasks and not cycle_err) else "NO",
                f"{len(plan.tasks)} task(s) in valid topological order" if (plan.tasks and not cycle_err) else (cycle_err or "Plan contains zero tasks"),
                severity="critical",
                fix_hint="Ensure tasks exist, reference valid dependency IDs, and contain no dependency cycles.",
            )
        )

        # P2: 100% Test Case set equality (`plan` TC set == `design` TC set) when design exists
        if design is not None:
            design_tcs = {tc.id for tc in design.test_cases}
            plan_tcs = {c for t in plan.tasks for c in t.covers_cases}
            missing_tcs = sorted(design_tcs - plan_tcs)
            extra_tcs = sorted(plan_tcs - design_tcs)
            p2_ok = not missing_tcs and not extra_tcs
            items.append(
                GateCheckItem(
                    "P2",
                    "Task Case IDs match `design.md` canonical `TC-*` set exactly",
                    "YES" if p2_ok else "NO",
                    f"Matched {len(design_tcs)} `TC-*` case(s)" if p2_ok else f"missing={missing_tcs}, ungrounded_extra={extra_tcs}",
                    severity="critical",
                    fix_hint="Align task `covers_cases` 1-to-1 with `design.md` §7 `TC-*` table.",
                )
            )

        # P3: 100% core requirement coverage across tasks
        req_ids = {r.id for r in spec.core_requirements()}
        covered_reqs = {r for t in plan.tasks for r in t.covers_reqs}
        uncovered_reqs = sorted(req_ids - covered_reqs)
        items.append(
            GateCheckItem(
                "P3",
                "Every specification requirement is assigned to >=1 task",
                "YES" if not uncovered_reqs else "NO",
                f"All {len(req_ids)} requirement(s) mapped" if not uncovered_reqs else f"Unmapped requirements: {uncovered_reqs}",
                severity="critical",
                fix_hint="Map every core requirement ID (`FR-*`/`NFR-*`/`IFR-*`) to at least one task.",
            )
        )

        # P4: Self-contained file & test paths on every task
        missing_paths = [
            t.id for t in plan.tasks if not (t.files_create or t.files_modify) or not t.files_test or not t.definition_of_done.strip()
        ]
        items.append(
            GateCheckItem(
                "P4",
                "Every task declares exact source paths, test paths, and Definition of Done",
                "YES" if not missing_paths else "NO",
                "All tasks self-contained" if not missing_paths else f"Incomplete paths/DoD on {missing_paths}",
                severity="major",
                fix_hint="Specify `files_create`/`files_modify`, `files_test`, and `definition_of_done` inline for every task.",
            )
        )

        # P5: Zero-Placeholder scan (forbids "TODO", "TBD", "similar to Task 1", "同上")
        placeholder_tasks: list[str] = []
        for t in plan.tasks:
            blob = " ".join([t.title, t.definition_of_done, *t.steps, *t.consumes, *t.produces])
            if _PLACEHOLDER_RE.search(blob):
                placeholder_tasks.append(t.id)
        items.append(
            GateCheckItem(
                "P5",
                "Zero placeholders (`TODO`/`TBD`/`similar to T1`/`同上`) in task specs",
                "YES" if not placeholder_tasks else "NO",
                "Zero placeholders detected" if not placeholder_tasks else f"Placeholders found in {placeholder_tasks}",
                severity="critical",
                fix_hint="Inline complete context in every task so a fresh subagent never needs chat history.",
            )
        )

        # P6: At most one task currently `in_progress`
        in_prog = [t.id for t in plan.tasks if t.status == TaskStatus.IN_PROGRESS]
        items.append(
            GateCheckItem(
                "P6",
                "Single active task invariant (<=1 `in_progress`)",
                "YES" if len(in_prog) <= 1 and len(task_ids) == len(plan.tasks) else "NO",
                f"in_progress={in_prog}",
                severity="major",
                fix_hint="Keep at most one task `in_progress` at a time and ensure unique task IDs.",
            )
        )

        passed = all(i.verdict != "NO" for i in items)
        return PhaseGateRecord(phase="plan", round_no=round_no, passed=passed, items=items)

    @staticmethod
    def _detect_dag_issues(plan: TaskPlan) -> str:
        by_id = {t.id: t for t in plan.tasks}
        if len(by_id) != len(plan.tasks):
            return "Duplicate task IDs in plan"
        indegree: dict[str, int] = {tid: 0 for tid in by_id}
        adj: dict[str, list[str]] = {tid: [] for tid in by_id}
        for t in plan.tasks:
            for dep in t.depends_on:
                if dep not in by_id:
                    return f"Task {t.id} depends on unknown task '{dep}'"
                adj[dep].append(t.id)
                indegree[t.id] += 1
        q = deque(tid for tid, deg in indegree.items() if deg == 0)
        visited = 0
        while q:
            cur = q.popleft()
            visited += 1
            for nxt in adj[cur]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    q.append(nxt)
        if visited != len(by_id):
            return "Cyclic dependency detected in task graph"
        return ""

    @classmethod
    def verify_sdc(
        cls,
        spec: SpecDocument,
        design: DesignDocument | None,
        plan: TaskPlan,
        trace: TraceabilityMatrix | None,
        diff_text: str = "",
        repo_root: Path | None = None,
        round_no: int = 1,
    ) -> tuple[PhaseGateRecord, list[Finding]]:
        """3D Spec / Design / Component compliance audit + ReviewProtocol findings."""
        items: list[GateCheckItem] = []
        findings: list[Finding] = []

        # S1: Every core requirement has >=1 passing task with TDD evidence
        passing_reqs = {
            r
            for t in plan.tasks
            if t.status == TaskStatus.PASSING and t.evidence is not None
            for r in t.covers_reqs
        }
        untested_reqs = [r.id for r in spec.core_requirements() if r.id not in passing_reqs]
        items.append(
            GateCheckItem(
                "S1",
                "All specification requirements backed by passing TDD tasks",
                "YES" if not untested_reqs else "NO",
                "100% requirement task verification" if not untested_reqs else f"Unverified requirements: {untested_reqs}",
                severity="critical",
                fix_hint="Complete RED->GREEN->REFACTOR tasks covering every requirement.",
            )
        )
        for req_id in untested_reqs:
            findings.append(
                cls._sdc_finding(
                    "sdc-untested-requirement",
                    f"Requirement `{req_id}` has no passing TDD task with recorded verification evidence",
                    Severity.MAJOR,
                )
            )

        # S5: Traceability matrix completeness
        unlinked_trace: list[str] = []
        if trace is not None:
            for row in trace.rows:
                if not row.tasks or not (row.code_files or row.test_files):
                    unlinked_trace.append(row.req_id)
        items.append(
            GateCheckItem(
                "S5",
                "Bidirectional traceability matrix links Req -> Task -> Code/Test",
                "YES" if not unlinked_trace else "NO",
                "Traceability links complete" if not unlinked_trace else f"Incomplete traceability rows: {unlinked_trace}",
                severity="major",
                fix_hint="Update `traceability.md` with task, source, and test links.",
            )
        )
        for req_id in unlinked_trace:
            findings.append(
                cls._sdc_finding(
                    "sdc-incomplete-traceability",
                    f"Traceability row for `{req_id}` is missing task or source/test file links",
                    Severity.MINOR,
                )
            )

        # D1: Planned interface symbols exist in target modules (when repo_root available)
        missing_ifaces: list[str] = []
        if design is not None and repo_root is not None:
            root = repo_root.resolve()
            for iface in design.interfaces:
                if not iface.module:
                    continue
                mod_path = (root / iface.module).resolve()
                if not mod_path.is_file() or not mod_path.is_relative_to(root):
                    missing_ifaces.append(f"{iface.module}:{iface.name}")
                    continue
                txt = mod_path.read_text(encoding="utf-8", errors="replace")
                if iface.name not in txt:
                    missing_ifaces.append(f"{iface.module}:{iface.name}")
                    findings.append(
                        cls._sdc_finding(
                            "sdc-missing-interface",
                            f"Planned interface symbol `{iface.name}` from `design.md` not found in `{iface.module}`",
                            Severity.MAJOR,
                            file=iface.module,
                            line=1,
                        )
                    )
        items.append(
            GateCheckItem(
                "D1",
                "Implemented public symbols match `design.md` interface contracts",
                "YES" if not missing_ifaces else "NO",
                "All planned interface symbols present" if not missing_ifaces else f"Missing symbols: {missing_ifaces}",
                severity="critical",
                fix_hint="Implement the exact symbol names/signatures approved in `design.md` (or revise `design.md` first).",
            )
        )

        # D3: Changed production files stay within declared task/design file plan
        declared_files = {p.replace("\\", "/") for t in plan.tasks for p in t.all_files()}
        if design is not None:
            declared_files.update(m.replace("\\", "/") for m in design.module_responsibilities)
        undeclared_touches: list[str] = []
        if diff_text and declared_files:
            for dl in parse_diff(diff_text):
                if not dl.added or not dl.file:
                    continue
                norm = dl.file.replace("\\", "/")
                if norm.startswith((".neoagent-devops/", "docs/specs/", "docs/changes/")) or is_test_file(norm):
                    continue
                if not any(norm == d or norm.startswith(d.rstrip("/") + "/") for d in declared_files):
                    if norm not in undeclared_touches:
                        undeclared_touches.append(norm)
                        findings.append(
                            cls._sdc_finding(
                                "sdc-undeclared-file-touch",
                                f"File `{norm}` modified outside approved `design.md` / `tasks.md` file plan",
                                Severity.MAJOR,
                                file=norm,
                                line=dl.line,
                                evidence=dl.text.strip()[:160],
                            )
                        )
        items.append(
            GateCheckItem(
                "D3",
                "Modified source files match declared design/task blast radius",
                "YES" if not undeclared_touches else "NO",
                "All changed source files within plan" if not undeclared_touches else f"Undeclared files: {undeclared_touches}",
                severity="major",
                fix_hint="Revert out-of-plan edits or update `design.md` & `tasks.md` with owner approval.",
            )
        )

        # C1: All tasks passing
        non_passing = [t.id for t in plan.tasks if t.status != TaskStatus.PASSING]
        items.append(
            GateCheckItem(
                "C1",
                "All planned tasks are in `passing` state",
                "YES" if not non_passing else "NO",
                f"{len(plan.tasks)}/{len(plan.tasks)} tasks passing" if not non_passing else f"Non-passing tasks: {non_passing}",
                severity="critical",
                fix_hint="Finish remaining tasks via `TaskTDDController` before final S/D/C sign-off.",
            )
        )

        passed = all(i.verdict != "NO" for i in items)
        return PhaseGateRecord(phase="sdc", round_no=round_no, passed=passed, items=items), findings

    @staticmethod
    def _sdc_finding(
        rule: str,
        message: str,
        severity: Severity,
        *,
        file: str = "",
        line: int = 0,
        evidence: str = "",
    ) -> Finding:
        spec = resolve_rule(rule)
        return Finding(
            rule=rule,
            message=message,
            severity=severity,
            file=file,
            line=line,
            source="blueprint-sdc",
            evidence=evidence,
            standard=spec.standard if spec else "Blueprint S/D/C Compliance",
            rework=spec.rework if spec else "build",
            category=FindingCategory(spec.triage if spec else "llm_fixable"),
        )
