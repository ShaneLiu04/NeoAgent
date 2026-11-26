"""Review record — a review without a record did not happen.

Every round writes ``review-record-r<N>.md`` (for humans) and appends to
``review-record.json`` (for tooling). The record is the audit trail for the question a release
manager will eventually ask: *who looked at this, with what, and what became of each finding?*
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import Finding, FindingStatus, GateResult, LoopRecord
from .protocol import ReviewOutcome, Verdict


def _cell(text: object, limit: int = 90) -> str:
    s = str(text).replace("|", "\\|").replace("\n", " ").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _where(f: Finding) -> str:
    if not f.file:
        return "diff"
    return f"{f.file}:{f.line}" if f.line else f.file


def round_summary(
    loop: LoopRecord,
    round_no: int,
    outcome: ReviewOutcome,
    gate: GateResult,
    verdict: Verdict,
    *,
    density_suppressed: int,
    vanished_unverified: int,
    changed_lines: int,
) -> dict[str, Any]:
    return {
        "round": round_no,
        "verdict": verdict.value,
        "gate_passed": gate.passed,
        "star_rating": gate.star_rating,
        "changed_lines": changed_lines,
        "open_findings": len(loop.open_findings()),
        "density_suppressed": density_suppressed,
        "vanished_unverified": vanished_unverified,
        **outcome.stats(),
        "grounding": outcome.grounding,
        "attestation": outcome.attestation,
    }


def render_review_record(
    loop: LoopRecord, summary: dict[str, Any], outcome: ReviewOutcome, gate: GateResult
) -> str:
    att = summary["attestation"]
    verdict = summary["verdict"].upper()
    lines = [
        f"# Review Record — {loop.id} · round {summary['round']}",
        "",
        f"- **Verdict**: `{verdict}`  ·  **Quality**: {summary['star_rating']}★  ·  "
        f"**Risk tier**: `{loop.tier.value}`  ·  **Changed lines**: {summary['changed_lines']}",
        f"- **Gate**: {'passed' if gate.passed else 'not passed'}"
        + (f" — {gate.reasons[0][:140]}" if gate.reasons else ""),
        "",
        "## Reviewer independence (attested)",
        "",
        f"- Isolated from the author: **{'yes' if att.get('isolated') else 'NO'}**",
        f"- Reviewers: {', '.join(att.get('reviewers', [])) or 'none'}",
        f"- Inputs received: {', '.join(att.get('inputs', [])) or 'diff'}",
        f"- Input digest: `{att.get('input_digest', '')}`",
        f"- Reviewer sessions: {', '.join(att.get('reviewer_sessions', [])) or 'in-process (no LLM session)'}",
        f"- Author sessions: {', '.join(att.get('author_sessions', [])) or 'none recorded'}",
    ]
    if att.get("shared_sessions"):
        lines.append(f"- **Shared sessions (independence violated)**: {att['shared_sessions']}")
    lines += [
        "",
        "## What this change is responsible for",
        "",
        "| Introduced | Carried over (pre-existing, touched) | Fixed by this change | Security debt (untouched) |",
        "|---|---|---|---|",
        f"| {summary['introduced']} | {summary['carried_over']} | {summary['fixed_by_change']} | {summary['debt']} |",
        "",
    ]
    cov = summary.get("citation_coverage")
    if cov is not None:
        lines.append(f"Blocking findings with a cited standard: **{int(cov * 100)}%**.")
    g = summary.get("grounding") or {}
    if g.get("dropped"):
        lines.append(
            f"Ungrounded generative findings dropped: **{g['dropped']}** "
            f"(reasons: {g.get('drop_reasons', {})}); snapped to real lines: {g.get('snapped', 0)}."
        )
    if summary["vanished_unverified"]:
        lines.append(
            f"Findings not re-reported but whose code is unchanged (kept open): "
            f"**{summary['vanished_unverified']}**."
        )
    if summary["density_suppressed"]:
        lines.append(f"Minor/info findings suppressed by the density cap: {summary['density_suppressed']}.")

    lines += ["", "## Findings and their resolution", ""]
    if loop.findings:
        lines += [
            "| ID | Severity | Rule | Standard | Where | Attribution | Class | Status | Resolution |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for f in sorted(loop.findings, key=lambda x: (-x.severity.rank, x.file, x.line)):
            lines.append(
                f"| `{f.id}` | {f.severity.value} | `{_cell(f.rule, 32)}` | {_cell(f.standard, 28)} | "
                f"`{_cell(_where(f), 40)}` | {f.attribution or '-'} | {f.category.value} | "
                f"{f.status.value} | {_cell(f.resolution or ('—' if f.status == FindingStatus.OPEN else ''))} |"
            )
    else:
        lines.append("No findings.")

    if outcome.fixed_by_change:
        lines += ["", "## Removed by this change", ""]
        for f in outcome.fixed_by_change[:15]:
            lines.append(f"- `{f.rule}` @ `{_where(f)}` — {_cell(f.evidence, 80)}")
    if outcome.debt:
        lines += ["", "## Security debt found in touched files (non-blocking)", ""]
        for f in sorted(outcome.debt, key=lambda x: -x.severity.rank)[:20]:
            lines.append(f"- [{f.severity.value}] `{f.rule}` ({f.standard}) @ `{_where(f)}`")
    if summary["verdict"] in (Verdict.NEEDS_HUMAN.value, Verdict.REDESIGN.value):
        lines += ["", "See `decision-brief.md` for the questions that need an owner's answer."]
    return "\n".join(lines) + "\n"


def write_record(
    art_dir: Path,
    loop: LoopRecord,
    summary: dict[str, Any],
    outcome: ReviewOutcome,
    gate: GateResult,
) -> str:
    """Persist the round; returns the markdown file *name* (never an absolute path)."""
    art_dir.mkdir(parents=True, exist_ok=True)
    name = f"review-record-r{summary['round']}.md"
    (art_dir / name).write_text(render_review_record(loop, summary, outcome, gate), encoding="utf-8")

    json_path = art_dir / "review-record.json"
    rounds: list[dict[str, Any]] = []
    if json_path.is_file():
        try:
            rounds = json.loads(json_path.read_text(encoding="utf-8")).get("rounds", [])
        except (OSError, json.JSONDecodeError):
            rounds = []
    rounds = [r for r in rounds if r.get("round") != summary["round"]] + [summary]
    json_path.write_text(
        json.dumps({"loop": loop.id, "rounds": rounds}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return name
