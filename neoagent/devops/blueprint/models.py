"""Domain models & Markdown/JSON codecs for the Spec-to-Code Blueprint subsystem.

Artifacts serve as the single source of truth across sessions, subagents, and
context resets:
  - ``spec.md``             — WHAT: EARS requirements, Given/When/Then criteria,
                              Change Type baselines, QAS scenarios, Open Questions.
  - ``design.md``           — HOW: 2-3 trade-off options, single-sentence module
                              responsibilities, 6-part interface contracts, error
                              model, and canonical ``TC-*`` test design table.
  - ``tasks.md``            — EXECUTION DAG: ``micro`` vs ``standard`` tasks,
                              ``Consumes``/``Produces`` signatures, RED/GREEN/REFACTOR
                              evidence, 5D ``clean_code_check``, and gate records.
  - ``traceability.md``     — Bidirectional matrix: Req -> Design -> TC -> Task ->
                              Code -> Test -> Verification Evidence.
  - ``acceptance-cases.md`` — Black-box acceptance verification & Go/No-Go report.
"""

from __future__ import annotations

import enum
import re
import time
from dataclasses import dataclass, field
from typing import Any


class RequirementType(str, enum.Enum):
    FR = "FR"    # Functional requirement: observable behavior
    NFR = "NFR"  # Non-functional quality attribute (requires QAS threshold)
    IFR = "IFR"  # External/cross-module interface contract requirement
    CON = "CON"  # Hard platform/compatibility/dependency constraint
    ASM = "ASM"  # Explicit assumption
    EXC = "EXC"  # Explicit out-of-scope exclusion


class EARSPattern(str, enum.Enum):
    UBIQUITOUS = "ubiquitous"              # The <subject> shall <behavior>
    EVENT_DRIVEN = "event_driven"          # When <trigger>, the <subject> shall <outcome>
    STATE_DRIVEN = "state_driven"          # While <state>, the <subject> shall <outcome>
    UNWANTED_BEHAVIOR = "unwanted"         # If <error>, then the <subject> shall <recovery>
    OPTIONAL_FEATURE = "optional"          # Where <feature>, the <subject> shall <outcome>


class ChangeType(str, enum.Enum):
    NEW = "new"        # Pure addition; existing observable semantics untouched
    MODIFY = "modify"  # Alters existing behavior/error/threshold (requires baseline)
    REMOVE = "remove"  # Deprecates/removes existing capability (requires baseline)


class TaskGranularity(str, enum.Enum):
    MICRO = "micro"        # <30 lines, single file, no cross-module dep -> inline TDD
    STANDARD = "standard"  # Multi-file or cross-module -> Red/Green subagent isolation


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PASSING = "passing"
    FAILED = "failed"
    BLOCKED = "blocked"


class ImplementerStatus(str, enum.Enum):
    DONE = "done"
    DONE_WITH_CONCERNS = "done_with_concerns"
    NEEDS_CONTEXT = "needs_context"
    BLOCKED = "blocked"


@dataclass
class QualityScenario:
    """ATAM 5-part Quality Attribute Scenario (QAS) for quantifiable NFRs."""

    source: str
    stimulus: str
    environment: str
    response: str
    measure: str  # Must contain concrete numeric threshold or verifiable criterion

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "stimulus": self.stimulus,
            "environment": self.environment,
            "response": self.response,
            "measure": self.measure,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> QualityScenario:
        return cls(
            source=str(d.get("source", "")),
            stimulus=str(d.get("stimulus", "")),
            environment=str(d.get("environment", "")),
            response=str(d.get("response", "")),
            measure=str(d.get("measure", "")),
        )


@dataclass
class OpenQuestion:
    """Explicit ambiguity surfaced during specification instead of LLM guessing."""

    id: str
    question: str
    blocking: bool = True
    owner: str = "product-owner"
    options: list[str] = field(default_factory=list)
    answer: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.answer.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "blocking": self.blocking,
            "owner": self.owner,
            "options": list(self.options),
            "answer": self.answer,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OpenQuestion:
        return cls(
            id=str(d["id"]),
            question=str(d["question"]),
            blocking=bool(d.get("blocking", True)),
            owner=str(d.get("owner", "product-owner")),
            options=list(d.get("options") or []),
            answer=str(d.get("answer", "")),
        )


@dataclass
class RequirementItem:
    id: str
    title: str
    statement: str  # EARS sentence with explicit subject & observable action
    req_type: RequirementType = RequirementType.FR
    ears_pattern: EARSPattern = EARSPattern.EVENT_DRIVEN
    acceptance: list[str] = field(default_factory=list)  # Given ... When ... Then ...
    priority: str = "Must"
    change_type: ChangeType = ChangeType.NEW
    existing_baseline: str = "N/A"
    qas: QualityScenario | None = None
    source_ref: str = "user-request"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "statement": self.statement,
            "req_type": self.req_type.value,
            "ears_pattern": self.ears_pattern.value,
            "acceptance": list(self.acceptance),
            "priority": self.priority,
            "change_type": self.change_type.value,
            "existing_baseline": self.existing_baseline,
            "qas": self.qas.to_dict() if self.qas else None,
            "source_ref": self.source_ref,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RequirementItem:
        qas_raw = d.get("qas")
        return cls(
            id=str(d["id"]),
            title=str(d.get("title", d["id"])),
            statement=str(d["statement"]),
            req_type=RequirementType(str(d.get("req_type", "FR"))),
            ears_pattern=EARSPattern(str(d.get("ears_pattern", "event_driven"))),
            acceptance=list(d.get("acceptance") or []),
            priority=str(d.get("priority", "Must")),
            change_type=ChangeType(str(d.get("change_type", "new"))),
            existing_baseline=str(d.get("existing_baseline", "N/A")),
            qas=QualityScenario.from_dict(qas_raw) if isinstance(qas_raw, dict) else None,
            source_ref=str(d.get("source_ref", "user-request")),
        )


@dataclass
class SpecDocument:
    loop_id: str
    title: str
    track: str = "full"  # full | lite
    background: str = ""
    in_scope: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    requirements: list[RequirementItem] = field(default_factory=list)
    open_questions: list[OpenQuestion] = field(default_factory=list)
    approved_by: str = ""
    created_at: float = field(default_factory=time.time)

    def blocking_questions(self) -> list[OpenQuestion]:
        return [q for q in self.open_questions if q.blocking and not q.resolved]

    def core_requirements(self) -> list[RequirementItem]:
        return [
            r
            for r in self.requirements
            if r.req_type in (RequirementType.FR, RequirementType.NFR, RequirementType.IFR, RequirementType.CON)
        ]

    def to_markdown(self) -> str:
        lines = [
            f"# Specification — {self.loop_id}: {self.title}",
            "",
            "| Field | Value |",
            "|---|---|",
            f"| Loop ID | `{self.loop_id}` |",
            f"| Track | `{self.track}` |",
            f"| Status | `{'Approved by ' + self.approved_by if self.approved_by else 'Draft'}` |",
            "",
            "## 1. Background & Objective",
            self.background or f"Deliver '{self.title}' with verifiable acceptance criteria.",
            "",
            "## 2. Scope Boundary",
            "**In Scope:**",
            *([f"- {x}" for x in self.in_scope] or ["- Core feature behavior declared below"]),
            "",
            "**Out of Scope (`EXC`):**",
            *([f"- {x}" for x in self.out_of_scope] or ["- Unrelated module refactoring or speculative plugin hooks"]),
            "",
            "## 3. Structured Requirements (EARS + Given/When/Then)",
        ]
        for req in self.requirements:
            lines.extend(
                [
                    "",
                    f"### {req.id}: {req.title}",
                    f"- **Type**: `{req.req_type.value}` | **Priority**: `{req.priority}` | "
                    f"**Change Type**: `{req.change_type.value}` | **Source**: `{req.source_ref}`",
                    f"- **Statement**: {req.statement}",
                    f"- **Existing Baseline**: {req.existing_baseline}",
                ]
            )
            if req.qas is not None:
                lines.append(
                    f"- **QAS**: source=`{req.qas.source}`, stimulus=`{req.qas.stimulus}`, "
                    f"env=`{req.qas.environment}`, response=`{req.qas.response}`, "
                    f"**measure**=`{req.qas.measure}`"
                )
            if req.acceptance:
                lines.append("- **Acceptance Criteria**:")
                for acc in req.acceptance:
                    lines.append(f"  - {acc}")

        lines.extend(["", "## 4. Open Questions"])
        if not self.open_questions:
            lines.append("- None (all scope and threshold questions resolved).")
        else:
            for q in self.open_questions:
                flag = "BLOCKING" if q.blocking else "non-blocking"
                state = f"ANSWERED: {q.answer}" if q.resolved else "OPEN"
                opts = f" (Options: {', '.join(q.options)})" if q.options else ""
                lines.append(f"- `{q.id}` [{flag} · owner=`{q.owner}` · {state}]: {q.question}{opts}")
        return "\n".join(lines) + "\n"

    def to_lite_markdown(self) -> str:
        lines = [
            f"# Lightweight Task Specification — {self.loop_id}: {self.title}",
            "",
            "## 1. Phenomenon & Background",
            self.background or self.title,
            "",
            "## 2. Expected Outcome & Acceptance",
        ]
        for req in self.requirements:
            lines.append(f"- **{req.id}**: {req.statement}")
            for acc in req.acceptance:
                lines.append(f"  - {acc}")
        lines.extend(
            [
                "",
                "## 3. Blast Radius",
                *([f"- `{s}`" for s in self.in_scope] or ["- Scoped single-module change"]),
                "",
                "## 4. Lite Qualification",
                "- Track: `lite` (no public contract breakage, no cross-module architectural change)",
            ]
        )
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, Any]:
        return {
            "loop_id": self.loop_id,
            "title": self.title,
            "track": self.track,
            "background": self.background,
            "in_scope": list(self.in_scope),
            "out_of_scope": list(self.out_of_scope),
            "requirements": [r.to_dict() for r in self.requirements],
            "open_questions": [q.to_dict() for q in self.open_questions],
            "approved_by": self.approved_by,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SpecDocument:
        return cls(
            loop_id=str(d["loop_id"]),
            title=str(d["title"]),
            track=str(d.get("track", "full")),
            background=str(d.get("background", "")),
            in_scope=list(d.get("in_scope") or []),
            out_of_scope=list(d.get("out_of_scope") or []),
            requirements=[RequirementItem.from_dict(x) for x in d.get("requirements") or []],
            open_questions=[OpenQuestion.from_dict(x) for x in d.get("open_questions") or []],
            approved_by=str(d.get("approved_by", "")),
            created_at=float(d.get("created_at", 0.0)),
        )


@dataclass
class DesignOption:
    id: str  # e.g. "A", "B"
    name: str
    summary: str
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    blast_radius: str = ""
    recommended: bool = False
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "summary": self.summary,
            "pros": list(self.pros),
            "cons": list(self.cons),
            "blast_radius": self.blast_radius,
            "recommended": self.recommended,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DesignOption:
        return cls(
            id=str(d["id"]),
            name=str(d["name"]),
            summary=str(d.get("summary", "")),
            pros=list(d.get("pros") or []),
            cons=list(d.get("cons") or []),
            blast_radius=str(d.get("blast_radius", "")),
            recommended=bool(d.get("recommended", False)),
            rationale=str(d.get("rationale", "")),
        )


@dataclass
class InterfaceContract:
    """6-part observable interface contract (enables writing tests without reading implementation)."""

    name: str
    module: str
    signature: str
    inputs_preconditions: str
    outputs_postconditions: str
    error_semantics: str
    side_effects: str = "None (pure)"
    concurrency: str = "Single-caller / stateless"
    compatibility: str = "Additive / backwards-compatible"

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "module": self.module,
            "signature": self.signature,
            "inputs_preconditions": self.inputs_preconditions,
            "outputs_postconditions": self.outputs_postconditions,
            "error_semantics": self.error_semantics,
            "side_effects": self.side_effects,
            "concurrency": self.concurrency,
            "compatibility": self.compatibility,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> InterfaceContract:
        return cls(
            name=str(d["name"]),
            module=str(d.get("module", "")),
            signature=str(d["signature"]),
            inputs_preconditions=str(d.get("inputs_preconditions", "")),
            outputs_postconditions=str(d.get("outputs_postconditions", "")),
            error_semantics=str(d.get("error_semantics", "")),
            side_effects=str(d.get("side_effects", "None")),
            concurrency=str(d.get("concurrency", "Stateless")),
            compatibility=str(d.get("compatibility", "Backwards-compatible")),
        )


@dataclass
class ErrorModelEntry:
    category: str  # caller_contract | runtime_expected | dependency_fault
    trigger: str
    handling: str
    state_guarantee: str  # atomic rollback | unchanged | partial-logged

    def to_dict(self) -> dict[str, str]:
        return {
            "category": self.category,
            "trigger": self.trigger,
            "handling": self.handling,
            "state_guarantee": self.state_guarantee,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ErrorModelEntry:
        return cls(
            category=str(d.get("category", "runtime_expected")),
            trigger=str(d["trigger"]),
            handling=str(d["handling"]),
            state_guarantee=str(d.get("state_guarantee", "state unchanged")),
        )


@dataclass
class TestCaseSpec:
    """Canonical test design entry bridging `spec.md` Acceptance -> `tasks.md` TDD."""

    id: str  # e.g. TC-001
    covers_req: str  # e.g. FR-001
    scenario_gwt: str
    expected: str
    level: str = "unit"  # unit | integration
    category: str = "positive"  # positive | boundary | error | regression
    mock_boundary: str = "none (exercise real module logic)"

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "covers_req": self.covers_req,
            "scenario_gwt": self.scenario_gwt,
            "expected": self.expected,
            "level": self.level,
            "category": self.category,
            "mock_boundary": self.mock_boundary,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TestCaseSpec:
        return cls(
            id=str(d["id"]),
            covers_req=str(d["covers_req"]),
            scenario_gwt=str(d["scenario_gwt"]),
            expected=str(d["expected"]),
            level=str(d.get("level", "unit")),
            category=str(d.get("category", "positive")),
            mock_boundary=str(d.get("mock_boundary", "none")),
        )


@dataclass
class DesignDocument:
    loop_id: str
    title: str
    overview: str = ""
    module_responsibilities: dict[str, str] = field(default_factory=dict)
    options: list[DesignOption] = field(default_factory=list)
    selected_option: str = "A"
    decision_source: str = "agent_recommended"  # human_selected | agent_recommended
    interfaces: list[InterfaceContract] = field(default_factory=list)
    error_model: list[ErrorModelEntry] = field(default_factory=list)
    flow_diagram_mermaid: str = ""
    test_cases: list[TestCaseSpec] = field(default_factory=list)
    approved_by: str = ""

    def to_markdown(self) -> str:
        lines = [
            f"# Detailed Design — {self.loop_id}: {self.title}",
            "",
            "## 1. Design Overview & Simplicity Principle",
            self.overview or f"Minimal, cohesive design satisfying `{self.loop_id}` requirements.",
            "",
            "## 2. Architecture Options & Trade-Off Decision",
            f"- **Selected Option**: `Option {self.selected_option}` (source: `{self.decision_source}`)",
        ]
        for opt in self.options:
            rec = " *(Recommended)*" if opt.recommended else ""
            lines.extend(
                [
                    "",
                    f"### Option {opt.id}: {opt.name}{rec}",
                    f"- **Summary**: {opt.summary}",
                    f"- **Pros**: {'; '.join(opt.pros) or 'Minimal moving parts'}",
                    f"- **Cons**: {'; '.join(opt.cons) or 'Scoped to current requirement'}",
                    f"- **Blast Radius**: {opt.blast_radius or 'Declared module scope'}",
                ]
            )
            if opt.rationale:
                lines.append(f"- **Rationale**: {opt.rationale}")

        lines.extend(["", "## 3. Module Responsibilities (Single-Sentence Rule)"])
        for mod, resp in self.module_responsibilities.items():
            lines.append(f"- `{mod}`: {resp}")

        if self.flow_diagram_mermaid:
            lines.extend(
                [
                    "",
                    "## 4. Control & Data Flow",
                    "```mermaid",
                    self.flow_diagram_mermaid.strip(),
                    "```",
                ]
            )

        lines.extend(["", "## 5. Interface Contracts (6-Part Observable Semantics)"])
        for iface in self.interfaces:
            lines.extend(
                [
                    "",
                    f"### `{iface.module}` · `{iface.name}`",
                    "```python",
                    iface.signature,
                    "```",
                    f"1. **Inputs & Preconditions**: {iface.inputs_preconditions}",
                    f"2. **Outputs & Postconditions**: {iface.outputs_postconditions}",
                    f"3. **Error Semantics**: {iface.error_semantics}",
                    f"4. **Side Effects**: {iface.side_effects}",
                    f"5. **Concurrency & Timing**: {iface.concurrency}",
                    f"6. **Compatibility**: {iface.compatibility}",
                ]
            )

        lines.extend(
            [
                "",
                "## 6. Error Model & Failure State Guarantees",
                "| Category | Trigger | Boundary Handling | State Guarantee |",
                "|---|---|---|---|",
            ]
        )
        for em in self.error_model:
            lines.append(
                f"| `{em.category}` | {em.trigger} | {em.handling} | {em.state_guarantee} |"
            )

        lines.extend(
            [
                "",
                "## 7. Canonical Test Design (`TC-*`)",
                "| Case ID | Covers Req | Category | Level | Scenario (Given/When/Then) | Expected Outcome | Mock Boundary |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for tc in self.test_cases:
            lines.append(
                f"| `{tc.id}` | `{tc.covers_req}` | `{tc.category}` | `{tc.level}` | "
                f"{tc.scenario_gwt} | {tc.expected} | {tc.mock_boundary} |"
            )
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, Any]:
        return {
            "loop_id": self.loop_id,
            "title": self.title,
            "overview": self.overview,
            "module_responsibilities": dict(self.module_responsibilities),
            "options": [o.to_dict() for o in self.options],
            "selected_option": self.selected_option,
            "decision_source": self.decision_source,
            "interfaces": [i.to_dict() for i in self.interfaces],
            "error_model": [e.to_dict() for e in self.error_model],
            "flow_diagram_mermaid": self.flow_diagram_mermaid,
            "test_cases": [t.to_dict() for t in self.test_cases],
            "approved_by": self.approved_by,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DesignDocument:
        return cls(
            loop_id=str(d["loop_id"]),
            title=str(d["title"]),
            overview=str(d.get("overview", "")),
            module_responsibilities=dict(d.get("module_responsibilities") or {}),
            options=[DesignOption.from_dict(x) for x in d.get("options") or []],
            selected_option=str(d.get("selected_option", "A")),
            decision_source=str(d.get("decision_source", "agent_recommended")),
            interfaces=[InterfaceContract.from_dict(x) for x in d.get("interfaces") or []],
            error_model=[ErrorModelEntry.from_dict(x) for x in d.get("error_model") or []],
            flow_diagram_mermaid=str(d.get("flow_diagram_mermaid", "")),
            test_cases=[TestCaseSpec.from_dict(x) for x in d.get("test_cases") or []],
            approved_by=str(d.get("approved_by", "")),
        )


@dataclass
class TDDEvidence:
    """Auditable RED -> GREEN -> REFACTOR + 5D Clean-Code proof for one task."""

    red_cmd: str = ""
    red_output: str = ""
    green_cmd: str = ""
    green_output: str = ""
    refactor_note: str = ""
    clean_code_check: dict[str, str] = field(default_factory=dict)
    commits: list[str] = field(default_factory=list)
    loaded_layers: list[str] = field(default_factory=list)
    red_session_id: str = ""
    green_session_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "red_cmd": self.red_cmd,
            "red_output": self.red_output,
            "green_cmd": self.green_cmd,
            "green_output": self.green_output,
            "refactor_note": self.refactor_note,
            "clean_code_check": dict(self.clean_code_check),
            "commits": list(self.commits),
            "loaded_layers": list(self.loaded_layers),
            "red_session_id": self.red_session_id,
            "green_session_id": self.green_session_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TDDEvidence:
        return cls(
            red_cmd=str(d.get("red_cmd", "")),
            red_output=str(d.get("red_output", "")),
            green_cmd=str(d.get("green_cmd", "")),
            green_output=str(d.get("green_output", "")),
            refactor_note=str(d.get("refactor_note", "")),
            clean_code_check=dict(d.get("clean_code_check") or {}),
            commits=list(d.get("commits") or []),
            loaded_layers=list(d.get("loaded_layers") or []),
            red_session_id=str(d.get("red_session_id", "")),
            green_session_id=str(d.get("green_session_id", "")),
        )


@dataclass
class TaskSpec:
    """Self-contained, zero-placeholder TDD slice executable by a cold subagent."""

    id: str  # T001
    title: str
    granularity: TaskGranularity = TaskGranularity.STANDARD
    status: TaskStatus = TaskStatus.PENDING
    depends_on: list[str] = field(default_factory=list)
    covers_reqs: list[str] = field(default_factory=list)
    covers_cases: list[str] = field(default_factory=list)
    files_create: list[str] = field(default_factory=list)
    files_modify: list[str] = field(default_factory=list)
    files_test: list[str] = field(default_factory=list)
    consumes: list[str] = field(default_factory=list)  # Exact upstream signatures
    produces: list[str] = field(default_factory=list)  # Exact downstream signatures
    steps: list[str] = field(default_factory=list)
    definition_of_done: str = ""
    blocker_reason: str = ""
    execution_mode: str = "isolated-subagent"
    model_tier: str = "standard"  # cheap | standard | reasoner
    evidence: TDDEvidence | None = None

    def all_files(self) -> list[str]:
        seen: list[str] = []
        for p in (*self.files_create, *self.files_modify, *self.files_test):
            if p and p not in seen:
                seen.append(p)
        return seen

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "granularity": self.granularity.value,
            "status": self.status.value,
            "depends_on": list(self.depends_on),
            "covers_reqs": list(self.covers_reqs),
            "covers_cases": list(self.covers_cases),
            "files_create": list(self.files_create),
            "files_modify": list(self.files_modify),
            "files_test": list(self.files_test),
            "consumes": list(self.consumes),
            "produces": list(self.produces),
            "steps": list(self.steps),
            "definition_of_done": self.definition_of_done,
            "blocker_reason": self.blocker_reason,
            "execution_mode": self.execution_mode,
            "model_tier": self.model_tier,
            "evidence": self.evidence.to_dict() if self.evidence else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TaskSpec:
        ev = d.get("evidence")
        return cls(
            id=str(d["id"]),
            title=str(d["title"]),
            granularity=TaskGranularity(str(d.get("granularity", "standard"))),
            status=TaskStatus(str(d.get("status", "pending"))),
            depends_on=list(d.get("depends_on") or []),
            covers_reqs=list(d.get("covers_reqs") or []),
            covers_cases=list(d.get("covers_cases") or []),
            files_create=list(d.get("files_create") or []),
            files_modify=list(d.get("files_modify") or []),
            files_test=list(d.get("files_test") or []),
            consumes=list(d.get("consumes") or []),
            produces=list(d.get("produces") or []),
            steps=list(d.get("steps") or []),
            definition_of_done=str(d.get("definition_of_done", "")),
            blocker_reason=str(d.get("blocker_reason", "")),
            execution_mode=str(d.get("execution_mode", "isolated-subagent")),
            model_tier=str(d.get("model_tier", "standard")),
            evidence=TDDEvidence.from_dict(ev) if isinstance(ev, dict) else None,
        )


@dataclass
class GateCheckItem:
    id: str
    title: str
    verdict: str  # YES | NO | WARN
    evidence: str
    severity: str = "major"  # critical | major | minor
    fix_hint: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "title": self.title,
            "verdict": self.verdict,
            "evidence": self.evidence,
            "severity": self.severity,
            "fix_hint": self.fix_hint,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GateCheckItem:
        return cls(
            id=str(d["id"]),
            title=str(d["title"]),
            verdict=str(d["verdict"]),
            evidence=str(d.get("evidence", "")),
            severity=str(d.get("severity", "major")),
            fix_hint=str(d.get("fix_hint", "")),
        )


@dataclass
class PhaseGateRecord:
    phase: str  # spec | design | plan | sdc
    round_no: int
    passed: bool
    items: list[GateCheckItem] = field(default_factory=list)
    forced_by: str = ""
    timestamp: float = field(default_factory=time.time)

    def failed_items(self) -> list[GateCheckItem]:
        return [i for i in self.items if i.verdict == "NO"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "round_no": self.round_no,
            "passed": self.passed,
            "items": [i.to_dict() for i in self.items],
            "forced_by": self.forced_by,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PhaseGateRecord:
        return cls(
            phase=str(d["phase"]),
            round_no=int(d.get("round_no", 1)),
            passed=bool(d.get("passed", False)),
            items=[GateCheckItem.from_dict(x) for x in d.get("items") or []],
            forced_by=str(d.get("forced_by", "")),
            timestamp=float(d.get("timestamp", 0.0)),
        )


@dataclass
class TaskPlan:
    loop_id: str
    goal: str
    architecture_summary: str = ""
    global_constraints: list[str] = field(default_factory=list)
    tasks: list[TaskSpec] = field(default_factory=list)
    gate_records: list[PhaseGateRecord] = field(default_factory=list)
    review_passed: bool = False
    acceptance_passed: bool = False

    def latest_gate(self, phase: str) -> PhaseGateRecord | None:
        matches = [g for g in self.gate_records if g.phase == phase]
        return matches[-1] if matches else None

    def gate_rounds(self, phase: str) -> int:
        return sum(1 for g in self.gate_records if g.phase == phase)

    def to_markdown(self) -> str:
        lines = [
            f"# Implementation Plan & Task Ledger — {self.loop_id}",
            "",
            f"**Goal:** {self.goal}",
            "",
            f"**Architecture:** {self.architecture_summary or 'Modular, test-first implementation matching design.md.'}",
            "",
            "## Global Constraints",
            *([f"- {c}" for c in self.global_constraints] or ["- TDD first (RED -> GREEN -> REFACTOR); context < 40%"]),
            "",
            "## Task Summary Table",
            "| ID | Title | Granularity | Depends | Status | Model Tier |",
            "|---|---|---|---|---|---|",
        ]
        for t in self.tasks:
            deps = ", ".join(t.depends_on) if t.depends_on else "-"
            lines.append(
                f"| {t.id} | {t.title} | {t.granularity.value} | {deps} | {t.status.value} | {t.model_tier} |"
            )

        lines.extend(["", "## Detailed Self-Contained Tasks"])
        for t in self.tasks:
            lines.extend(
                [
                    "",
                    f"### {t.id}: {t.title} [{t.status.value}]",
                    f"- **Granularity**: `{t.granularity.value}` | **Execution**: `{t.execution_mode}` | **Model Tier**: `{t.model_tier}`",
                    f"- **Covers Requirements**: {', '.join(f'`{r}`' for r in t.covers_reqs) or 'N/A'}",
                    f"- **Covers Test Cases**: {', '.join(f'`{c}`' for c in t.covers_cases) or 'N/A'}",
                    f"- **Dependencies**: {', '.join(f'`{d}`' for d in t.depends_on) or 'none'}",
                    "- **Files**:",
                ]
            )
            for fc in t.files_create:
                lines.append(f"  - Create: `{fc}`")
            for fm in t.files_modify:
                lines.append(f"  - Modify: `{fm}`")
            for ft in t.files_test:
                lines.append(f"  - Test: `{ft}`")
            lines.append("- **Interfaces**:")
            lines.append(f"  - Consumes: {'; '.join(f'`{c}`' for c in t.consumes) or 'none'}")
            lines.append(f"  - Produces: {'; '.join(f'`{p}`' for p in t.produces) or 'internal behavior'}")
            lines.append("- **TDD Steps**:")
            done_mark = "x" if t.status == TaskStatus.PASSING else " "
            for st in t.steps:
                lines.append(f"  - [{done_mark}] {st}")
            lines.append(f"- **Definition of Done**: {t.definition_of_done}")
            if t.blocker_reason:
                lines.append(f"- **Blocker**: {t.blocker_reason}")
            if t.evidence is not None:
                ev = t.evidence
                lines.extend(
                    [
                        "- **TDD Evidence**:",
                        f"  - RED: `{ev.red_cmd}` -> {ev.red_output}",
                        f"  - GREEN: `{ev.green_cmd}` -> {ev.green_output}",
                        f"  - REFACTOR: {ev.refactor_note}",
                    ]
                )
                if ev.clean_code_check:
                    lines.append("  - Clean-Code 5D Check:")
                    for dim, note in ev.clean_code_check.items():
                        lines.append(f"    - `{dim}`: {note}")

        lines.extend(["", "## Phase Gate Records"])
        if not self.gate_records:
            lines.append("- (no phase gate records yet)")
        else:
            for g in self.gate_records:
                verdict = "PASS" if g.passed else "FAIL"
                forced = f" (forced by {g.forced_by})" if g.forced_by else ""
                failed_ids = ", ".join(i.id for i in g.failed_items()) or "none"
                lines.extend(
                    [
                        "",
                        f"### {g.phase} Gate Record (Round {g.round_no})",
                        f"- Gate Verdict: {verdict}{forced}",
                        f"- Checks Evaluated: {len(g.items)}",
                        f"- Failed Items: {failed_ids}",
                    ]
                )

        if self.review_passed:
            lines.extend(["", "## S/D/C Review Record", "- Verdict: PASS"])
        if self.acceptance_passed:
            lines.extend(["", "## Acceptance (ST) Record", "- Go/No-Go: Go (PASS)"])
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, Any]:
        return {
            "loop_id": self.loop_id,
            "goal": self.goal,
            "architecture_summary": self.architecture_summary,
            "global_constraints": list(self.global_constraints),
            "tasks": [t.to_dict() for t in self.tasks],
            "gate_records": [g.to_dict() for g in self.gate_records],
            "review_passed": self.review_passed,
            "acceptance_passed": self.acceptance_passed,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TaskPlan:
        return cls(
            loop_id=str(d["loop_id"]),
            goal=str(d["goal"]),
            architecture_summary=str(d.get("architecture_summary", "")),
            global_constraints=list(d.get("global_constraints") or []),
            tasks=[TaskSpec.from_dict(x) for x in d.get("tasks") or []],
            gate_records=[PhaseGateRecord.from_dict(x) for x in d.get("gate_records") or []],
            review_passed=bool(d.get("review_passed", False)),
            acceptance_passed=bool(d.get("acceptance_passed", False)),
        )


@dataclass
class TraceabilityRow:
    req_id: str
    change_type: str
    source_ref: str
    design_section: str = ""
    test_cases: list[str] = field(default_factory=list)
    tasks: list[str] = field(default_factory=list)
    code_files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    verification_evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "req_id": self.req_id,
            "change_type": self.change_type,
            "source_ref": self.source_ref,
            "design_section": self.design_section,
            "test_cases": list(self.test_cases),
            "tasks": list(self.tasks),
            "code_files": list(self.code_files),
            "test_files": list(self.test_files),
            "verification_evidence": self.verification_evidence,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TraceabilityRow:
        return cls(
            req_id=str(d["req_id"]),
            change_type=str(d.get("change_type", "new")),
            source_ref=str(d.get("source_ref", "")),
            design_section=str(d.get("design_section", "")),
            test_cases=list(d.get("test_cases") or []),
            tasks=list(d.get("tasks") or []),
            code_files=list(d.get("code_files") or []),
            test_files=list(d.get("test_files") or []),
            verification_evidence=str(d.get("verification_evidence", "")),
        )


@dataclass
class TraceabilityMatrix:
    loop_id: str
    rows: list[TraceabilityRow] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            f"# Bidirectional Traceability Matrix — {self.loop_id}",
            "",
            "| Requirement | Change Type | Source | Design Section | Test Cases | Tasks | Source Files | Test Files | Evidence |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in self.rows:
            lines.append(
                f"| `{r.req_id}` | `{r.change_type}` | {r.source_ref} | {r.design_section or '-'} | "
                f"{', '.join(r.test_cases) or '-'} | {', '.join(r.tasks) or '-'} | "
                f"{', '.join(r.code_files) or '-'} | {', '.join(r.test_files) or '-'} | "
                f"{r.verification_evidence or 'pending'} |"
            )
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, Any]:
        return {"loop_id": self.loop_id, "rows": [r.to_dict() for r in self.rows]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TraceabilityMatrix:
        return cls(
            loop_id=str(d["loop_id"]),
            rows=[TraceabilityRow.from_dict(x) for x in d.get("rows") or []],
        )


@dataclass
class AcceptanceCase:
    id: str  # AT-001
    req_id: str
    category: str  # normal | boundary | error | regression
    scenario_gwt: str
    expected: str
    actual: str = ""
    status: str = "PASS"  # PASS | FAIL | BLOCKED

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "req_id": self.req_id,
            "category": self.category,
            "scenario_gwt": self.scenario_gwt,
            "expected": self.expected,
            "actual": self.actual,
            "status": self.status,
        }


@dataclass
class AcceptanceReport:
    loop_id: str
    title: str
    cases: list[AcceptanceCase] = field(default_factory=list)
    requirement_coverage: float = 1.0
    verdict: str = "GO"  # GO | CONDITIONAL_GO | NO_GO
    residual_minors: list[str] = field(default_factory=list)
    archived_to: str = ""

    def to_markdown(self) -> str:
        passed = sum(1 for c in self.cases if c.status == "PASS")
        failed = sum(1 for c in self.cases if c.status == "FAIL")
        blocked = sum(1 for c in self.cases if c.status == "BLOCKED")
        lines = [
            f"# Acceptance Test Cases & Go/No-Go Report — {self.loop_id}: {self.title}",
            "",
            "## 1. Execution Summary",
            "| Total | Passed | Failed | Blocked | Requirement Coverage | Verdict |",
            "|---|---|---|---|---|---|",
            f"| {len(self.cases)} | {passed} | {failed} | {blocked} | {self.requirement_coverage:.0%} | **{self.verdict}** |",
            "",
            "## 2. Black-Box Acceptance Cases (Derived from `spec.md`)",
        ]
        for c in self.cases:
            lines.extend(
                [
                    "",
                    f"### {c.id} · Covers `{c.req_id}` ({c.category})",
                    f"- **Scenario**: {c.scenario_gwt}",
                    f"- **Expected**: {c.expected}",
                    f"- **Actual**: {c.actual or 'Verified by automated suite'}",
                    f"- **Status**: `{c.status}`",
                ]
            )
        if self.residual_minors:
            lines.extend(["", "## 3. Residual Non-Blocking Items"])
            for m in self.residual_minors:
                lines.append(f"- {m}")
        if self.archived_to:
            lines.extend(["", f"## 4. Archive Location\n- `{self.archived_to}`"])
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, Any]:
        return {
            "loop_id": self.loop_id,
            "title": self.title,
            "cases": [c.to_dict() for c in self.cases],
            "requirement_coverage": self.requirement_coverage,
            "verdict": self.verdict,
            "residual_minors": list(self.residual_minors),
            "archived_to": self.archived_to,
        }


# ── Markdown Sync Helpers (supports hand-edited `tasks.md`) ──────────────────

_GATE_BLOCK_RE = re.compile(
    r"###\s+[^\n]*?\b(spec|req|design|plan|sdc)\b[^\n]*?Gate Record[\s\S]*?Gate Verdict\s*:\s*(PASS|FAIL)",
    re.IGNORECASE,
)


def parse_markdown_task_statuses(markdown_text: str) -> dict[str, TaskStatus]:
    """Parse task statuses from Markdown `## Task Summary Table` if edited by hand."""
    out: dict[str, TaskStatus] = {}
    in_table = False
    for raw in (markdown_text or "").splitlines():
        line = raw.strip()
        if line.startswith("|") and "ID" in line and "Status" in line:
            in_table = True
            continue
        if in_table and line.startswith("|") and re.match(r"^\|[\s\-|]+\|$", line):
            continue
        if in_table and line.startswith("|"):
            cells = [c.strip() for c in line.split("|") if c.strip()]
            if len(cells) >= 5:
                tid = cells[0]
                st_raw = cells[4].lower()
                for st in TaskStatus:
                    if st.value in st_raw:
                        out[tid] = st
                        break
        elif in_table and not line.startswith("|"):
            in_table = False
    return out


def parse_markdown_gate_verdicts(markdown_text: str) -> dict[str, str]:
    """Extract latest PASS/FAIL verdict per phase from `tasks.md` gate records."""
    res: dict[str, str] = {}
    for m in _GATE_BLOCK_RE.finditer(markdown_text or ""):
        phase = m.group(1).lower()
        if phase == "req":
            phase = "spec"
        res[phase] = m.group(2).upper()
    return res
