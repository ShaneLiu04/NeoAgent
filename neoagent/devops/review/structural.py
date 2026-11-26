"""Change-contract reviewers: does the diff keep the promises the change made?

Pattern-based reviewers ask "is this line bad?". These ask a different question — "does this
*change* behave like the change it claimed to be?":

* ``ScopeDriftReviewer``     — touched files outside the declared scope (AI scope creep).
* ``TestEvidenceReviewer``   — production logic changed, no test touched (no evidence, no merge).
* ``ContractDriftReviewer``  — a public function's signature changed or vanished silently.
* ``WeakTestReviewer``       — new tests that run code but verify nothing (assertion-free,
                               tautological, or existence-only), the favourite shortcut of
                               a model optimising for a green check.

All are deterministic and need no LLM. They complement the anti-weakening rules in
``StaticASTReviewer``, which catch tests being *removed*; these catch tests being *hollowed out
at birth*.
"""

from __future__ import annotations

import ast
import fnmatch
import re
from pathlib import Path

from ..models import Finding, FindingCategory, Severity
from ..reviewers import DiffLine, is_test_file, parse_diff
from .catalog import resolve_rule

CODE_SUFFIXES = frozenset(
    {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".c", ".cc", ".cpp",
     ".h", ".hpp", ".cs", ".kt", ".rb", ".php", ".swift"}
)
_COMMENT_PREFIXES = ("#", "//", "/*", "*", "*/", '"""', "'''")
_DOC_LIKE = re.compile(r"(^|/)(docs?|examples?|scripts?|migrations?)/", re.IGNORECASE)


def _finding(
    rule: str,
    message: str,
    severity: Severity,
    *,
    source: str,
    file: str = "",
    line: int = 0,
    suggestion: str = "",
    evidence: str = "",
) -> Finding:
    spec = resolve_rule(rule)
    return Finding(
        rule=rule,
        message=message,
        severity=severity,
        file=file,
        line=line,
        source=source,
        suggestion=suggestion,
        evidence=evidence,
        standard=spec.standard if spec else "",
        rework=spec.rework if spec else "",
        category=FindingCategory(spec.triage if spec else "llm_fixable"),
    )


def _is_logic_line(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and not stripped.startswith(_COMMENT_PREFIXES)


def _is_prod_code(path: str) -> bool:
    p = path.replace("\\", "/")
    return (
        Path(p).suffix.lower() in CODE_SUFFIXES
        and not is_test_file(p)
        and not _DOC_LIKE.search(p)
        and Path(p).name != "__init__.py"
    )


# ── Scope drift ──────────────────────────────────────────────────────────────


_ANCILLARY_EXT = (".toml", ".lock", ".cfg", ".ini", ".yml", ".yaml", ".json", ".gitignore", ".editorconfig")
_ANCILLARY_DIRS = (".github/", ".gitlab/", ".neoagent-devops/", ".vscode/", ".bug-patterns/")


def _is_ancillary(path: str) -> bool:
    """Repo metadata (root configs, CI definitions, tool state) rather than product source."""
    p = path.replace("\\", "/")
    if p.startswith(_ANCILLARY_DIRS):
        return True
    return "/" not in p and (p.startswith(".") or p.lower().endswith(_ANCILLARY_EXT))


class ScopeDriftReviewer:
    """Flag files changed outside the scope the loop declared at intake."""

    name = "scope-drift"
    deterministic = True
    attributable = False
    reads_worktree = False
    context_inputs = ("diff", "declared_scope")

    def __init__(
        self, declared: list[str], *, allow_tests: bool = True, max_findings: int = 12
    ) -> None:
        self.declared = [d.replace("\\", "/").strip() for d in declared if d.strip()]
        self.allow_tests = allow_tests
        self.max_findings = max_findings

    def _in_scope(self, path: str) -> bool:
        for d in self.declared:
            if path == d or fnmatch.fnmatch(path, d) or path.startswith(d.rstrip("/") + "/"):
                return True
        return False

    def review(self, diff_text: str, repo_root: Path | None = None) -> list[Finding]:
        if not self.declared:
            return []
        files = sorted({dl.file for dl in parse_diff(diff_text) if dl.file})
        out: list[Finding] = []
        for path in files:
            if self._in_scope(path):
                continue
            if self.allow_tests and is_test_file(path):
                continue
            if path.lower().endswith((".md", ".rst", ".txt")):
                continue
            out.append(
                _finding(
                    "scope-drift",
                    f"'{path}' changed but is outside the declared scope {self.declared}",
                    # Source edits outside scope block; ancillary config/CI/metadata edits only warn.
                    Severity.MINOR if _is_ancillary(path) else Severity.MAJOR,
                    source=self.name,
                    file=path,
                    suggestion=(
                        "Revert the unrelated edit, or have the owner extend the declared scope "
                        "(then record why). Do not widen scope silently."
                    ),
                    evidence=path,
                )
            )
            if len(out) >= self.max_findings:
                break
        return out


# ── Missing test evidence ────────────────────────────────────────────────────


class TestEvidenceReviewer:
    """Production logic changed and no test file moved with it."""

    __test__ = False  # not a pytest class despite the name
    name = "test-evidence"
    deterministic = True
    attributable = False
    reads_worktree = False
    context_inputs = ("diff",)

    def __init__(self, min_logic_lines: int = 8) -> None:
        self.min_logic_lines = min_logic_lines

    def review(self, diff_text: str, repo_root: Path | None = None) -> list[Finding]:
        prod: dict[str, int] = {}
        any_test = False
        for dl in parse_diff(diff_text):
            if not dl.file or not dl.added:
                continue
            if is_test_file(dl.file):
                any_test = any_test or _is_logic_line(dl.text)
            elif _is_prod_code(dl.file) and _is_logic_line(dl.text):
                prod[dl.file] = prod.get(dl.file, 0) + 1
        total = sum(prod.values())
        if any_test or total < self.min_logic_lines:
            return []
        biggest = max(prod, key=lambda k: prod[k])
        return [
            _finding(
                "change-without-tests",
                f"{total} added logic line(s) across {len(prod)} production file(s) but no test "
                "was added or changed",
                Severity.MAJOR,
                source=self.name,
                file=biggest,
                suggestion=(
                    "Add a test that fails without this change (RED) and passes with it (GREEN). "
                    "If the change is genuinely untestable, say why in the commit and waive."
                ),
                evidence=f"{total} logic lines, 0 test lines",
            )
        ]


# ── Public contract drift ────────────────────────────────────────────────────

_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\((.*)$")


def _is_public(name: str) -> bool:
    if name in ("__init__", "__call__"):
        return True
    return not name.startswith("_")


def _split_params(sig: str) -> list[str] | None:
    """Parameter source text up to the closing paren; ``None`` if the signature spans lines."""
    depth, buf, params = 1, "", []
    for ch in sig:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                if buf.strip():
                    params.append(buf.strip())
                return params
        if ch == "," and depth == 1:
            if buf.strip():
                params.append(buf.strip())
            buf = ""
        else:
            buf += ch
    return None


def _param_name(param: str) -> str:
    return re.split(r"[:=]", param, maxsplit=1)[0].strip()


def _is_optional(param: str) -> bool:
    """Defaults, ``*args``/``**kwargs`` and the ``*``/``/`` separators never break a caller."""
    if param.startswith("*") or param == "/":
        return True
    return "=" in param.split(":", 1)[-1] if ":" in param else "=" in param


class ContractDriftReviewer:
    """Public ``def`` signatures that changed incompatibly or disappeared."""

    name = "contract-drift"
    deterministic = True
    attributable = False
    reads_worktree = False
    context_inputs = ("diff",)

    def review(self, diff_text: str, repo_root: Path | None = None) -> list[Finding]:
        removed: dict[tuple[str, str], tuple[DiffLine, list[str] | None]] = {}
        added: dict[tuple[str, str], tuple[DiffLine, list[str] | None]] = {}
        added_names: set[str] = set()
        for dl in parse_diff(diff_text):
            if not dl.file or is_test_file(dl.file) or Path(dl.file).suffix != ".py":
                continue
            m = _DEF_RE.match(dl.text)
            if not m or not _is_public(m.group(1)):
                continue
            entry = (dl, _split_params(m.group(2)))
            (added if dl.added else removed)[(dl.file, m.group(1))] = entry
            if dl.added:
                added_names.add(m.group(1))

        out: list[Finding] = []
        for key, (old_dl, old_params) in removed.items():
            file, name = key
            if key not in added:
                if name in added_names:
                    continue  # moved to another file: a relocation, not a removal
                out.append(
                    _finding(
                        "public-api-removed",
                        f"Public function '{name}' was removed",
                        Severity.MAJOR,
                        source=self.name,
                        file=file,
                        line=0,
                        suggestion=(
                            "Confirm no caller depends on it (search usages) and record the removal "
                            "in the design/changelog, or keep a deprecated shim."
                        ),
                        evidence=old_dl.text.strip()[:200],
                    )
                )
                continue
            new_dl, new_params = added[key]
            problem = self._incompatibility(old_params, new_params)
            if problem:
                out.append(
                    _finding(
                        "public-contract-change",
                        f"Public function '{name}' changed incompatibly: {problem}",
                        Severity.MAJOR,
                        source=self.name,
                        file=file,
                        line=new_dl.line,
                        suggestion=(
                            "Keep the old signature working (new parameters need defaults), or make "
                            "this an explicit, recorded breaking change with every caller updated."
                        ),
                        evidence=new_dl.text.strip()[:200],
                    )
                )
        return out

    @staticmethod
    def _incompatibility(old: list[str] | None, new: list[str] | None) -> str:
        if old is None or new is None:
            return ""  # multi-line signature: not enough information to judge
        old_names = [_param_name(p) for p in old]
        new_names = [_param_name(p) for p in new]
        if old_names == new_names:
            return ""
        if new_names[: len(old_names)] == old_names and all(_is_optional(p) for p in new[len(old_names):]):
            return ""  # appended optional parameters stay backwards compatible
        dropped = [n for n in old_names if n not in new_names]
        if dropped:
            return f"parameter(s) {dropped} removed or renamed"
        added_required = [
            _param_name(p) for p in new if _param_name(p) not in old_names and not _is_optional(p)
        ]
        if added_required:
            return f"new required parameter(s) {added_required}"
        return "parameters were reordered"


# ── Hollow tests ─────────────────────────────────────────────────────────────

_ASSERT_CALL_PREFIXES = ("assert", "fail")
_RAISES_NAMES = {"raises", "warns", "deprecated_call", "assertRaises", "assertWarns"}


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _is_tautology(test: ast.expr) -> bool:
    if isinstance(test, ast.Constant):
        return bool(test.value)
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return isinstance(test.operand, ast.Constant) and not test.operand.value
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], (ast.Eq, ast.Is, ast.GtE, ast.LtE)):
        return ast.dump(test.left) == ast.dump(test.comparators[0])
    return False


def _is_existence_only(test: ast.expr) -> bool:
    return (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], (ast.IsNot, ast.NotEq))
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value is None
    )


class WeakTestReviewer:
    """Newly added test functions that cannot fail for the right reason."""

    name = "weak-tests"
    deterministic = True
    attributable = False
    reads_worktree = True
    context_inputs = ("diff", "worktree_tests")

    def review(self, diff_text: str, repo_root: Path | None = None) -> list[Finding]:
        if repo_root is None:
            return []
        added: dict[str, set[int]] = {}
        for dl in parse_diff(diff_text):
            if dl.added and dl.file and dl.file.endswith(".py") and is_test_file(dl.file):
                added.setdefault(dl.file, set()).add(dl.line)
        out: list[Finding] = []
        root = repo_root.resolve()
        for rel, lines in added.items():
            full = (root / rel).resolve()
            if not full.is_file() or not full.is_relative_to(root):
                continue
            try:
                tree = ast.parse(full.read_text(encoding="utf-8", errors="replace"), filename=rel)
            except SyntaxError:
                continue  # StaticASTReviewer already reports syntax errors
            for node in ast.walk(tree):
                if (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name.startswith("test")
                    and node.lineno in lines
                ):
                    out.extend(self._inspect(node, rel))
        return out

    def _inspect(self, fn: ast.FunctionDef | ast.AsyncFunctionDef, rel: str) -> list[Finding]:
        asserts: list[ast.Assert] = []
        calls_assertion = False
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Assert):
                asserts.append(sub)
            elif isinstance(sub, ast.Call):
                name = _call_name(sub)
                if name.startswith(_ASSERT_CALL_PREFIXES) or name in _RAISES_NAMES:
                    calls_assertion = True
        if not asserts and not calls_assertion:
            return [
                _finding(
                    "test-without-assertion",
                    f"Test '{fn.name}' executes code but contains no assertion",
                    Severity.MAJOR,
                    source=self.name,
                    file=rel,
                    line=fn.lineno,
                    suggestion="Assert the observable outcome (return value, state change, raised error).",
                    evidence=f"def {fn.name}(...)",
                )
            ]
        if any(_is_tautology(a.test) for a in asserts) and not calls_assertion:
            bad = next(a for a in asserts if _is_tautology(a.test))
            return [
                _finding(
                    "tautological-assertion",
                    f"Test '{fn.name}' contains an assertion that can never fail",
                    Severity.MAJOR,
                    source=self.name,
                    file=rel,
                    line=bad.lineno,
                    suggestion="Compare the actual result with an independent expected value.",
                    evidence=ast.unparse(bad)[:200],
                )
            ]
        if asserts and not calls_assertion and all(_is_existence_only(a.test) for a in asserts):
            return [
                _finding(
                    "weak-assertion-only",
                    f"Test '{fn.name}' only asserts a value is not None",
                    Severity.MINOR,
                    source=self.name,
                    file=rel,
                    line=asserts[0].lineno,
                    suggestion="Assert the value itself, not merely that something came back.",
                    evidence=ast.unparse(asserts[0])[:200],
                )
            ]
        return []
