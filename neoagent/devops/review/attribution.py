"""Change attribution — report what *this change* is responsible for, and nothing else.

A reviewer that flags every defect it can see on a touched line punishes authors for code they
did not write, and teaches them to ignore the review. The rule this module enforces is simple:

* ``introduced``    — the issue exists on the new side and had no counterpart on the old side.
* ``carried_over``  — the same issue was already there; the change only reformatted/renamed
                      around it. Reported, but never blocks a merge on its own.
* ``fixed``         — the issue existed on the old side and is gone on the new side. This is
                      credit: a change that removes three defects is not the same as one that
                      removes none.

The trick that keeps this cheap and reviewer-agnostic: invert the diff (``-`` becomes ``+``) and
run the *same deterministic reviewers* over it. The findings they produce describe the old code.
Matching old findings to new findings by (rule, file, similar line) gives all three buckets
without a second parser and without asking an LLM.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from ..models import Finding
from ..reviewers import parse_diff

_HUNK_SWAP_RE = re.compile(r"^@@ -(\S+) \+(\S+) @@(.*)$")

INTRODUCED = "introduced"
CARRIED_OVER = "carried_over"
PREEXISTING = "preexisting"  # deep-audit only: untouched by the change at all

#: Findings on lines shorter than this carry too little signal to call a "touch only" edit.
_MIN_TOUCH_LEN = 12
_MATCH_RATIO = 0.75
_TOUCH_RATIO = 0.97


def invert_diff(diff_text: str) -> str:
    """Return the diff as if the change were being un-applied (old code becomes the 'added' side)."""
    out: list[str] = []
    for raw in diff_text.splitlines():
        if raw.startswith("--- "):
            path = raw[4:].strip()
            out.append("+++ " + (("b/" + path[2:]) if path.startswith("a/") else path))
        elif raw.startswith("+++ "):
            path = raw[4:].strip()
            out.append("--- " + (("a/" + path[2:]) if path.startswith("b/") else path))
        elif raw.startswith("@@"):
            m = _HUNK_SWAP_RE.match(raw)
            out.append(f"@@ -{m.group(2)} +{m.group(1)} @@{m.group(3)}" if m else raw)
        elif raw.startswith("+"):
            out.append("-" + raw[1:])
        elif raw.startswith("-"):
            out.append("+" + raw[1:])
        else:
            out.append(raw)
    return "\n".join(out) + "\n"


def fill_evidence(findings: list[Finding], diff_text: str) -> None:
    """Attach the offending source line to findings that only know ``file:line``."""
    index: dict[tuple[str, int], str] = {}
    for dl in parse_diff(diff_text):
        if dl.added and dl.file and dl.line > 0:
            index[(dl.file, dl.line)] = dl.text
    for f in findings:
        if f.evidence or not f.file or f.line <= 0:
            continue
        text = index.get((f.file.replace("\\", "/"), f.line))
        if text is not None:
            f.evidence = text.strip()[:200]


def _norm(text: str) -> str:
    return " ".join(text.split())


def _squash(text: str) -> str:
    return "".join(text.split())


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


@dataclass
class AttributionResult:
    introduced: list[Finding] = field(default_factory=list)
    carried_over: list[Finding] = field(default_factory=list)
    fixed: list[Finding] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "introduced": len(self.introduced),
            "carried_over": len(self.carried_over),
            "fixed_by_change": len(self.fixed),
        }


def attribute_findings(
    fresh: list[Finding], old_side: list[Finding], diff_text: str
) -> AttributionResult:
    """Label ``fresh`` findings and return the issues the change removed.

    ``old_side`` are findings produced by running the deterministic reviewers over
    :func:`invert_diff` — i.e. the issues that lived in the code *before* the change.
    """
    removed_by_file: dict[str, list[str]] = {}
    for dl in parse_diff(diff_text):
        if not dl.added and dl.file:
            removed_by_file.setdefault(dl.file, []).append(_squash(dl.text))

    used: set[int] = set()
    result = AttributionResult()

    for n in fresh:
        if n.attribution == PREEXISTING:
            continue
        match: int | None = None
        best = 0.0
        if n.evidence:
            for i, o in enumerate(old_side):
                if i in used or o.rule != n.rule or o.file != n.file:
                    continue
                score = _ratio(o.evidence, n.evidence)
                if score >= _MATCH_RATIO and score > best:
                    match, best = i, score

        touch_only = False
        if match is None and len(n.evidence) >= _MIN_TOUCH_LEN:
            squashed = _squash(n.evidence)
            touch_only = any(
                difflib.SequenceMatcher(None, squashed, r).ratio() >= _TOUCH_RATIO
                for r in removed_by_file.get(n.file, ())
                if len(r) >= _MIN_TOUCH_LEN
            )

        if match is not None or touch_only:
            if match is not None:
                used.add(match)
            n.attribution = CARRIED_OVER
            result.carried_over.append(n)
        else:
            n.attribution = INTRODUCED
            result.introduced.append(n)

    result.fixed = [o for i, o in enumerate(old_side) if i not in used]
    return result
