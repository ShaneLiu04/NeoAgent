"""Evidence grounding — an LLM finding must point at code that is really in the diff.

Generative reviewers are fluent and occasionally wrong in a particular way: they describe a
plausible defect on a line that is not there, or in a file that was never touched. Left alone,
those findings enter the repair loop and send the fixer chasing ghosts (or worse, "fixing"
healthy code). The cure is mechanical:

1. The finding's file must be one of the files in the diff.
2. The offending line must be an *added* line of that file (a small tolerance repairs the
   off-by-one drift typical of LLM line numbers and snaps the finding to the real line).
3. If the reviewer quotes the offending code, the quote must occur in the added lines.

Findings that cannot be grounded are dropped and *counted* — the per-source drop rate is a
direct, auditable measure of how much a reviewer hallucinates.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Finding
from ..reviewers import parse_diff

#: Source labels of generative reviewers. Deterministic reviewers are grounded by construction.
LLM_SOURCE_PREFIXES = ("neoagent-skills", "llm")


def is_llm_source(source: str) -> bool:
    return source.startswith(LLM_SOURCE_PREFIXES)


@dataclass
class GroundingReport:
    kept: list[Finding] = field(default_factory=list)
    dropped: list[tuple[Finding, str]] = field(default_factory=list)
    snapped: int = 0
    by_source: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "kept": len(self.kept),
            "dropped": len(self.dropped),
            "snapped": self.snapped,
            "drop_reasons": _count([reason for _, reason in self.dropped]),
            "by_source": self.by_source,
        }


def _count(items: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return out


def _norm_path(path: str) -> str:
    p = path.replace("\\", "/").strip()
    for prefix in ("./", "a/", "b/"):
        if p.startswith(prefix):
            p = p[len(prefix):]
    return p


def _squash(text: str) -> str:
    return "".join(text.split())


def ground_findings(
    findings: list[Finding], diff_text: str, *, tolerance: int = 2
) -> GroundingReport:
    added: dict[str, dict[int, str]] = {}
    files: set[str] = set()
    for dl in parse_diff(diff_text):
        if not dl.file:
            continue
        files.add(dl.file)
        if dl.added and dl.line > 0:
            added.setdefault(dl.file, {})[dl.line] = dl.text

    report = GroundingReport()

    def _tally(source: str, key: str) -> None:
        bucket = report.by_source.setdefault(source, {"total": 0, "dropped": 0})
        bucket[key] += 1

    for f in findings:
        if not is_llm_source(f.source):
            report.kept.append(f)
            continue
        _tally(f.source, "total")

        target = _resolve_file(f.file, files)
        if target is None:
            report.dropped.append((f, "file-not-in-diff"))
            _tally(f.source, "dropped")
            continue
        lines = added.get(target, {})
        if not lines:
            report.dropped.append((f, "no-added-lines"))
            _tally(f.source, "dropped")
            continue

        line = _locate(f, lines, tolerance)
        if line is None:
            reason = "evidence-not-in-diff" if f.evidence else "line-not-in-diff"
            report.dropped.append((f, reason))
            _tally(f.source, "dropped")
            continue

        if line != f.line:
            report.snapped += 1
        f.file, f.line = target, line
        if not f.evidence:
            f.evidence = lines[line].strip()[:200]
        report.kept.append(f)
    return report


def _resolve_file(name: str, files: set[str]) -> str | None:
    if not name:
        return None
    norm = _norm_path(name)
    if norm in files:
        return norm
    matches = [p for p in files if p.endswith("/" + norm) or norm.endswith("/" + p)]
    return matches[0] if len(matches) == 1 else None


def _locate(f: Finding, lines: dict[int, str], tolerance: int) -> int | None:
    if f.evidence:
        quote = _squash(f.evidence)
        if len(quote) >= 4:
            hits = [ln for ln, text in lines.items() if quote in _squash(text)]
            if not hits:
                return None
            if f.line in hits:
                return f.line
            return min(hits, key=lambda ln: abs(ln - f.line))
    if f.line in lines:
        return f.line
    near = [ln for ln in lines if abs(ln - f.line) <= tolerance]
    return min(near, key=lambda ln: abs(ln - f.line)) if near and f.line > 0 else None
