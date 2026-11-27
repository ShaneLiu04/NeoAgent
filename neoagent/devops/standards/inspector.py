"""Deterministic Clean-Code & Language-Standards Reviewer.

Enforces high-signal, zero-LLM structural and AST rules on changed code so
naming/complexity/concurrency/exception/state defects are caught deterministically
and attributed (`introduced` vs `carried_over` vs `fixed_by_change`) by
``ReviewProtocol``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from ..models import Finding, FindingCategory, Severity
from ..review.catalog import resolve_rule
from ..reviewers import is_test_file, parse_diff
from .stack import CodeMetricsThresholds, inspect_debt_markers

_WILDCARD_IMPORT_RE = re.compile(r"^\s*from\s+[A-Za-z0-9_.]+\s+import\s+\*\s*(?:#.*)?$")
_ASYNC_BLOCKING_CALLS = frozenset(
    {
        "time.sleep",
        "requests.get",
        "requests.post",
        "requests.put",
        "requests.delete",
        "requests.patch",
        "requests.head",
        "requests.request",
        "urllib.request.urlopen",
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
    }
)


def _make_finding(
    rule: str,
    message: str,
    severity: Severity,
    *,
    source: str,
    file: str,
    line: int,
    suggestion: str,
    evidence: str,
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
        evidence=evidence[:200],
        standard=spec.standard if spec else "",
        rework=spec.rework if spec else "",
        category=FindingCategory(spec.triage if spec else "llm_fixable"),
    )


def _dotted_call_name(node: ast.Call) -> str:
    parts: list[str] = []
    cur: ast.expr = node.func
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return ""


def _cyclomatic_complexity(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    score = 1
    for node in ast.walk(fn):
        if isinstance(node, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.ExceptHandler, ast.IfExp)):
            score += 1
        elif isinstance(node, ast.BoolOp):
            score += max(1, len(node.values) - 1)
        elif isinstance(node, ast.comprehension):
            score += 1 + len(node.ifs)
        elif hasattr(ast, "Match") and isinstance(node, ast.Match):
            score += max(0, len(node.cases) - 1)
    return score


def _max_nesting_depth(stmts: list[ast.stmt], current: int = 0) -> int:
    best = current
    for st in stmts:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue  # nested defs carry their own scope
        if isinstance(st, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try)):
            nxt = current + 1
            best = max(best, nxt)
            best = max(best, _max_nesting_depth(st.body, nxt))
            if isinstance(st, ast.If):
                # `elif` chains do not increase visual indentation depth
                if len(st.orelse) == 1 and isinstance(st.orelse[0], ast.If):
                    best = max(best, _max_nesting_depth(st.orelse, current))
                else:
                    best = max(best, _max_nesting_depth(st.orelse, nxt))
            elif isinstance(st, ast.Try):
                for h in st.handlers:
                    best = max(best, _max_nesting_depth(h.body, nxt))
                best = max(best, _max_nesting_depth(st.orelse, nxt))
                best = max(best, _max_nesting_depth(st.finalbody, nxt))
            else:
                best = max(best, _max_nesting_depth(st.orelse, nxt))
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            best = max(best, _max_nesting_depth(st.body, current))
    return best


def _collect_target_names(target: ast.expr) -> set[str]:
    out: set[str] = set()
    if isinstance(target, ast.Name):
        out.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            out.update(_collect_target_names(elt))
    return out


class CleanCodeReviewer:
    """Deterministic Clean-Code + Python AST & cross-language debt hygiene reviewer."""

    name = "clean-code"
    deterministic = True
    attributable = True
    reads_worktree = False
    context_inputs = ("diff", "touched_file_ast")

    def __init__(self, thresholds: CodeMetricsThresholds | None = None) -> None:
        self.thresholds = thresholds or CodeMetricsThresholds()

    def review(self, diff_text: str, repo_root: Path | None = None) -> list[Finding]:
        parsed = parse_diff(diff_text)
        findings: list[Finding] = []
        py_added_lines: dict[str, dict[int, str]] = {}

        # 1. Diff-line checks (work identically on live diff and inverted old-side diff)
        for dl in parsed:
            if not dl.added or not dl.file:
                continue
            has_debt, valid_debt = inspect_debt_markers(dl.text)
            if has_debt and not valid_debt:
                findings.append(
                    _make_finding(
                        "cc-untracked-debt",
                        f"Shortcut marker lacks an explicit revisit trigger [{dl.text.strip()[:60]}]",
                        Severity.MINOR,
                        source=self.name,
                        file=dl.file,
                        line=dl.line,
                        suggestion="Use `# debt: <known ceiling>, revisit when <concrete trigger>`.",
                        evidence=dl.text.strip(),
                    )
                )

            if dl.file.endswith((".py", ".pyi")):
                py_added_lines.setdefault(dl.file, {})[dl.line] = dl.text
                if Path(dl.file).name != "__init__.py" and _WILDCARD_IMPORT_RE.match(dl.text):
                    findings.append(
                        _make_finding(
                            "py-wildcard-import",
                            f"Wildcard import `{dl.text.strip()}` pollutes namespace and defeats static analysis",
                            Severity.MINOR,
                            source=self.name,
                            file=dl.file,
                            line=dl.line,
                            suggestion="Import explicit symbols or import the module namespace.",
                            evidence=dl.text.strip(),
                        )
                    )

        # 2. Python AST structural & language-gotcha checks
        for rel, line_map in py_added_lines.items():
            source_text = self._load_python_source(rel, line_map, repo_root)
            if not source_text:
                continue
            try:
                tree = ast.parse(source_text, filename=rel)
            except SyntaxError:
                continue
            findings.extend(
                self._inspect_python_ast(
                    tree,
                    rel,
                    set(line_map.keys()),
                    source_text.splitlines(),
                    is_test=is_test_file(rel),
                )
            )

        return findings

    @staticmethod
    def _load_python_source(
        rel: str, added_map: dict[int, str], repo_root: Path | None
    ) -> str:
        if repo_root is not None:
            root = repo_root.resolve()
            cand = (root / rel).resolve()
            if cand.is_file() and cand.is_relative_to(root):
                return cand.read_text(encoding="utf-8", errors="replace")
        # Pure-diff fallback (used in old-side attribution & unit tests without worktree):
        # reconstruct contiguous added blocks when line numbers start at 1 or form valid Python.
        if not added_map:
            return ""
        max_ln = max(added_map)
        if max_ln > 2_000:
            return ""
        buf = [added_map.get(i, "") for i in range(1, max_ln + 1)]
        candidate = "\n".join(buf)
        try:
            ast.parse(candidate)
            return candidate
        except SyntaxError:
            return ""

    def _inspect_python_ast(
        self,
        tree: ast.Module,
        rel: str,
        added_lines: set[int],
        src_lines: list[str],
        *,
        is_test: bool,
    ) -> list[Finding]:
        out: list[Finding] = []
        th = self.thresholds

        def _ev(lineno: int, fallback: str = "") -> str:
            if 1 <= lineno <= len(src_lines):
                return src_lines[lineno - 1].strip()
            return fallback

        for node in ast.walk(tree):
            # ── Function-level clean-code & async checks ─────────────────────
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end_ln = getattr(node, "end_lineno", node.lineno) or node.lineno
                touches_fn = any(node.lineno <= ln <= end_ln for ln in added_lines)
                header_added = node.lineno in added_lines

                if not is_test and header_added:
                    length = end_ln - node.lineno + 1
                    if length > th.max_function_lines:
                        sev = Severity.MAJOR if length > th.hard_function_lines else Severity.MINOR
                        out.append(
                            _make_finding(
                                "cc-long-function",
                                f"Function '{node.name}' spans {length} lines (budget <= {th.max_function_lines})",
                                sev,
                                source=self.name,
                                file=rel,
                                line=node.lineno,
                                suggestion="Extract cohesive intent paragraphs into focused helper functions.",
                                evidence=_ev(node.lineno, f"def {node.name}(...)"),
                            )
                        )

                    depth = _max_nesting_depth(node.body)
                    if depth > th.max_nesting:
                        sev = Severity.MAJOR if depth > th.max_nesting + 2 else Severity.MINOR
                        out.append(
                            _make_finding(
                                "cc-deep-nesting",
                                f"Function '{node.name}' nests control flow {depth} levels deep (max {th.max_nesting})",
                                sev,
                                source=self.name,
                                file=rel,
                                line=node.lineno,
                                suggestion="Use early-return guard clauses so the main path stays flat.",
                                evidence=_ev(node.lineno, f"def {node.name}(...)"),
                            )
                        )

                    cc = _cyclomatic_complexity(node)
                    if cc > th.warn_cyclomatic:
                        sev = Severity.MAJOR if cc > th.hard_cyclomatic else Severity.MINOR
                        out.append(
                            _make_finding(
                                "cc-high-complexity",
                                f"Function '{node.name}' has cyclomatic complexity {cc} (threshold {th.warn_cyclomatic})",
                                sev,
                                source=self.name,
                                file=rel,
                                line=node.lineno,
                                suggestion="Split dispatch/branch tables or extract independent predicates.",
                                evidence=_ev(node.lineno, f"def {node.name}(...)"),
                            )
                        )

                    pos_args = [
                        a.arg
                        for a in (*node.args.posonlyargs, *node.args.args)
                        if a.arg not in ("self", "cls")
                    ]
                    if len(pos_args) > th.max_params:
                        out.append(
                            _make_finding(
                                "cc-too-many-params",
                                f"Function '{node.name}' takes {len(pos_args)} positional parameters (max {th.max_params})",
                                Severity.MINOR,
                                source=self.name,
                                file=rel,
                                line=node.lineno,
                                suggestion="Group cohesive parameters into a `@dataclass(frozen=True)` or keyword-only options.",
                                evidence=_ev(node.lineno, f"def {node.name}(...)"),
                            )
                        )

                    if not node.name.startswith("_"):
                        bool_defaults = [
                            d for d in node.args.defaults if isinstance(d, ast.Constant) and isinstance(d.value, bool)
                        ]
                        if len(bool_defaults) >= 2:
                            out.append(
                                _make_finding(
                                    "cc-boolean-trap",
                                    f"Public function '{node.name}' exposes {len(bool_defaults)} positional boolean flag defaults",
                                    Severity.MINOR,
                                    source=self.name,
                                    file=rel,
                                    line=node.lineno,
                                    suggestion="Make boolean flags keyword-only (`*, flag: bool = False`) or split into explicit functions.",
                                    evidence=_ev(node.lineno, f"def {node.name}(...)"),
                                )
                            )

                if isinstance(node, ast.AsyncFunctionDef) and touches_fn:
                    out.extend(self._check_async_blocking(node, rel, added_lines, _ev))

            # ── Class-body shared mutable state ──────────────────────────────
            elif isinstance(node, ast.ClassDef):
                bases = {
                    (b.id if isinstance(b, ast.Name) else getattr(b, "attr", ""))
                    for b in node.bases
                }
                if bases & {"Enum", "IntEnum", "StrEnum"}:
                    continue
                is_dc = any(
                    (isinstance(d, ast.Name) and d.id == "dataclass")
                    or (isinstance(d, ast.Call) and _dotted_call_name(d).endswith("dataclass"))
                    for d in node.decorator_list
                )
                if is_dc:
                    continue
                for stmt in node.body:
                    if stmt.lineno not in added_lines:
                        continue
                    val: ast.expr | None = None
                    attr_name = ""
                    has_classvar = False
                    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                        attr_name = stmt.targets[0].id
                        val = stmt.value
                    elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                        attr_name = stmt.target.id
                        val = stmt.value
                        ann_txt = ast.unparse(stmt.annotation)
                        has_classvar = "ClassVar" in ann_txt or "Final" in ann_txt
                    if (
                        val is not None
                        and not has_classvar
                        and not attr_name.isupper()
                        and not attr_name.startswith("_")
                        and isinstance(val, (ast.List, ast.Dict, ast.Set))
                    ):
                        out.append(
                            _make_finding(
                                "py-mutable-class-attr",
                                f"Class '{node.name}' defines mutable attribute '{attr_name}' on class body (shared across instances)",
                                Severity.MAJOR,
                                source=self.name,
                                file=rel,
                                line=stmt.lineno,
                                suggestion="Initialize mutable containers in `__init__`, use `field(default_factory=...)`, or annotate `ClassVar`.",
                                evidence=_ev(stmt.lineno),
                            )
                        )

            # ── Try/except/finally hazards ───────────────────────────────────
            elif isinstance(node, ast.Try):
                for fin_stmt in node.finalbody:
                    for sub in ast.walk(fin_stmt):
                        if isinstance(sub, (ast.Return, ast.Break, ast.Continue)) and getattr(sub, "lineno", 0) in added_lines:
                            out.append(
                                _make_finding(
                                    "py-finally-jump",
                                    "Control-flow jump (`return`/`break`/`continue`) inside `finally` silently discards active exceptions",
                                    Severity.MAJOR,
                                    source=self.name,
                                    file=rel,
                                    line=sub.lineno,
                                    suggestion="Keep `finally` strictly for resource cleanup; move `return` outside `finally`.",
                                    evidence=_ev(sub.lineno),
                                )
                            )
                for handler in node.handlers:
                    err_var = handler.name or "err"
                    for sub in ast.walk(handler):
                        if (
                            isinstance(sub, ast.Raise)
                            and sub.exc is not None
                            and sub.cause is None
                            and sub.lineno in added_lines
                            and not (handler.name and isinstance(sub.exc, ast.Name) and sub.exc.id == handler.name)
                        ):
                            out.append(
                                _make_finding(
                                    "py-reraise-without-context",
                                    f"Raising new exception inside `except` without `from {err_var}` (or `from None`) loses root-cause chain",
                                    Severity.MINOR,
                                    source=self.name,
                                    file=rel,
                                    line=sub.lineno,
                                    suggestion=f"Capture `as {err_var}` and use `raise ... from {err_var}` (or `from None` when intentionally hiding internal cause).",
                                    evidence=_ev(sub.lineno),
                                )
                            )

            # ── Late-binding closure inside loop / comprehension ─────────────
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                loop_vars = _collect_target_names(node.target)
                out.extend(self._check_loop_lambdas(node.body, loop_vars, rel, added_lines, _ev))
            elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
                loop_vars = set()
                for gen in node.generators:
                    loop_vars.update(_collect_target_names(gen.target))
                if isinstance(node.elt, ast.Lambda) and getattr(node.elt, "lineno", node.lineno) in added_lines:
                    out.extend(self._inspect_lambda_capture(node.elt, loop_vars, rel, _ev))

        return out

    def _check_async_blocking(
        self,
        fn: ast.AsyncFunctionDef,
        rel: str,
        added_lines: set[int],
        ev_fn: object,
    ) -> list[Finding]:
        out: list[Finding] = []
        # Walk statements without descending into nested sync defs
        stack: list[ast.AST] = list(fn.body)
        while stack:
            cur = stack.pop()
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(cur, ast.Call) and getattr(cur, "lineno", 0) in added_lines:
                dotted = _dotted_call_name(cur)
                if dotted in _ASYNC_BLOCKING_CALLS:
                    out.append(
                        _make_finding(
                            "py-async-blocking-call",
                            f"Synchronous blocking call `{dotted}(...)` inside `async def {fn.name}` blocks the event loop",
                            Severity.MAJOR,
                            source=self.name,
                            file=rel,
                            line=cur.lineno,
                            suggestion="Use `await asyncio.sleep(...)`, an async HTTP/subprocess API, or `await asyncio.to_thread(...)`.",
                            evidence=ev_fn(cur.lineno, dotted),  # type: ignore[operator]
                        )
                    )
            stack.extend(ast.iter_child_nodes(cur))
        return out

    def _check_loop_lambdas(
        self,
        stmts: list[ast.stmt],
        loop_vars: set[str],
        rel: str,
        added_lines: set[int],
        ev_fn: object,
    ) -> list[Finding]:
        out: list[Finding] = []
        for st in stmts:
            for sub in ast.walk(st):
                if isinstance(sub, ast.Lambda) and getattr(sub, "lineno", 0) in added_lines:
                    out.extend(self._inspect_lambda_capture(sub, loop_vars, rel, ev_fn))
        return out

    def _inspect_lambda_capture(
        self,
        lam: ast.Lambda,
        loop_vars: set[str],
        rel: str,
        ev_fn: object,
    ) -> list[Finding]:
        param_names = {a.arg for a in (*lam.args.posonlyargs, *lam.args.args, *lam.args.kwonlyargs)}
        captured = {
            n.id
            for n in ast.walk(lam.body)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in loop_vars and n.id not in param_names
        }
        if not captured:
            return []
        var = sorted(captured)[0]
        return [
            _make_finding(
                "py-late-binding-closure",
                f"Lambda captures loop variable '{var}' by reference (all closures will see final iteration value)",
                Severity.MAJOR,
                source=self.name,
                file=rel,
                line=lam.lineno,
                suggestion=f"Bind current iteration value via default argument: `lambda {var}={var}: ...`.",
                evidence=ev_fn(lam.lineno, f"lambda: ... {var}"),  # type: ignore[operator]
            )
        ]
