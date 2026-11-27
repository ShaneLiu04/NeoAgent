"""Layered code review engine: deterministic floor + repository memory + parallel LLM review.

  1. Deterministic Floor (``StaticASTReviewer``): Regex + Python AST + DLP privacy
     analysis that runs in milliseconds with zero LLM dependency — catches leaked
     secrets (CWE-798), command/SQL injection (CWE-78/CWE-89), mutable default
     arguments, swallowed exceptions (CWE-390), CWE-22 path prefix bugs,
     workstation/internal-URL privacy leaks, and **AI test-weakening cheats** (CRITICAL).
  2. Organizational Memory (``BugPatternReviewer``): Evaluates diffs against repository
     ``.bug-patterns/checklist.md`` and auto-evolves recurring root causes (Harness L7).
  3. Multi-Perspective AI Review (``SkillOrchestratedReviewer``): Spawns 3 isolated
     NeoAgent sub-agents (Correctness, Security, CleanCode) pre-loaded with matched
     ``SKILL.md`` standards, then enforces Clean-Code Minor/Info density control
     ``ceil(changed_lines / 200)``.

Around these layers, :mod:`neoagent.devops.review` adds the governance that makes a review
believable: change attribution, evidence grounding, independence attestation and routing.
Reviewers declare how the protocol may treat them through four optional attributes:
``deterministic`` (its silence is evidence), ``attributable`` (it can be re-run on the old code),
``reads_worktree`` (it needs checked-out files) and ``context_inputs`` (what it was given).
"""

from __future__ import annotations

import ast
import asyncio
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .models import Finding, FindingCategory, Severity


class Reviewer(Protocol):
    name: str

    def review(self, diff_text: str, repo_root: Path | None = None) -> list[Finding]: ...


class ReviewerBackendUnavailable(Exception):
    """Raised when an optional LLM reviewer backend is unavailable."""


# ── Unified Diff Parser ───────────────────────────────────────────────────────


@dataclass
class DiffLine:
    file: str
    line: int
    text: str
    added: bool


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_diff(diff_text: str) -> list[DiffLine]:
    out: list[DiffLine] = []
    cur_file = ""
    new_ln = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            p = raw[4:].strip()
            cur_file = "" if p == "/dev/null" else (p.removeprefix("b/"))
            continue
        if raw.startswith("--- "):
            if not cur_file:
                p = raw[4:].strip()
                cur_file = p.removeprefix("a/")
            continue
        if raw.startswith("diff --git"):
            cur_file = ""
            continue
        m = _HUNK_RE.match(raw)
        if m:
            new_ln = int(m.group(2))
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            out.append(DiffLine(cur_file, new_ln, raw[1:], True))
            new_ln += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            out.append(DiffLine(cur_file, 0, raw[1:], False))
        elif raw.startswith(" "):
            new_ln += 1
    return out


def is_test_file(path: str) -> bool:
    norm = path.replace("\\", "/").lower()
    return bool(
        re.search(
            r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/)test_[^/]*|_test\.[a-z]+$|\.(test|spec)\.[jt]sx?$",
            norm,
        )
    )


# ── Layer 1: Deterministic Static + Python AST + Anti-Cheat Reviewer ─────────


@dataclass
class _DiffRule:
    rule: str
    pattern: re.Pattern[str]
    severity: Severity
    message: str
    suggestion: str = ""
    code_only: bool = False


_DIFF_RULES: list[_DiffRule] = [
    _DiffRule(
        "secret-hardcoded",
        re.compile(
            r"""(?i)(?:^|(?<![a-z0-9]))(password|passwd|secret|token|api[_-]?key|access[_-]?key)(?:$|(?![a-z0-9]))\s*[:=]\s*['"][^'"\s]{6,}['"]"""
        ),
        Severity.CRITICAL,
        "Suspected hardcoded credential/secret in source code",
        "Load secrets from environment variables or SecretManager and rotate exposed keys.",
    ),
    _DiffRule(
        "secret-aws-key",
        re.compile(r"AKIA[0-9A-Z]{16}"),
        Severity.CRITICAL,
        "Suspected AWS AccessKey ID committed",
        "Revoke key immediately and migrate to IAM role / Vault.",
    ),
    _DiffRule(
        "secret-private-key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
        Severity.CRITICAL,
        "Private cryptographic key material committed",
        "Remove private key from git history and load from secret store.",
    ),
    _DiffRule(
        "dynamic-exec",
        re.compile(r"(?<![\w.])(eval|exec)\s*\("),
        Severity.MAJOR,
        "Dynamic code execution via eval/exec (CWE-94)",
        "Use ast.literal_eval or SafeEvaluator whitelist instead.",
        code_only=True,
    ),
    _DiffRule(
        "shell-injection",
        re.compile(r"shell\s*=\s*True|os\.system\s*\(|os\.popen\s*\("),
        Severity.MAJOR,
        "Command execution via shell=True / os.system risks command injection (CWE-78)",
        "Pass explicit argument lists to subprocess.run(..., shell=False).",
        code_only=True,
    ),
    _DiffRule(
        "unsafe-deserialization",
        re.compile(r"\bpickle\.loads?\s*\(|\byaml\.load\s*\([^)]*(?!Loader\s*=)"),
        Severity.CRITICAL,
        "Unsafe deserialization via pickle or unguarded yaml.load (CWE-502)",
        "Use json.loads or yaml.safe_load on untrusted data.",
        code_only=True,
    ),
    _DiffRule(
        "sql-concat",
        re.compile(
            r"""(?i)(?:execute\w*\(\s*f?["'][^"']*(?:select|insert|update|delete)[^"']*(?:\{|%s|"\s*\+|'\s*\+)|f["']\s*(?:select|insert|update|delete)\b[^"']*\b(?:from|into|set)\b[^"']*\{[^}]+\})"""
        ),
        Severity.CRITICAL,
        "SQL string interpolation/concatenation risks SQL injection (CWE-89)",
        "Use parameterized SQL queries with bound placeholders.",
        code_only=True,
    ),
    _DiffRule(
        "tls-verify-off",
        re.compile(r"verify\s*=\s*False|rejectUnauthorized\s*:\s*false|InsecureSkipVerify\s*:\s*true"),
        Severity.MAJOR,
        "TLS certificate verification disabled",
        "Keep TLS verification enabled and configure CA bundle properly.",
        code_only=True,
    ),
    _DiffRule(
        "cwe22-startswith-path",
        re.compile(r"str\([^)]+\)\.startswith\(\s*str\("),
        Severity.MAJOR,
        "Path confinement check using str.startswith() is vulnerable to sibling-prefix bypass (CWE-22)",
        "Use Path.resolve().is_relative_to(base_dir.resolve()) instead.",
        code_only=True,
    ),
    _DiffRule(
        "bare-except-regex",
        re.compile(r"^\s*except\s*:\s*(#.*)?$"),
        Severity.MAJOR,
        "Bare 'except:' catches SystemExit/KeyboardInterrupt and hides bugs (CWE-390)",
        "Catch specific exception subclasses and log or chain with 'raise ... from err'.",
        code_only=True,
    ),
    _DiffRule(
        "debug-leftover",
        re.compile(r"\bconsole\.log\(|^\s*debugger;?\s*$|\bpdb\.set_trace\(|\bbreakpoint\(\)"),
        Severity.MINOR,
        "Leftover interactive debugger or debug print statement",
        "Remove debugger statement or replace with structured logger.",
        code_only=True,
    ),
    _DiffRule(
        "todo-marker",
        re.compile(r"\b(TODO|FIXME|XXX)\b"),
        Severity.INFO,
        "Unresolved TODO/FIXME marker in new code",
        "Link to a tracked issue ID or resolve before shipping.",
        code_only=True,
    ),
]

_SKIP_MARKERS_RE = re.compile(
    r"@pytest\.mark\.(skip|xfail)|pytest\.skip\(|@unittest\.skip|"
    r"\b(it|test|describe)\.skip\(|\bx(it|describe)\(|\bt\.Skip\(|#\s*noqa|//\s*nolint|@Disabled\b"
)
_COV_FLOOR_RE = re.compile(
    r"(?i)(?:--cov-fail-under[= ]|fail_under\s*[:=]\s*|coverage[_-]?(?:min|threshold)\s*[:=]\s*)(\d+(?:\.\d+)?)"
)
_ASSERT_RE = re.compile(r"\bassert\b|\bexpect\(|\.should\b|\bt\.(Error|Fatal)f?\(|\bAssert\.")


class StaticASTReviewer:
    """Deterministic static + Python AST + DLP privacy + anti-test-weakening reviewer."""

    name = "static-ast"
    deterministic = True
    attributable = True
    reads_worktree = True  # the AST pass reads modified .py files from the worktree
    context_inputs = ("diff", "worktree_sources")

    def __init__(self, privacy_policy: Any = None) -> None:
        self.privacy_policy = privacy_policy

    def review(self, diff_text: str, repo_root: Path | None = None) -> list[Finding]:
        from .privacy import scan_line_privacy, violations_to_findings

        lines = parse_diff(diff_text)
        findings: list[Finding] = []
        privacy_hits = []

        added_lines_by_file: dict[str, set[int]] = {}
        for dl in lines:
            if not dl.file:
                continue
            if dl.added:
                added_lines_by_file.setdefault(dl.file, set()).add(dl.line)
                test_flag = is_test_file(dl.file)
                if not test_flag:
                    privacy_hits.extend(
                        scan_line_privacy(dl.text, dl.file, dl.line, self.privacy_policy)
                    )
                for r in _DIFF_RULES:
                    if r.code_only and test_flag:
                        continue
                    if r.pattern.search(dl.text):
                        findings.append(
                            Finding(
                                rule=r.rule,
                                message=r.message,
                                severity=r.severity,
                                file=dl.file,
                                line=dl.line,
                                source=self.name,
                                suggestion=r.suggestion,
                            )
                        )

        findings.extend(violations_to_findings(privacy_hits, source=self.name))
        findings.extend(self._detect_test_weakening(lines))

        if repo_root is not None:
            findings.extend(self._ast_inspect_python(repo_root, added_lines_by_file))

        return self._dedupe(findings)

    def _detect_test_weakening(self, lines: list[DiffLine]) -> list[Finding]:
        """Block AI reward-hacking: disabling tests, deleting assertions, lowering coverage."""
        out: list[Finding] = []
        removed_asserts: dict[str, int] = {}
        added_asserts: dict[str, int] = {}
        removed_floor: dict[str, float] = {}
        added_floor: dict[str, float] = {}

        for dl in lines:
            if not dl.file:
                continue
            if is_test_file(dl.file):
                if dl.added and _SKIP_MARKERS_RE.search(dl.text):
                    out.append(
                        Finding(
                            rule="test-weakening",
                            message="Added skip/xfail/noqa suppression in test file — forbidden AI shortcut",
                            severity=Severity.CRITICAL,
                            file=dl.file,
                            line=dl.line,
                            source=self.name,
                            category=FindingCategory.SPEC_MISMATCH,
                            suggestion="Fix the underlying bug instead of skipping or suppressing tests.",
                        )
                    )
                if _ASSERT_RE.search(dl.text):
                    bucket = added_asserts if dl.added else removed_asserts
                    bucket[dl.file] = bucket.get(dl.file, 0) + 1

            m = _COV_FLOOR_RE.search(dl.text)
            if m:
                (added_floor if dl.added else removed_floor)[dl.file] = float(m.group(1))

        for f_path, rem in removed_asserts.items():
            add = added_asserts.get(f_path, 0)
            if rem > add:
                out.append(
                    Finding(
                        rule="test-weakening",
                        message=f"Net reduction of {rem - add} assertion(s) in test file (removed {rem}, added {add})",
                        severity=Severity.CRITICAL,
                        file=f_path,
                        line=0,
                        source=self.name,
                        category=FindingCategory.SPEC_MISMATCH,
                        suggestion="Preserve or strengthen existing test assertions.",
                    )
                )

        for f_path, new_val in added_floor.items():
            old_val = removed_floor.get(f_path)
            if old_val is not None and new_val < old_val:
                out.append(
                    Finding(
                        rule="test-weakening",
                        message=f"Coverage gate lowered from {old_val:g}% to {new_val:g}%",
                        severity=Severity.CRITICAL,
                        file=f_path,
                        line=0,
                        source=self.name,
                        category=FindingCategory.SPEC_MISMATCH,
                        suggestion="Add unit tests to meet coverage target instead of lowering threshold.",
                    )
                )
        return out

    def _ast_inspect_python(
        self, repo_root: Path, added_lines_by_file: dict[str, set[int]]
    ) -> list[Finding]:
        """Deep Python AST inspection on modified .py files (PEP 8 & Clean Python Standards)."""
        out: list[Finding] = []
        for rel_file, added_lines in added_lines_by_file.items():
            if not rel_file.endswith(".py") or is_test_file(rel_file):
                continue
            full_path = (repo_root / rel_file).resolve()
            if not full_path.is_file() or not full_path.is_relative_to(repo_root.resolve()):
                continue
            try:
                source = full_path.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source, filename=rel_file)
            except SyntaxError as syn_err:
                out.append(
                    Finding(
                        rule="python-syntax-error",
                        message=f"SyntaxError in modified Python file: {syn_err.msg}",
                        severity=Severity.CRITICAL,
                        file=rel_file,
                        line=syn_err.lineno or 1,
                        source=self.name,
                    )
                )
                continue

            for node in ast.walk(tree):
                lineno = getattr(node, "lineno", 0)
                if lineno not in added_lines:
                    continue

                # Mutable default arguments share state across invocations
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    all_defaults = list(node.args.defaults) + [
                        d for d in node.args.kw_defaults if d is not None
                    ]
                    for d in all_defaults:
                        if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                            out.append(
                                Finding(
                                    rule="mutable-default-arg",
                                    message=(
                                        f"Function '{node.name}' uses mutable default argument "
                                        "(shared mutable state across calls)"
                                    ),
                                    severity=Severity.MAJOR,
                                    file=rel_file,
                                    line=lineno,
                                    source=self.name,
                                    suggestion="Use None as default and initialize inside the function.",
                                )
                            )
                            break

                # CWE-390: Silent `except Exception: pass` swallowing runtime errors
                if isinstance(node, ast.ExceptHandler):
                    is_pass_only = len(node.body) == 1 and isinstance(node.body[0], ast.Pass)
                    if is_pass_only:
                        exc_name = getattr(node.type, "id", None) if node.type else "bare"
                        if exc_name in ("bare", "Exception", "BaseException"):
                            out.append(
                                Finding(
                                    rule="silent-exception-swallow",
                                    message=(
                                        f"Silent 'except {exc_name}: pass' swallows runtime errors "
                                        "without logging or context (CWE-390)"
                                    ),
                                    severity=Severity.MINOR,
                                    file=rel_file,
                                    line=lineno,
                                    source=self.name,
                                    suggestion="Catch narrow exception types and log context via logging.",
                                )
                            )
        return out

    @staticmethod
    def _dedupe(findings: list[Finding]) -> list[Finding]:
        seen: dict[str, Finding] = {}
        for f in findings:
            key = f"{f.rule}|{f.file}|{f.line}"
            if key not in seen or f.severity.rank > seen[key].severity.rank:
                seen[key] = f
        return list(seen.values())


# ── Layer 2: Repository Historical Bug-Pattern Reviewer & Self-Evolution ─────


class BugPatternReviewer:
    """Checks diffs against ``.bug-patterns/checklist.md`` (from bug-pattern-checker skill).

    Format supported in checklist.md:
        ### [P1] Pattern Title
        - Severity: major
        - Regex: ``pattern_here``
        - Message: Explanation and fix advice
    """

    name = "bug-patterns"
    deterministic = True
    attributable = True
    reads_worktree = False
    context_inputs = ("diff", "repo_checklist")

    def __init__(self, checklist_rel_path: str = ".bug-patterns/checklist.md") -> None:
        self.checklist_rel_path = checklist_rel_path

    def review(self, diff_text: str, repo_root: Path | None = None) -> list[Finding]:
        if repo_root is None:
            return []
        checklist_file = repo_root / self.checklist_rel_path
        if not checklist_file.is_file():
            return []

        patterns = self._parse_checklist(checklist_file.read_text(encoding="utf-8", errors="replace"))
        if not patterns:
            return []

        lines = parse_diff(diff_text)
        findings: list[Finding] = []
        for dl in lines:
            if not dl.added or not dl.file or is_test_file(dl.file):
                continue
            for pat_id, sev, regex, msg in patterns:
                if regex.search(dl.text):
                    findings.append(
                        Finding(
                            rule=f"bug-pattern:{pat_id}",
                            message=msg,
                            severity=sev,
                            file=dl.file,
                            line=dl.line,
                            source=self.name,
                        )
                    )
        return findings

    @staticmethod
    def _parse_checklist(markdown_text: str) -> list[tuple[str, Severity, re.Pattern[str], str]]:
        parsed: list[tuple[str, Severity, re.Pattern[str], str]] = []
        blocks = re.split(r"(?m)^###\s+\[(P\d+)\]\s+(.+)$", markdown_text)
        # blocks = [preamble, id1, title1, body1, id2, title2, body2, ...]
        for idx in range(1, len(blocks) - 2, 3):
            pat_id = blocks[idx].strip()
            title = blocks[idx + 1].strip()
            body = blocks[idx + 2]
            sev_match = re.search(r"(?im)severity\s*[:|]\s*(\w+)", body)
            regex_match = re.search(r"(?im)regex\s*[:|]\s*`([^`]+)`", body)
            msg_match = re.search(r"(?im)message\s*[:|]\s*(.+)$", body)
            if not regex_match:
                continue
            try:
                compiled = re.compile(regex_match.group(1))
            except re.error:
                continue
            sev = Severity.parse(sev_match.group(1) if sev_match else "major", Severity.MAJOR)
            msg = msg_match.group(1).strip() if msg_match else f"[{pat_id}] {title}"
            parsed.append((pat_id, sev, compiled, msg))
        return parsed

    def evolve_checklist(self, repo_root: Path, findings: list[Finding]) -> list[str]:
        """Harness L7 Self-Evolution: persist newly discovered critical/major rules."""
        checklist_file = repo_root / self.checklist_rel_path
        existing_text = (
            checklist_file.read_text(encoding="utf-8", errors="replace")
            if checklist_file.is_file()
            else "# Repository Historical Bug Patterns Checklist\n\n"
        )
        existing_ids = [int(n) for n in re.findall(r"\[P(\d+)\]", existing_text)]
        next_num = (max(existing_ids) + 1) if existing_ids else 1

        added_ids: list[str] = []
        for f in findings:
            if f.severity.rank < Severity.MAJOR.rank or f.rule.startswith("bug-pattern:"):
                continue
            marker = f"RuleOrigin: {f.rule}"
            if marker in existing_text:
                continue
            pat_id = f"P{next_num}"
            next_num += 1
            entry = (
                f"\n### [{pat_id}] Recurring Defect — {f.rule}\n"
                f"- Severity: {f.severity.value}\n"
                f"- RuleOrigin: {f.rule}\n"
                f"- Message: Historical defect pattern ({f.rule}): {f.message[:140]}\n"
            )
            existing_text += entry
            added_ids.append(pat_id)

        if added_ids:
            checklist_file.parent.mkdir(parents=True, exist_ok=True)
            checklist_file.write_text(existing_text, encoding="utf-8")
        return added_ids


# ── Layer 3: Multi-Agent Skill-Augmented Parallel LLM Reviewer ───────────────


REVIEW_PERSPECTIVES: dict[str, str] = {
    "correctness": (
        "You are a Principal Engineer reviewing ONLY [Correctness & Reliability]: "
        "logic flaws, boundary/off-by-one bugs, concurrency/race conditions, resource leaks, "
        "missing error handling, and spec drift."
    ),
    "security": (
        "You are an Application Security Architect reviewing ONLY [Security]: "
        "missing authn/authz, IDOR, SQL/command/template injection, SSRF, path traversal, "
        "secret exposure, and unsafe deserialization."
    ),
    "clean_code": (
        "You are a Staff Clean-Code Reviewer reviewing ONLY [Clean Code & Maintainability]: "
        "mutable defaults, naming clarity, single responsibility, missing unit test edge cases, "
        "and API contract stability."
    ),
}

_SKEPTIC_PREAMBLE = (
    "Review like a skeptic: ask 'where does this code lie to its reader?'. Read error paths and "
    "resource-release paths FIRST — defect density is highest there. Do not rationalise: a diff that "
    "'only adds a field', 'is just a fix' or 'is a feature' can carry the same defect classes as any "
    "other. Report only what THIS change introduces; ignore code it did not touch. Quote the exact "
    "offending line in `evidence` — if you cannot quote it, do not report it."
)

_JSON_CONTRACT = (
    "Output ONLY a valid JSON array (no markdown prose). Each item MUST be:\n"
    '{"severity":"critical|major|minor|info","file":"path","line":1,"rule":"short-kebab-rule",'
    '"evidence":"the exact offending line copied from the diff",'
    '"standard":"CWE/OWASP/language rule if one applies, else empty",'
    '"message":"what is wrong","suggestion":"how to fix"}.\n'
    "Output [] if there are no substantive defects. Report ONLY issues in added/modified diff lines; "
    "findings whose evidence does not appear in the diff are discarded automatically."
)


def parse_llm_findings(raw_text: str, source: str) -> list[Finding]:
    """Resiliently parse JSON findings array from LLM output."""
    if not raw_text:
        return []
    candidates = [raw_text.strip()]
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw_text, re.DOTALL)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    start, end = raw_text.find("["), raw_text.rfind("]")
    if start != -1 and end > start:
        candidates.append(raw_text[start : end + 1])

    for cand in candidates:
        try:
            arr = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if not isinstance(arr, list):
            continue
        parsed: list[Finding] = []
        for item in arr:
            if not isinstance(item, dict) or not item.get("message"):
                continue
            try:
                ln = int(item.get("line") or 0)
            except (TypeError, ValueError):
                ln = 0
            parsed.append(
                Finding(
                    rule=str(item.get("rule") or "llm-review")[:60],
                    message=str(item["message"])[:500],
                    severity=Severity.parse(item.get("severity")),
                    file=str(item.get("file") or ""),
                    line=ln,
                    source=source,
                    suggestion=str(item.get("suggestion") or "")[:300],
                    evidence=str(item.get("evidence") or "").strip()[:200],
                    standard=str(item.get("standard") or "").strip()[:60],
                )
            )
        return parsed
    return []


def apply_density_cap(
    findings: list[Finding], changed_lines: int, lines_per_minor: int = 200
) -> tuple[list[Finding], int]:
    """Clean-code review density control:
    - 100% of CRITICAL and MAJOR findings are reported without cap.
    - MINOR + INFO findings are capped at ``max(1, ceil(changed_lines / lines_per_minor))``
      to prevent review comment fatigue.
    Returns ``(kept_findings, suppressed_count)``.
    """
    if lines_per_minor <= 0:
        return findings, 0
    cap = max(1, math.ceil(max(1, changed_lines) / lines_per_minor))
    high_prio = [f for f in findings if f.severity.rank >= Severity.MAJOR.rank]
    low_prio = sorted(
        (f for f in findings if f.severity.rank < Severity.MAJOR.rank),
        key=lambda x: (-x.severity.rank, x.file, x.line),
    )
    kept_low = low_prio[:cap]
    suppressed = max(0, len(low_prio) - len(kept_low))
    return high_prio + kept_low, suppressed


class SkillOrchestratedReviewer:
    """Parallel multi-perspective reviewer powered by NeoAgent Orchestrator + SkillRegistry."""

    name = "neoagent-skills"
    deterministic = False  # generative: its silence is not evidence
    attributable = False
    reads_worktree = False
    #: Exactly what each sub-agent receives — never the author's conversation or reasoning.
    context_inputs = ("diff", "skill_bundle")

    def __init__(
        self,
        perspectives: list[str] | None = None,
        max_diff_chars: int = 24_000,
        timeout: float = 180.0,
        orchestrator: Any = None,
        skill_context: str = "",
    ) -> None:
        self.perspectives = perspectives or list(REVIEW_PERSPECTIVES)
        self.max_diff_chars = max_diff_chars
        self.timeout = timeout
        self._orchestrator = orchestrator
        self.skill_context = skill_context
        #: Task ids of the fresh sub-agent sessions of the latest run (for the attestation).
        self.session_ids: list[str] = []

    def _get_orchestrator(self) -> Any:
        if self._orchestrator is not None:
            return self._orchestrator
        from ..core.config import get_config

        cfg = get_config()
        if not cfg.api_key_for(cfg.llm.default_provider):
            raise ReviewerBackendUnavailable(
                f"No API key configured for provider '{cfg.llm.default_provider}'"
            )
        from ..core.orchestrator import AgentOrchestrator, OrchestratorConfig

        return AgentOrchestrator(
            OrchestratorConfig(
                max_parallel=len(self.perspectives),
                timeout_per_task=self.timeout,
            )
        )

    def review(self, diff_text: str, repo_root: Path | None = None) -> list[Finding]:
        if not diff_text.strip():
            return []
        orch = self._get_orchestrator()
        from ..core.orchestrator import SubAgentRole, SubAgentTask

        body = diff_text[: self.max_diff_chars]
        if len(diff_text) > self.max_diff_chars:
            body += "\n... [diff truncated]"

        skill_block = f"\n\n{self.skill_context}\n" if self.skill_context else ""
        tasks = [
            SubAgentTask(
                id=f"review-{p}",
                prompt=(
                    f"{REVIEW_PERSPECTIVES[p]}\n{_SKEPTIC_PREAMBLE}{skill_block}\n"
                    f"{_JSON_CONTRACT}\n\nDiff:\n{body}"
                ),
                role=SubAgentRole.REVIEWER,
                role_prompt=REVIEW_PERSPECTIVES[p],
            )
            for p in self.perspectives
        ]
        self.session_ids = [t.id for t in tasks]

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                results = pool.submit(lambda: asyncio.run(orch.run_parallel(tasks))).result()
        else:
            results = asyncio.run(orch.run_parallel(tasks))

        findings: list[Finding] = []
        for res in results:
            if not getattr(res, "success", True):
                findings.append(
                    Finding(
                        rule="reviewer-subagent-degraded",
                        message=(
                            f"Perspective '{getattr(res, 'task_id', '?')}' failed: "
                            f"{str(getattr(res, 'error', ''))[:140]}"
                        ),
                        severity=Severity.INFO,
                        source=self.name,
                    )
                )
                continue
            findings.extend(
                parse_llm_findings(
                    getattr(res, "output", ""),
                    f"{self.name}:{getattr(res, 'task_id', '')}",
                )
            )
        return findings


def build_review_chain(
    names: list[str],
    *,
    skill_context: str = "",
    checklist_rel_path: str = ".bug-patterns/checklist.md",
    privacy_policy: Any = None,
    deep: bool = False,
    governed: bool = True,
) -> tuple[list[Reviewer], list[str]]:
    """Construct reviewer pipeline.

    ``StaticASTReviewer`` + ``BugPatternReviewer`` are always active. With ``governed`` (default)
    the deterministic Governed-Review-Protocol reviewers join them — security patterns, public
    contract drift, test evidence and weak-test detection — at zero LLM cost. ``deep`` makes the
    security reviewer also audit whole touched files (pre-existing issues become debt, not blockers).
    """
    from .review import (  # local import: the review package imports this module
        ContractDriftReviewer,
        SecurityReviewer,
        TestEvidenceReviewer,
        WeakTestReviewer,
    )
    from .standards import CleanCodeReviewer

    chain: list[Reviewer] = [
        StaticASTReviewer(privacy_policy=privacy_policy),
        BugPatternReviewer(checklist_rel_path=checklist_rel_path),
    ]
    if governed:
        chain += [
            SecurityReviewer(deep=deep),
            ContractDriftReviewer(),
            TestEvidenceReviewer(),
            WeakTestReviewer(),
            CleanCodeReviewer(),
        ]
    warnings: list[str] = []
    for raw in names:
        n = raw.strip().lower()
        if n in (
            "static",
            "static-ast",
            "bug-patterns",
            "security",
            "contract",
            "test-evidence",
            "weak-tests",
            "clean-code",
            "standards",
            "",
        ):
            continue
        if n in ("neoagent", "neoagent-skills", "llm"):
            chain.append(SkillOrchestratedReviewer(skill_context=skill_context))
        else:
            warnings.append(f"Unknown reviewer '{raw}' ignored")
    return chain, warnings
