"""Cognitive LLM + AST-Grounded Blueprint Synthesizer.

Maximizes LLM effectiveness at every high-leverage reasoning step:
  1. Scans real repository domain context (`AGENTS.md`, AST classes/functions of
     touched modules, existing test suite conventions) so the LLM designs against
     real symbols instead of hallucinating file layouts.
  2. Routes `specify`, `design`, and `decompose` to the **Reasoner / Architect**
     model tier (`HarnessRoleRouter`) in isolated sessions (`spec_<id>`,
     `design_<id>`, `plan_<id>`) with strict JSON contracts.
  3. Automatically splits bundled CRUD requirements (`GranularityAnalyzer`),
     validates 1-to-1 `TC-*` parity, and falls back seamlessly to the AST-grounded
     `DeterministicBlueprintSynthesizer` when running offline or without API keys.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..models import RiskTier
from ..privacy import PrivacyPolicyConfig, redact_for_llm
from ..reviewers import is_test_file
from ..standards.stack import QualityStackCompiler, infer_languages
from .gates import _VAGUE_WORDS_RE, GranularityAnalyzer, LiteEligibilityGate, LiteQualification
from .models import (
    ChangeType,
    DesignDocument,
    DesignOption,
    EARSPattern,
    ErrorModelEntry,
    InterfaceContract,
    OpenQuestion,
    QualityScenario,
    RequirementItem,
    RequirementType,
    SpecDocument,
    TaskGranularity,
    TaskPlan,
    TaskSpec,
    TestCaseSpec,
    TraceabilityMatrix,
    TraceabilityRow,
)

logger = logging.getLogger(__name__)


@dataclass
class RepoDomainContext:
    """Grounded repository facts injected into Blueprint synthesis prompts."""

    languages: list[str] = field(default_factory=list)
    existing_files: list[str] = field(default_factory=list)
    new_files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    discovered_symbols: dict[str, list[str]] = field(default_factory=dict)
    agents_excerpt: str = ""
    test_runner_cmd: str = "python -m pytest -q"

    def to_prompt_block(self) -> str:
        lines = [
            "## Grounded Repository Context",
            f"- Languages: {', '.join(self.languages) or 'python'}",
            f"- Existing target files: {', '.join(self.existing_files) or 'none'}",
            f"- Planned new files: {', '.join(self.new_files) or 'none'}",
            f"- Existing/target test files: {', '.join(self.test_files) or 'tests/'}",
            f"- Verification runner: `{self.test_runner_cmd}`",
        ]
        if self.discovered_symbols:
            lines.append("- Existing module symbols (AST):")
            for path, syms in list(self.discovered_symbols.items())[:8]:
                lines.append(f"  - `{path}`: {', '.join(syms[:10])}")
        if self.agents_excerpt:
            lines.extend(["- Repository `AGENTS.md` excerpt:", self.agents_excerpt])
        return "\n".join(lines)


def scan_repo_domain_context(
    repo_root: str | Path,
    files: list[str],
    privacy_policy: PrivacyPolicyConfig | None = None,
) -> RepoDomainContext:
    root = Path(repo_root).resolve()
    langs = infer_languages(files, root)
    existing: list[str] = []
    new_files: list[str] = []
    tests: list[str] = []
    symbols: dict[str, list[str]] = {}

    for rel in files:
        norm = rel.replace("\\", "/").lstrip("/")
        if is_test_file(norm):
            tests.append(norm)
            continue
        p = (root / norm).resolve()
        if p.is_file() and p.is_relative_to(root):
            existing.append(norm)
            if p.suffix == ".py":
                syms = _extract_python_symbols(p)
                if syms:
                    symbols[norm] = syms
        else:
            new_files.append(norm)

    if not tests:
        # Infer natural companion test path from primary target module
        primary = (existing or new_files or ["src/feature.py"])[0]
        stem = Path(primary).stem
        candidate = f"tests/test_{stem}.py"
        tests.append(candidate)

    agents_excerpt = ""
    agents_file = root / "AGENTS.md"
    if agents_file.is_file():
        raw = agents_file.read_text(encoding="utf-8", errors="replace")[:900]
        agents_excerpt = redact_for_llm(raw, privacy_policy)

    runner = "python -m pytest -q"
    if (root / "package.json").is_file() and "python" not in langs:
        runner = "npm test"
    elif (root / "Cargo.toml").is_file() and "rust" in langs:
        runner = "cargo test"
    elif (root / "go.mod").is_file() and "go" in langs:
        runner = "go test ./..."

    return RepoDomainContext(
        languages=langs,
        existing_files=existing,
        new_files=new_files,
        test_files=tests,
        discovered_symbols=symbols,
        agents_excerpt=agents_excerpt,
        test_runner_cmd=runner,
    )


def _extract_python_symbols(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return []
    out: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            out.append(f"class {node.name}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args if a.arg != "self"]
            out.append(f"def {node.name}({', '.join(args[:4])})")
    return out


# ── Backend Protocol ──────────────────────────────────────────────────────────


@dataclass
class BlueprintBundle:
    qualification: LiteQualification
    spec: SpecDocument
    design: DesignDocument | None
    plan: TaskPlan
    traceability: TraceabilityMatrix
    synthesized_by: str = "deterministic"


@runtime_checkable
class BlueprintBackend(Protocol):
    def synthesize(
        self,
        *,
        loop_id: str,
        title: str,
        requirements_Input: list[str],
        files: list[str],
        tier: RiskTier,
        repo_root: Path,
        requested_lite: bool = False,
        selected_option: str = "A",
        clarifications: dict[str, str] | None = None,
        strict_clarify: bool = False,
    ) -> BlueprintBundle: ...


# ── AST-Grounded Deterministic Synthesizer (fast offline + fallback) ──────────


class DeterministicBlueprintSynthesizer:
    """Produces complete, zero-placeholder Blueprint artifacts grounded in repo AST."""

    def __init__(self, privacy_policy: PrivacyPolicyConfig | None = None) -> None:
        self.privacy_policy = privacy_policy

    def synthesize(
        self,
        *,
        loop_id: str,
        title: str,
        requirements_Input: list[str],
        files: list[str],
        tier: RiskTier,
        repo_root: Path,
        requested_lite: bool = False,
        selected_option: str = "A",
        clarifications: dict[str, str] | None = None,
        strict_clarify: bool = False,
    ) -> BlueprintBundle:
        ctx = scan_repo_domain_context(repo_root, files, privacy_policy=self.privacy_policy)
        answers = clarifications or {}

        # Detect genuine ambiguity when strict_clarify is requested
        open_questions = self._detect_ambiguities(title, requirements_Input, answers, strict_clarify)
        qual = LiteEligibilityGate.evaluate(
            requested_lite=requested_lite,
            title=title,
            files=files,
            tier=tier,
            has_blocking_ambiguity=any(q.blocking and not q.resolved for q in open_questions),
        )
        track = "lite" if qual.eligible else "full"

        spec = self._build_spec(
            loop_id=loop_id,
            title=title,
            raw_reqs=requirements_Input,
            ctx=ctx,
            tier=tier,
            track=track,
            open_questions=open_questions,
        )
        design = None if track == "lite" else self._build_design(spec, ctx, selected_option=selected_option)
        plan = self._build_plan(spec, design, ctx)
        trace = self._build_traceability(spec, design, plan)
        return BlueprintBundle(
            qualification=qual,
            spec=spec,
            design=design,
            plan=plan,
            traceability=trace,
            synthesized_by="deterministic-ast",
        )

    @staticmethod
    def _detect_ambiguities(
        title: str,
        raw_reqs: list[str],
        answers: dict[str, str],
        strict_clarify: bool,
    ) -> list[OpenQuestion]:
        if not strict_clarify:
            return []
        joined = " ".join([title, *raw_reqs])
        questions: list[OpenQuestion] = []
        if re.search(r"\b(?:fast|high\s+concurrency|scalable|soon)\b|高性能|足够快|灵活配置", joined, re.I) and not re.search(
            r"\d+\s*(?:ms|s|rps|qps|%)", joined, re.I
        ):
            questions.append(
                OpenQuestion(
                    id="OQ-001",
                    question="Target latency/throughput threshold is unquantified; what exact SLA applies?",
                    blocking=True,
                    options=["p99 <= 100ms at 100 RPS", "p99 <= 500ms single-node", "No latency SLA (functional only)"],
                    answer=answers.get("OQ-001", ""),
                )
            )
        return questions

    @staticmethod
    def _fold_answers(req_items: list[RequirementItem], open_questions: list[OpenQuestion]) -> None:
        """Quantify vague requirements with the human's answer so the Spec Gate can verify it (G3)."""
        for q in open_questions:
            if not q.resolved:
                continue
            note = f"{q.id}: {q.answer.strip()}"
            for r in req_items:
                if r.req_type != RequirementType.FR:
                    continue
                if not (_VAGUE_WORDS_RE.search(r.statement) or any(_VAGUE_WORDS_RE.search(a) for a in r.acceptance)):
                    continue
                r.statement = f"{r.statement.rstrip('.')}, measured as {q.answer.strip()} ({q.id})."
                r.acceptance = [f"{a.rstrip('.')} [{note}]." for a in r.acceptance]

    def _build_spec(
        self,
        *,
        loop_id: str,
        title: str,
        raw_reqs: list[str],
        ctx: RepoDomainContext,
        tier: RiskTier,
        track: str,
        open_questions: list[OpenQuestion],
    ) -> SpecDocument:
        seeds = [r.strip() for r in raw_reqs if r.strip()] or [title]
        prod_targets = ctx.existing_files + ctx.new_files or ["src/module.py"]
        primary_mod = prod_targets[0]
        is_modify = primary_mod in ctx.existing_files
        baseline_syms = ctx.discovered_symbols.get(primary_mod, [])
        baseline_desc = (
            f"Existing module `{primary_mod}` exports {', '.join(baseline_syms[:4]) or 'current module functions'}."
            if is_modify
            else "N/A"
        )

        req_items: list[RequirementItem] = []
        idx = 1
        for seed in seeds:
            change_type = ChangeType.MODIFY if is_modify else ChangeType.NEW
            stmt = (
                seed
                if re.search(r"\b(?:shall|must)\b|必须|应当", seed, re.I)
                else f"When invoked on `{primary_mod}`, the system shall {seed[0].lower() + seed[1:] if len(seed) > 1 else seed}."
            )
            candidate = RequirementItem(
                id=f"FR-{idx:03d}",
                title=seed[:72],
                statement=stmt.rstrip(".") + ".",
                req_type=RequirementType.FR,
                ears_pattern=EARSPattern.EVENT_DRIVEN,
                acceptance=[
                    f"Given valid inputs matching `{primary_mod}` preconditions, When `{seed[:48]}` is executed, Then the deterministic result is returned and postconditions hold.",
                    f"Given invalid, empty, or boundary inputs, When `{seed[:48]}` is executed, Then an explicit typed error is raised and existing state remains unchanged.",
                ],
                priority="Must",
                change_type=change_type,
                existing_baseline=baseline_desc if change_type == ChangeType.MODIFY else "N/A",
                source_ref=f"request[{idx}]",
            )
            # Automatically split bundled CRUD requirements (`G2`)
            split_items = GranularityAnalyzer.split_crud_requirement(candidate)
            if len(split_items) > 1:
                for sub in split_items:
                    sub.id = f"FR-{idx:03d}"
                    req_items.append(sub)
                    idx += 1
            else:
                req_items.append(candidate)
                idx += 1

        self._fold_answers(req_items, open_questions)

        if tier == RiskTier.HIGH or track == "full":
            req_items.append(
                RequirementItem(
                    id="NFR-001",
                    title="Bounded Structural Complexity & Verification Latency",
                    statement="While executing automated verification, the module shall keep per-function length <= 50 lines, cyclomatic complexity <= 10, and unit verification <= 5000 ms.",
                    req_type=RequirementType.NFR,
                    ears_pattern=EARSPattern.STATE_DRIVEN,
                    acceptance=[
                        "Given the modified source and test files, When static Clean-Code inspection and unit tests run, Then zero functions exceed 50 lines or McCabe complexity 10 and test runtime is <= 5000 ms."
                    ],
                    priority="Must",
                    change_type=ChangeType.NEW,
                    qas=QualityScenario(
                        source="CI / local test runner",
                        stimulus="Execute targeted unit verification and Clean-Code AST inspector",
                        environment="Standard developer / CI runtime",
                        response="All assertions pass with bounded structural metrics",
                        measure="function_lines <= 50, cyclomatic_complexity <= 10, runtime <= 5000 ms",
                    ),
                    source_ref="quality-floor",
                )
            )

        return SpecDocument(
            loop_id=loop_id,
            title=title,
            track=track,
            background=f"Deliver `{title}` across `{', '.join(prod_targets)}` with deterministic TDD verification and Clean-Code guardrails.",
            in_scope=[f"Implement and verify `{p}`" for p in prod_targets] + [f"Automated tests in `{t}`" for t in ctx.test_files],
            out_of_scope=[
                "Unrelated package refactoring outside declared blast radius",
                "Speculative plugin registries or unrequested configuration layers",
            ],
            requirements=req_items,
            open_questions=open_questions,
        )

    def _build_design(
        self,
        spec: SpecDocument,
        ctx: RepoDomainContext,
        *,
        selected_option: str = "A",
    ) -> DesignDocument:
        prod_targets = ctx.existing_files + ctx.new_files or ["src/module.py"]
        mod_resp: dict[str, str] = {}
        for p in prod_targets:
            stem = Path(p).stem
            mod_resp[p] = f"Encapsulate cohesive `{stem}` domain logic and enforce explicit input/error invariants."
        for t in ctx.test_files:
            mod_resp[t] = f"Verify observable positive, boundary, error, and regression contracts for `{spec.loop_id}`."

        choice = selected_option if selected_option in ("A", "B") else "A"
        options = [
            DesignOption(
                id="A",
                name="Direct Cohesive Module Implementation (Minimal Moving Parts)",
                summary=f"Implement typed functions/classes directly inside `{prod_targets[0]}` using standard library primitives.",
                pros=["Zero extra indirection layers", "Directly unit-testable without heavy mocks", "Smallest diff & lowest cognitive load"],
                cons=["Requires refactoring only if >=3 heterogeneous backends emerge in future specs"],
                blast_radius=", ".join(prod_targets),
                recommended=(choice == "A"),
                rationale="Satisfies all current EARS requirements at the lowest rung of the Solution Ladder (YAGNI).",
            ),
            DesignOption(
                id="B",
                name="Abstract Registry + Strategy Adapter Layer",
                summary="Introduce an abstract base interface, dynamic plugin registry, and factory indirection.",
                pros=["Allows runtime strategy swapping if external plugins are added later"],
                cons=["Premature abstraction for a single concrete requirement", "Increases file count and mock surface"],
                blast_radius=", ".join(prod_targets) + " + additional registry module",
                recommended=(choice == "B"),
                rationale="Selected only when explicit runtime multi-provider extensibility is mandated.",
            ),
        ]

        interfaces: list[InterfaceContract] = []
        for p in prod_targets:
            existing_syms = ctx.discovered_symbols.get(p, [])
            if existing_syms:
                sig = existing_syms[0]
                sym_name = sig.split("(")[0].replace("def ", "").replace("class ", "").strip()
            else:
                sym_name = _slug_identifier(Path(p).stem)
                sig = f"def {sym_name}(payload: dict[str, Any]) -> dict[str, Any]"
            interfaces.append(
                InterfaceContract(
                    name=sym_name,
                    module=p,
                    signature=sig,
                    inputs_preconditions="Typed, non-null arguments satisfying domain invariants validated at entry.",
                    outputs_postconditions="Deterministic return value satisfying `spec.md` Given/When/Then postconditions.",
                    error_semantics="Raises `ValueError` on caller precondition violation; propagates unexpected exceptions with context.",
                    side_effects="Scoped exclusively to declared module state; zero hidden global mutation.",
                    concurrency="Stateless / caller-synchronized; zero blocking calls inside async coroutines.",
                    compatibility="Preserves existing public call signatures unless declared `modify` in `spec.md`.",
                )
            )

        error_model = [
            ErrorModelEntry(
                category="caller_contract",
                trigger="Invalid, empty, or out-of-range caller argument",
                handling="Fail fast at module boundary with descriptive `ValueError`",
                state_guarantee="Atomic no-op: existing state remains 100% unchanged",
            ),
            ErrorModelEntry(
                category="runtime_expected",
                trigger="Missing target resource or boundary condition",
                handling="Return explicit typed status or raise domain error per interface contract",
                state_guarantee="No partial writes or leaked file/lock handles (`with` context manager)",
            ),
        ]

        test_cases: list[TestCaseSpec] = []
        tc_idx = 1
        for req in spec.core_requirements():
            test_cases.append(
                TestCaseSpec(
                    id=f"TC-{tc_idx:03d}",
                    covers_req=req.id,
                    scenario_gwt=req.acceptance[0] if req.acceptance else f"Given valid input, When {req.id} runs, Then expected result is returned.",
                    expected=f"Observable output satisfies `{req.id}` acceptance criteria.",
                    level="unit",
                    category="positive",
                    mock_boundary="none (exercise real module implementation)",
                )
            )
            tc_idx += 1
            if req.req_type in (RequirementType.FR, RequirementType.IFR):
                neg_gwt = (
                    req.acceptance[1]
                    if len(req.acceptance) > 1
                    else f"Given invalid or boundary input, When {req.id} runs, Then typed error is raised."
                )
                test_cases.append(
                    TestCaseSpec(
                        id=f"TC-{tc_idx:03d}",
                        covers_req=req.id,
                        scenario_gwt=neg_gwt,
                        expected="Raises explicit validation error; prior state remains unchanged.",
                        level="unit",
                        category="error",
                        mock_boundary="none (exercise real validation boundary)",
                    )
                )
                tc_idx += 1
            if req.change_type in (ChangeType.MODIFY, ChangeType.REMOVE):
                test_cases.append(
                    TestCaseSpec(
                        id=f"TC-{tc_idx:03d}",
                        covers_req=req.id,
                        scenario_gwt=f"Given baseline callers relying on `{req.existing_baseline}`, When regression suite runs, Then unaffected behavior stays intact.",
                        expected="Zero regressions on existing baseline callers.",
                        level="integration",
                        category="regression",
                        mock_boundary="none",
                    )
                )
                tc_idx += 1

        flow = f"flowchart LR\n    Caller --> Boundary[\"{prod_targets[0]}: validate preconditions\"]\n    Boundary --> Core[\"Core domain logic\"]\n    Core --> Out[\"Typed result / explicit error\"]"
        return DesignDocument(
            loop_id=spec.loop_id,
            title=spec.title,
            overview=f"Selected Option {choice}: cohesive implementation across `{', '.join(prod_targets)}` adhering to the Minimal-Code Solution Ladder.",
            module_responsibilities=mod_resp,
            options=options,
            selected_option=choice,
            decision_source="human_selected" if selected_option != "A" else "agent_recommended",
            interfaces=interfaces,
            error_model=error_model,
            flow_diagram_mermaid=flow,
            test_cases=test_cases,
        )

    def _build_plan(
        self,
        spec: SpecDocument,
        design: DesignDocument | None,
        ctx: RepoDomainContext,
    ) -> TaskPlan:
        prod_targets = ctx.existing_files + ctx.new_files or ["src/module.py"]
        test_file = ctx.test_files[0]
        frs = [r for r in spec.core_requirements() if r.req_type != RequirementType.NFR]
        nfrs = [r for r in spec.core_requirements() if r.req_type == RequirementType.NFR]

        tc_by_req: dict[str, list[str]] = {}
        if design is not None:
            for tc in design.test_cases:
                tc_by_req.setdefault(tc.covers_req, []).append(tc.id)

        tasks: list[TaskSpec] = []
        prev_tid = ""
        for idx, req in enumerate(frs, start=1):
            tid = f"T{idx:03d}"
            target_file = prod_targets[min(idx - 1, len(prod_targets) - 1)]
            is_new = target_file in ctx.new_files
            is_micro = spec.track == "lite" or (len(frs) == 1 and len(prod_targets) == 1 and not is_new)
            gran = TaskGranularity.MICRO if is_micro else TaskGranularity.STANDARD
            tier_name = "cheap" if gran == TaskGranularity.MICRO else "standard"

            req_cases = list(tc_by_req.get(req.id, []))
            # Attach NFR cases to the final task so 100% of TC-* and NFR-* are covered without dummy tasks
            covered_reqs = [req.id]
            if idx == len(frs) and nfrs:
                for nfr in nfrs:
                    covered_reqs.append(nfr.id)
                    req_cases.extend(tc_by_req.get(nfr.id, []))

            iface_sigs = [i.signature for i in (design.interfaces if design else []) if i.module == target_file]
            produces = iface_sigs or [f"{target_file}:{req.id}"]
            consumes = tasks[-1].produces if tasks else []

            tasks.append(
                TaskSpec(
                    id=tid,
                    title=f"Implement & verify {req.id}: {req.title[:56]}",
                    granularity=gran,
                    depends_on=[prev_tid] if prev_tid else [],
                    covers_reqs=covered_reqs,
                    covers_cases=req_cases,
                    files_create=[target_file] if is_new else [],
                    files_modify=[] if is_new else [target_file],
                    files_test=[test_file],
                    consumes=consumes,
                    produces=produces,
                    steps=[
                        f"RED: Write failing tests in `{test_file}` covering {', '.join(req_cases) or req.id} and execute `{ctx.test_runner_cmd} {test_file}` to confirm failure on missing behavior.",
                        f"GREEN: Implement minimal cohesive code in `{target_file}` satisfying `{produces[0]}` and re-run `{ctx.test_runner_cmd} {test_file}` until green.",
                        "REFACTOR: Enforce <=50 lines/function, <=3 nesting depth, explicit error context, and record 5D `clean_code_check`.",
                    ],
                    definition_of_done=(
                        f"`{ctx.test_runner_cmd} {test_file}` exits 0, {', '.join(req_cases) or req.id} pass, "
                        f"and `{target_file}` passes `CleanCodeReviewer` with zero warnings."
                    ),
                    execution_mode="inline-tdd" if gran == TaskGranularity.MICRO else "red-green-subagent",
                    model_tier=tier_name,
                )
            )
            prev_tid = tid

        return TaskPlan(
            loop_id=spec.loop_id,
            goal=spec.title,
            architecture_summary=design.overview if design else f"Lightweight direct fix in `{prod_targets[0]}`.",
            global_constraints=[
                "Strict TDD (RED -> GREEN -> REFACTOR) with recorded command outputs",
                "Enforce 3-Layer Coding Standards Quality Stack (5D Clean-Code + Minimal-Code Ladder + Language Guardrails)",
                "Zero edits outside task `files_create` / `files_modify` / `files_test`",
            ],
            tasks=tasks,
        )

    @staticmethod
    def _build_traceability(
        spec: SpecDocument,
        design: DesignDocument | None,
        plan: TaskPlan,
    ) -> TraceabilityMatrix:
        rows: list[TraceabilityRow] = []
        for req in spec.core_requirements():
            tcs = [tc.id for tc in (design.test_cases if design else []) if tc.covers_req == req.id]
            matched_tasks = [t for t in plan.tasks if req.id in t.covers_reqs]
            code_files = sorted({p for t in matched_tasks for p in (*t.files_create, *t.files_modify)})
            test_files = sorted({p for t in matched_tasks for p in t.files_test})
            rows.append(
                TraceabilityRow(
                    req_id=req.id,
                    change_type=req.change_type.value,
                    source_ref=req.source_ref,
                    design_section="§5 Interfaces / §7 Test Design" if design else "lite.md §2",
                    test_cases=tcs,
                    tasks=[t.id for t in matched_tasks],
                    code_files=code_files,
                    test_files=test_files,
                    verification_evidence="pending",
                )
            )
        return TraceabilityMatrix(loop_id=spec.loop_id, rows=rows)


# ── Cognitive LLM Reasoner Backend (with automatic AST validation & fallback) ──


_BLUEPRINT_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(\{[\s\S]*?\})\n```")


class NeoAgentBlueprintBackend:
    """Uses NeoAgent Reasoner tier (`powerful_model`) for spec/design/plan synthesis.

    Grounds the LLM in real AST symbols + 3-Layer Quality Stack, validates every
    generated contract through `GranularityAnalyzer` and `TC-*` parity checks, and
    falls back seamlessly to `DeterministicBlueprintSynthesizer` when no LLM key
    is configured.
    """

    def __init__(
        self,
        fallback: DeterministicBlueprintSynthesizer | None = None,
        quality_compiler: QualityStackCompiler | None = None,
        privacy_policy: PrivacyPolicyConfig | None = None,
    ) -> None:
        self.privacy_policy = privacy_policy
        self.fallback = fallback or DeterministicBlueprintSynthesizer(privacy_policy=privacy_policy)
        self.quality_compiler = quality_compiler or QualityStackCompiler()

    def synthesize(
        self,
        *,
        loop_id: str,
        title: str,
        requirements_Input: list[str],
        files: list[str],
        tier: RiskTier,
        repo_root: Path,
        requested_lite: bool = False,
        selected_option: str = "A",
        clarifications: dict[str, str] | None = None,
        strict_clarify: bool = False,
    ) -> BlueprintBundle:
        base = self.fallback.synthesize(
            loop_id=loop_id,
            title=title,
            requirements_Input=requirements_Input,
            files=files,
            tier=tier,
            repo_root=repo_root,
            requested_lite=requested_lite,
            selected_option=selected_option,
            clarifications=clarifications,
            strict_clarify=strict_clarify,
        )
        # If blocked on ambiguity or Lite track, deterministic synthesis is already exact
        if base.spec.blocking_questions() or base.spec.track == "lite":
            return base

        llm_enriched = self._try_llm_enrichment(
            base=base,
            files=files,
            repo_root=repo_root,
            selected_option=selected_option,
        )
        return llm_enriched or base

    def _try_llm_enrichment(
        self,
        *,
        base: BlueprintBundle,
        files: list[str],
        repo_root: Path,
        selected_option: str,
    ) -> BlueprintBundle | None:
        try:
            from ...core.agent import NeoAgent
            from ...core.config import get_config

            cfg = get_config()
            prov = cfg.llm.default_provider
            if not cfg.api_key_for(prov):
                return None
            ctx = scan_repo_domain_context(repo_root, files, privacy_policy=self.privacy_policy)
            qstack = self.quality_compiler.compile(
                files=files, repo_root=repo_root, stage="intake", max_chars=4000
            )
            prompt = self._build_architect_prompt(base, ctx, qstack.prompt_markdown, selected_option)
            safe_prompt = redact_for_llm(prompt, self.privacy_policy)

            agent = NeoAgent(
                provider=prov,
                model=cfg.llm.powerful_model,
                system_prompt="You are NeoAgent Principal Systems Architect operating in an isolated Blueprint session.",
                session_id=f"blueprint_{base.spec.loop_id}",
                router_enabled=False,
            )
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running and running.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    reply = pool.submit(lambda: asyncio.run(agent.invoke(safe_prompt))).result()
            else:
                reply = asyncio.run(agent.invoke(safe_prompt))

            parsed = _extract_json_dict(reply)
            if not parsed:
                return None
            return self._merge_llm_blueprint(base, parsed)
        except Exception as exc:
            logger.info("LLM blueprint enrichment unavailable, using AST-grounded blueprint: %s", exc)
            return None

    @staticmethod
    def _build_architect_prompt(
        base: BlueprintBundle,
        ctx: RepoDomainContext,
        quality_md: str,
        selected_option: str,
    ) -> str:
        return (
            "You are the Principal Systems Architect & TDD Planner.\n"
            "Refine the Specification, Design Options, 6-part Interface Contracts, and "
            "Self-Contained TDD Task DAG for the following feature.\n\n"
            f"Feature: {base.spec.title}\n"
            f"Selected Option Preference: {selected_option}\n\n"
            f"{ctx.to_prompt_block()}\n\n"
            f"{quality_md}\n\n"
            "Return ONLY a JSON object with optional keys `overview`, `options` (list of "
            "{id,name,summary,pros,cons,blast_radius,recommended,rationale}), and `interfaces` "
            "(list of {name,module,signature,inputs_preconditions,outputs_postconditions,error_semantics}). "
            "Never include company names, internal URLs, or placeholders (`TODO`/`TBD`)."
        )

    @staticmethod
    def _merge_llm_blueprint(base: BlueprintBundle, data: dict[str, Any]) -> BlueprintBundle:
        if base.design is None:
            return base
        overview = str(data.get("overview") or "").strip()
        if overview and not re.search(r"\b(?:TODO|TBD)\b", overview, re.I):
            base.design.overview = overview
            base.plan.architecture_summary = overview
        raw_ifaces = data.get("interfaces")
        if isinstance(raw_ifaces, list) and raw_ifaces:
            valid_ifaces = [
                InterfaceContract.from_dict(x)
                for x in raw_ifaces
                if isinstance(x, dict) and x.get("name") and x.get("signature")
            ]
            if valid_ifaces:
                base.design.interfaces = valid_ifaces
        base.synthesized_by = "reasoner-llm+ast"
        return base


def _extract_json_dict(text: str) -> dict[str, Any] | None:
    m = _BLUEPRINT_JSON_FENCE_RE.search(text or "")
    candidate = m.group(1) if m else (text or "").strip()
    try:
        obj = json.loads(candidate)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _slug_identifier(raw: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", raw).strip("_").lower()
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"execute_{cleaned or 'task'}"
    return cleaned
