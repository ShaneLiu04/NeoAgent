"""Multi-Layer Coding Standards & Clean-Code Quality Stack Compiler.

Compiles a concise, high-leverage engineering guardrail bundle (<40% Smart Zone)
across three orthogonal layers so LLM generators, implementer subagents, and
reviewers share an identical, mechanically auditable definition of "well-written code":

  - **Layer 1 — Clean-Code 5D Floor**: Simplicity, Reliability, Maintainability,
    Testability, Performance + Scope Discipline (with quantitative thresholds).
  - **Layer 2 — 7-Rung Minimal-Code Solution Ladder & Never-Cut Safety Floor**:
    Guides solution shape (reuse -> stdlib -> platform -> installed dep -> one-liner ->
    minimal code) while strictly forbidding shortcuts on trust-boundary validation,
    data-loss error handling, or security controls. Requires bounded debt markers:
    ``# debt: <known ceiling>, revisit when <trigger>``.
  - **Layer 3 — Language-Specific Guardrails**: Public-standard-cited rules
    (PEP 8 / PEP 484 / PEP 3134, C++ Core Guidelines / CERT C++, CERT C, TypeScript
    strict, Go, Rust, Java, SQL) inferred automatically from touched file extensions,
    layered with repository ``AGENTS.md`` conventions and mounted ``SkillRegistry`` guides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FIVE_DIMENSIONS: tuple[str, ...] = (
    "simplicity",
    "reliability",
    "maintainability",
    "testability",
    "performance",
    "scope_discipline",
)

_VAGUE_CHECK_RE = re.compile(
    r"^\s*(?:ok|good|clean|looks?\s+clean|looks?\s+good|all\s+good|fine|pass|yes|lgtm|none)\s*[.!]?\s*$",
    re.IGNORECASE,
)

_DEBT_TAG_RE = re.compile(r"(?:#|//|/\*)\s*(?:debt|lazy)\s*:\s*(.+)", re.IGNORECASE)
_DEBT_TRIGGER_RE = re.compile(
    r"\b(?:when|until|once|if|above|exceeds?|after|before|revisit|upgrade)\b|当|超过|达到|触发|上限|瓶颈|重构",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CodeMetricsThresholds:
    """Default quantitative clean-code thresholds (overridable by repo config)."""

    warn_function_lines: int = 50
    max_function_lines: int = 80
    hard_function_lines: int = 120
    max_params: int = 6
    max_nesting: int = 4
    warn_cyclomatic: int = 12
    hard_cyclomatic: int = 20
    max_file_lines: int = 600


CLEAN_CODE_FLOOR_MD = """\
### Layer 1 · Clean-Code 5D Quality Floor
1. **Simplicity**: Build the smallest structure that satisfies the current specification. Zero speculative plugin hooks, single-implementation interfaces, or unused configuration knobs (YAGNI). One function, one responsibility, one level of abstraction.
2. **Reliability**: Handle every fallible call explicitly. Keep the happy path flat via early-return guard clauses; release files/locks/connections deterministically on every failure path; preserve root-cause context when translating errors at module boundaries.
3. **Maintainability**: Names must state intent and units (`timeout_ms`, `is_verified`) and never lie about side effects. Avoid magic literals, deep nesting (>4 levels), data-clump parameter lists (>6 params), and dead/commented-out code. Extract only genuinely shared domain rules (Rule of Three).
4. **Testability**: Verify observable behavior (inputs -> outputs, state transitions, raised errors), never internal mock call shapes. Zero test-only backdoors in production code; tests must be deterministic, isolated, and free of real wall-clock sleeps.
5. **Performance**: Avoid obvious structural traps on hot paths (N+1 queries, unbounded reads/loops, blocking sync I/O inside async coroutines, leaked handles). Run independent async I/O concurrently; do not add unmeasured micro-optimizations.
6. **Scope Discipline**: One diff, one purpose. Touch only files authorized by the task brief; log out-of-scope observations as follow-up debt rather than expanding the diff."""

SOLUTION_LADDER_MD = """\
### Layer 2 · 7-Rung Minimal-Code Solution Ladder
Read and trace every caller/callee of the target code FIRST, then stop at the lowest rung that cleanly solves the requirement:
- **Rung 1 (Spec Check)**: Is the behavior required by the spec/test cases? Never invent unrequested features; if a spec item looks redundant, implement it and note the question — never silently drop spec scope.
- **Rung 2 (Repo Reuse)**: Reuse existing helpers, types, and patterns already in this repository before writing new ones.
- **Rung 3 (Standard Library)**: Prefer language standard library primitives (`functools`, `pathlib`, `collections`, `dataclasses`, `secrets`) over custom hand-rolled utilities.
- **Rung 4 (Platform Native)**: Prefer built-in runtime/OS/SQL/CSS capabilities over application-layer reimplementations.
- **Rung 5 (Installed Dependencies)**: Use already-installed project dependencies; NEVER add an unplanned third-party package without explicit approval (`BLOCKED`).
- **Rung 6 (Direct Expression)**: Express straightforward transformations directly without ceremonial wrapper classes.
- **Rung 7 (Minimal Necessary Code)**: Write the smallest clear, boundary-safe implementation.

**Never-Cut Safety Floor (CRITICAL to violate)**:
- NEVER cut input validation at trust boundaries (HTTP, CLI, file paths, env, external payloads).
- NEVER cut error handling on paths that can corrupt or lose state.
- NEVER cut security controls (authZ, parameterization, safe deserialization, cryptographic RNG).
- NEVER weaken test assertions or skip edge-case tests to write less code.
- **Bounded Shortcut Rule**: Any deliberate simplification with a known scaling ceiling MUST carry `# debt: <known ceiling>, revisit when <concrete trigger>`."""


@dataclass(frozen=True)
class LanguageStandard:
    language: str
    extensions: tuple[str, ...]
    citations: tuple[str, ...]
    rules: tuple[str, ...]

    def to_markdown(self) -> str:
        cites = ", ".join(self.citations)
        bullets = "\n".join(f"- {r}" for r in self.rules)
        return f"#### Language Guardrails · `{self.language}` ({cites})\n{bullets}"


LANGUAGE_STANDARDS: dict[str, LanguageStandard] = {
    "python": LanguageStandard(
        language="python",
        extensions=(".py", ".pyi"),
        citations=("PEP 8", "PEP 484", "PEP 3134", "CWE-78/94/502"),
        rules=(
            "State isolation: never use mutable default arguments (`[]`, `{}`) or mutable class-body containers; use `None` sentinels or `field(default_factory=...)`. Bind loop variables explicitly in closures (`lambda x=x: ...`).",
            "Types & data contracts: annotate public function parameters and return types (`T | None`, `dataclass(frozen=True)`, `Protocol`); avoid passing untyped `dict[str, Any]` across module boundaries.",
            "Exceptions (EAFP): catch specific exception classes; chain wrapped exceptions via `raise DomainError(...) from exc`; never use bare `except:`, silent `pass`, or `return`/`break` inside `finally:`.",
            "Resources & concurrency: manage files/locks/subprocesses with `with` context managers and explicit timeouts; never call blocking sync I/O (`time.sleep`, `requests.*`, `subprocess.run`) inside `async def`.",
            "Identity & imports: use `is`/`is not` exclusively for `None`/booleans, `isinstance()` for types, `math.isclose()` for floats; never use `from module import *`.",
            "Security: forbid `eval`/`exec`, `shell=True`, `pickle.loads`, unguarded `yaml.load`, `tempfile.mktemp`, `random.*` for secrets (use `secrets`), and un-resolved path prefixes (use `Path.resolve().is_relative_to(...)`).",
        ),
    ),
    "typescript": LanguageStandard(
        language="typescript",
        extensions=(".ts", ".tsx", ".mts", ".cts"),
        citations=("TypeScript Strict", "OWASP ASVS", "CWE-79/942"),
        rules=(
            "Type contracts: model states with discriminated unions and `readonly` interfaces; avoid `any` and unchecked `as` casts — narrow `unknown` at external boundaries.",
            "Immutability & state: prefer copy-on-write updates (`{ ...prev }`, `map`/`filter`) over mutating shared props/state.",
            "Async discipline: parallelize independent promises with `Promise.all`; handle every rejected promise and abort controller signal.",
            "Security: never sink untrusted strings into `innerHTML`/`dangerouslySetInnerHTML`/`eval`; validate URLs before navigation or fetch.",
        ),
    ),
    "javascript": LanguageStandard(
        language="javascript",
        extensions=(".js", ".jsx", ".mjs", ".cjs", ".vue"),
        citations=("ECMAScript Strict", "OWASP ASVS", "CWE-79"),
        rules=(
            "Strict equality & scope: always use `const`/`let` and `===`/`!==`; avoid mutating caller-owned objects or prototypes.",
            "Async & error handling: never leave floating promises; wrap boundary JSON/network calls with contextual error messages and timeouts.",
            "DOM & browser safety: prefer `textContent` and explicit DOM construction over HTML string concatenation; never store secrets in `localStorage`.",
        ),
    ),
    "cpp": LanguageStandard(
        language="cpp",
        extensions=(".cpp", ".cc", ".cxx", ".hpp", ".hxx"),
        citations=("C++ Core Guidelines", "CERT C++", "CWE-119/416"),
        rules=(
            "RAII & ownership: manage memory and handles via `std::unique_ptr`/`std::make_unique`, `std::lock_guard`, and value semantics; never pair `new[]` with `delete` or return addresses of locals.",
            "Class invariants: initialize all members explicitly; mark polymorphic base destructors `virtual` and overrides `override`; never throw from destructors or call virtual methods in constructors/destructors.",
            "Bounds & integer safety: validate external indices/sizes before container or buffer access; guard against signed overflow, unsigned wraparound, and division by zero.",
            "Type & header hygiene: prefer `nullptr`, `constexpr`, and `static_cast`; make headers self-contained with include guards and never place `using namespace` in headers.",
            "Concurrency: guard condition variables with predicate loops; capture escaping lambdas by value rather than dangling local references.",
        ),
    ),
    "c": LanguageStandard(
        language="c",
        extensions=(".c", ".h"),
        citations=("CERT C", "MISRA C Principles", "CWE-120/401"),
        rules=(
            "Memory & cleanup: check every allocation return value; release all acquired resources along a centralized cleanup path on error; avoid use-after-free and double-free.",
            "Buffer safety: enforce explicit byte bounds on all string/memory copies and formatted output; ban unbounded `gets`/`strcpy`/`sprintf`.",
            "Interfaces & macros: make headers self-contained with include guards, restrict file-local helpers with `static`, parenthesize macro parameters, and prefer `const`/`enum`/`static inline`.",
            "Control flow & concurrency: check every fallible return code, include `default:` in `switch`, and pair every lock acquisition with deterministic release.",
        ),
    ),
    "go": LanguageStandard(
        language="go",
        extensions=(".go",),
        citations=("Effective Go", "Go Code Review Comments", "CWE-78/89"),
        rules=(
            "Error handling: check every returned `error`, wrap with `fmt.Errorf(\"...: %w\", err)`, and reserve `panic` for unrecoverable startup invariants.",
            "Concurrency & context: plumb `context.Context` as the first parameter, bound goroutine lifetimes to prevent leaks, and guard shared state with `sync.Mutex` or channels.",
            "Resources: `defer` close/unlock immediately after successful acquisition and check write-close errors.",
        ),
    ),
    "rust": LanguageStandard(
        language="rust",
        extensions=(".rs",),
        citations=("Rust API Guidelines", "Clippy Correctness"),
        rules=(
            "Error propagation: return `Result<T, E>` with `?` in library/service code; forbid `.unwrap()`/`.expect()` on untrusted runtime inputs.",
            "Ownership & safety: prefer borrowing slices (`&str`, `&[T]`) over unnecessary clones; document every `unsafe` block with a `// SAFETY:` invariant proof.",
        ),
    ),
    "java": LanguageStandard(
        language="java",
        extensions=(".java", ".kt"),
        citations=("Effective Java", "CERT Oracle Java", "CWE-89/502/611"),
        rules=(
            "Resources & null safety: close `AutoCloseable` resources via try-with-resources; validate inputs with `Objects.requireNonNull` and return `Optional`/empty collections instead of `null`.",
            "Security: use parameterized `PreparedStatement` (never MyBatis `${}`), disable XXE external entities, and forbid `ObjectInputStream` on untrusted streams.",
        ),
    ),
    "sql": LanguageStandard(
        language="sql",
        extensions=(".sql",),
        citations=("ANSI SQL", "OWASP SQL Prevention", "CWE-89"),
        rules=(
            "Query safety & cost: always bind parameters, list explicit column names instead of `SELECT *`, ensure `WHERE`/`JOIN` predicates align with indexes, and guard `UPDATE`/`DELETE` bounds.",
        ),
    ),
}

_EXT_TO_LANG: dict[str, str] = {
    ext: lang for lang, spec in LANGUAGE_STANDARDS.items() for ext in spec.extensions
}


def infer_languages(files: list[str] | None, repo_root: Path | None = None) -> list[str]:
    """Infer canonical language keys from touched file paths in deterministic order."""
    seen: list[str] = []
    for raw in files or []:
        ext = Path(raw).suffix.lower()
        lang = _EXT_TO_LANG.get(ext)
        if lang and lang not in seen:
            seen.append(lang)
    if not seen and repo_root is not None and (Path(repo_root) / "pyproject.toml").is_file():
        seen.append("python")
    return seen


@dataclass
class CleanCodeCheck:
    """Structured 5-dimension + scope-discipline self-verification contract."""

    simplicity: str = ""
    reliability: str = ""
    maintainability: str = ""
    testability: str = ""
    performance: str = ""
    scope_discipline: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "simplicity": self.simplicity,
            "reliability": self.reliability,
            "maintainability": self.maintainability,
            "testability": self.testability,
            "performance": self.performance,
            "scope_discipline": self.scope_discipline,
        }

    def validate(self) -> list[str]:
        """Mechanically reject empty or rubber-stamped ('looks clean') self-checks."""
        errors: list[str] = []
        for dim in FIVE_DIMENSIONS:
            val = getattr(self, dim, "").strip()
            if not val:
                errors.append(f"Missing clean_code_check dimension '{dim}'")
            elif _VAGUE_CHECK_RE.match(val) or len(val) < 8:
                errors.append(
                    f"Dimension '{dim}' is too vague ({val!r}); cite concrete check or N/A reason"
                )
        return errors


_DIM_ALIASES: dict[str, str] = {
    "simplicity": "simplicity",
    "简洁": "simplicity",
    "reliability": "reliability",
    "可靠": "reliability",
    "maintainability": "maintainability",
    "可维护": "maintainability",
    "testability": "testability",
    "可测试": "testability",
    "performance": "performance",
    "高性能": "performance",
    "性能": "performance",
    "scope_discipline": "scope_discipline",
    "scope": "scope_discipline",
    "范围纪律": "scope_discipline",
    "范围": "scope_discipline",
}


def parse_clean_code_check(text: str) -> CleanCodeCheck:
    """Parse bullet/key-value ``clean_code_check`` lines (English or Chinese labels)."""
    fields: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        if ":" not in line and "：" not in line:
            continue
        sep = ":" if ":" in line else "："
        key, val = line.split(sep, 1)
        norm_key = key.strip().lower().replace(" ", "_").replace("-", "_")
        target = _DIM_ALIASES.get(norm_key)
        if target and val.strip():
            fields[target] = val.strip()
    return CleanCodeCheck(**fields)


def inspect_debt_markers(line: str) -> tuple[bool, bool]:
    """Return ``(has_debt_marker, has_valid_trigger)`` for `# debt:` / `# lazy:` comments."""
    m = _DEBT_TAG_RE.search(line)
    if not m:
        return False, True
    body = m.group(1).strip()
    has_trigger = bool(_DEBT_TRIGGER_RE.search(body)) and ("," in body or ";" in body or "，" in body or "；" in body or len(body) >= 24)
    return True, has_trigger


@dataclass
class QualityStackBundle:
    """Compiled 3-Layer Quality Stack ready for prompt injection or CLI inspection."""

    stage: str
    languages: list[str]
    required_layers: list[str]
    matched_skills: list[str] = field(default_factory=list)
    has_repo_agents_md: bool = False
    thresholds: CodeMetricsThresholds = field(default_factory=CodeMetricsThresholds)
    prompt_text: str = ""

    @property
    def prompt_markdown(self) -> str:
        return self.prompt_text

    @property
    def loaded_layers(self) -> list[str]:
        return self.required_layers

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "languages": list(self.languages),
            "required_layers": list(self.required_layers),
            "matched_skills": list(self.matched_skills),
            "has_repo_agents_md": self.has_repo_agents_md,
            "thresholds": {
                "warn_function_lines": self.thresholds.warn_function_lines,
                "max_function_lines": self.thresholds.max_function_lines,
                "max_params": self.thresholds.max_params,
                "max_nesting": self.thresholds.max_nesting,
                "warn_cyclomatic": self.thresholds.warn_cyclomatic,
            },
            "prompt_chars": len(self.prompt_text),
        }


class QualityStackCompiler:
    """Compiles the 3-Layer Quality Stack within a strict character budget (<40% context rule)."""

    def __init__(self, skill_registry: Any = None, **_: Any) -> None:
        self._default_skill_registry = skill_registry

    def compile(
        self,
        files: list[str] | None = None,
        *,
        languages: list[str] | None = None,
        stage: str = "build",
        query: str = "",
        repo_root: Path | None = None,
        skill_registry: Any = None,
        thresholds: CodeMetricsThresholds | None = None,
        max_chars: int = 10_000,
    ) -> QualityStackBundle:
        from ..privacy import redact_for_llm

        th = thresholds or CodeMetricsThresholds()
        langs = list(languages) if languages else infer_languages(files, repo_root)
        active_registry = skill_registry if skill_registry is not None else self._default_skill_registry

        layers = ["clean-code-5d", "minimal-code-ladder"]
        sections: list[str] = [
            "## NeoAgent 3-Layer Engineering Quality Stack (Mandatory)",
            CLEAN_CODE_FLOOR_MD,
            SOLUTION_LADDER_MD,
        ]

        for lang in langs:
            spec = LANGUAGE_STANDARDS.get(lang.lower())
            if spec is not None:
                layers.append(f"{spec.language}-standards")
                sections.append(spec.to_markdown())

        has_agents = False
        if repo_root is not None:
            agents_path = Path(repo_root) / "AGENTS.md"
            if agents_path.is_file():
                has_agents = True
                layers.append("repo-agents-schema")
                raw_agents = agents_path.read_text(encoding="utf-8", errors="replace")[:2_000]
                sections.append(
                    "### Repository `AGENTS.md` Schema Excerpt\n" + redact_for_llm(raw_agents.strip())
                )

        matched_skills: list[str] = []
        if active_registry is not None:
            recs = active_registry.recommend(stage=stage, files=files or [], query=query, limit=4)
            matched_skills = [s.name for s in recs]
            if matched_skills:
                skill_budget = max(1_200, max_chars - sum(len(s) for s in sections) - 200)
                skill_bundle = active_registry.build_context_bundle(
                    matched_skills, max_total_chars=skill_budget
                )
                if skill_bundle:
                    sections.append(skill_bundle)

        full_text = "\n\n".join(sections)
        if len(full_text) > max_chars:
            full_text = full_text[: max_chars - 60] + "\n\n... [Quality Stack capped for 40% Smart Zone]"

        return QualityStackBundle(
            stage=stage,
            languages=langs,
            required_layers=layers,
            matched_skills=matched_skills,
            has_repo_agents_md=has_agents,
            thresholds=th,
            prompt_text=full_text,
        )
