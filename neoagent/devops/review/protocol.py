"""Governed Review Protocol — the rules that make a review trustworthy, not just a review.

Running reviewers is the easy part. What decides whether a review can be *believed* is what
happens around them. This module enforces five invariants, each one mechanical:

1. **Independence is recorded, not claimed.** Every round stores an attestation of exactly what
   the reviewers received (a digest of the diff + skill bundle), which sessions they ran in, and
   whether any of those sessions overlap with the author's. ``harness-audit`` reads it back.
2. **Evidence or silence.** LLM findings must land on real added lines (``grounding``); the
   rest are dropped and counted.
3. **Blame only what the change did.** Findings are attributed ``introduced`` /
   ``carried_over`` / ``fixed`` (``attribution``); carried-over issues cannot block a merge.
4. **Route by who can decide.** Findings the AI may repair go back to BUILD; findings that need
   a business fact or an architectural ruling escalate with a *decision brief* instead of letting
   the fixer guess. A problem that belongs upstream (spec/design) yields a ``REDESIGN`` verdict.
5. **A finding only disappears for a reason.** An LLM finding that is merely *not re-reported*
   while its offending code is untouched stays open — reviewer variance must not read as a fix.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import ReviewProtocolConfig
from ..models import Finding, FindingCategory, LoopRecord, Severity
from ..reviewers import Reviewer, parse_diff
from .attribution import (
    CARRIED_OVER,
    PREEXISTING,
    attribute_findings,
    fill_evidence,
    invert_diff,
)
from .catalog import resolve_rule
from .grounding import GroundingReport, ground_findings, is_llm_source
from .structural import ScopeDriftReviewer

#: Inputs that would break reviewer independence if a reviewer declared them.
_FORBIDDEN_INPUTS = frozenset({"author_transcript", "chat_history", "conversation", "author_reasoning"})


class Verdict(str, enum.Enum):
    PASS = "pass"                      # no blocking findings; may advance
    NEEDS_CHANGES = "needs_changes"    # the AI fixer can repair everything that blocks
    NEEDS_HUMAN = "needs_human"        # a business fact / ruling is required before more code
    REDESIGN = "redesign"              # the defect is upstream (spec/design); do not patch code


def collect_findings(
    reviewers: list[Reviewer], diff_text: str, repo_root: Path | None
) -> list[Finding]:
    """Run every reviewer; keep one finding per identity, preferring the most severe."""
    merged: dict[str, Finding] = {}
    for rev in reviewers:
        for f in rev.review(diff_text, repo_root=repo_root):
            fp = f.fingerprint()
            existing = merged.get(fp)
            if existing is None or f.severity.rank > existing.severity.rank:
                merged[fp] = f
    return list(merged.values())


def enrich_from_catalog(f: Finding) -> None:
    """Fill the citation, rework stage and routing class a reviewer did not state itself."""
    spec = resolve_rule(f.rule, f.message)
    if spec is None:
        return
    if not f.standard:
        f.standard = spec.standard
    if not f.rework and spec.rework != "build":
        f.rework = spec.rework
    if f.category == FindingCategory.LLM_FIXABLE and spec.triage != FindingCategory.LLM_FIXABLE.value:
        f.category = FindingCategory(spec.triage)


@dataclass
class ReviewOutcome:
    findings: list[Finding] = field(default_factory=list)
    debt: list[Finding] = field(default_factory=list)
    fixed_by_change: list[Finding] = field(default_factory=list)
    carried_over: int = 0
    grounding: dict[str, Any] = field(default_factory=dict)
    attestation: dict[str, Any] = field(default_factory=dict)

    def stats(self) -> dict[str, Any]:
        return {
            "introduced": sum(1 for f in self.findings if f.attribution != CARRIED_OVER),
            "carried_over": self.carried_over,
            "fixed_by_change": len(self.fixed_by_change),
            "debt": len(self.debt),
            "ungrounded_dropped": int(self.grounding.get("dropped", 0)),
            "citation_coverage": self._citation_coverage(),
        }

    def _citation_coverage(self) -> float | None:
        blocking = [f for f in self.findings if f.severity.rank >= Severity.MAJOR.rank]
        if not blocking:
            return None
        return round(sum(1 for f in blocking if f.standard) / len(blocking), 2)


class ReviewProtocol:
    def __init__(self, cfg: ReviewProtocolConfig, root: Path) -> None:
        self.cfg = cfg
        self.root = root

    # ── Orchestration ────────────────────────────────────────────────────────

    def contextual_reviewers(self, loop: LoopRecord, reviewers: list[Reviewer]) -> list[Reviewer]:
        """Reviewers that need the loop's own declarations (scope & Blueprint), added on top of the chain."""
        extra: list[Reviewer] = []
        if (
            self.cfg.scope_drift
            and loop.files
            and not any(isinstance(r, ScopeDriftReviewer) for r in reviewers)
        ):
            extra.append(ScopeDriftReviewer(loop.files))
        if self.cfg.sdc_compliance and loop.evidence.get("blueprint"):
            bp_dir = self.root / ".neoagent-devops" / "artifacts" / loop.id / "blueprint"
            if (bp_dir / "blueprint-state.json").is_file():
                extra.append(_BlueprintSDCReviewer(bp_dir))
        return extra

    def run(
        self, loop: LoopRecord, diff_text: str, reviewers: list[Reviewer], round_no: int
    ) -> ReviewOutcome:
        active_reviewers = (
            [r for r in reviewers if getattr(r, "name", "") != "clean-code"]
            if not self.cfg.clean_code
            else list(reviewers)
        )
        chain = [*active_reviewers, *self.contextual_reviewers(loop, active_reviewers)]
        raw = collect_findings(chain, diff_text, self.root)
        fill_evidence(raw, diff_text)

        outcome = ReviewOutcome()
        if self.cfg.ground_llm_findings:
            report: GroundingReport = ground_findings(raw, diff_text)
            raw = report.kept
            outcome.grounding = report.to_dict()

        for f in raw:
            enrich_from_catalog(f)

        outcome.debt = [f for f in raw if f.attribution == PREEXISTING]
        raw = [f for f in raw if f.attribution != PREEXISTING]

        if self.cfg.attribution:
            old_side = self._old_side(chain, diff_text)
            result = attribute_findings(raw, old_side, diff_text)
            outcome.fixed_by_change = result.fixed
            outcome.carried_over = len(result.carried_over)
            raw = self._apply_carried_policy(raw)

        outcome.findings = raw
        outcome.attestation = self.attest(loop, chain, diff_text, round_no)
        return outcome

    def _old_side(self, chain: list[Reviewer], diff_text: str) -> list[Finding]:
        """Run the deterministic, attributable reviewers over the *old* code (inverted diff)."""
        inverted = invert_diff(diff_text)
        old: list[Finding] = []
        for rev in chain:
            if not (getattr(rev, "deterministic", False) and getattr(rev, "attributable", False)):
                continue
            root = None if getattr(rev, "reads_worktree", False) else self.root
            try:
                old.extend(rev.review(inverted, repo_root=root))
            except Exception:  # noqa: BLE001 - a reviewer must never be able to break attribution
                continue
        fill_evidence(old, inverted)
        # Whole-diff integrity rules are meaningless when inverted (added asserts look "removed").
        return [o for o in old if o.line > 0 and o.rule != "test-weakening" and o.evidence]

    def _apply_carried_policy(self, findings: list[Finding]) -> list[Finding]:
        policy = self.cfg.carried_over_policy
        if policy == "keep":
            return findings
        kept: list[Finding] = []
        for f in findings:
            if f.attribution != CARRIED_OVER:
                kept.append(f)
            elif policy == "downgrade":
                if f.severity != Severity.CRITICAL:  # never silence a critical, even if pre-existing
                    f.severity = Severity.INFO
                kept.append(f)
            # policy == "drop": omitted
        return kept

    # ── Independence attestation ─────────────────────────────────────────────

    @staticmethod
    def attest(
        loop: LoopRecord, chain: list[Reviewer], diff_text: str, round_no: int
    ) -> dict[str, Any]:
        inputs: set[str] = set()
        reviewer_sessions: list[str] = []
        for rev in chain:
            inputs.update(getattr(rev, "context_inputs", ("diff",)))
            reviewer_sessions.extend(getattr(rev, "session_ids", []) or [])
        author_sessions = [str(s) for s in loop.evidence.get("author_sessions", [])]
        shared = sorted(set(reviewer_sessions) & set(author_sessions))
        forbidden = sorted(inputs & _FORBIDDEN_INPUTS)
        digest = hashlib.sha256(
            (diff_text + "\x00" + ",".join(sorted(loop.activated_skills))).encode("utf-8")
        ).hexdigest()[:16]
        return {
            "round": round_no,
            "reviewers": [r.name for r in chain],
            "inputs": sorted(inputs),
            "input_digest": digest,
            "reviewer_sessions": sorted(set(reviewer_sessions)),
            "author_sessions": author_sessions,
            "shared_sessions": shared,
            "forbidden_inputs": forbidden,
            "isolated": not shared and not forbidden,
        }


# ── Verdict & routing ───────────────────────────────────────────────────────


def decide_verdict(loop: LoopRecord, gate_passed: bool) -> Verdict:
    if gate_passed:
        return Verdict.PASS
    blockers = [f for f in loop.open_findings() if f.severity.rank >= Severity.MAJOR.rank]
    if any(f.rework in ("spec", "design") for f in blockers):
        return Verdict.REDESIGN
    if any(f.category.needs_human for f in blockers):
        return Verdict.NEEDS_HUMAN
    return Verdict.NEEDS_CHANGES


_DECISIONS: dict[str, tuple[str, str]] = {
    "scope-drift": (
        "Files outside the declared scope were changed. Is that intended?",
        "(a) extend the declared scope and record why; (b) revert those edits",
    ),
    "public-contract-change": (
        "A public function signature changed incompatibly. Is this a deliberate breaking change?",
        "(a) keep the old signature working; (b) accept the break and list every caller to update",
    ),
    "public-api-removed": (
        "A public function was removed. Does anything still depend on it?",
        "(a) restore it (or a deprecated shim); (b) confirm there are no callers and record the removal",
    ),
    "sdc-missing-interface": (
        "An interface contract declared in `design.md` is missing in code. Did the design change?",
        "(a) implement the declared interface symbol; (b) update `design.md` and re-pass Design Gate",
    ),
    "sdc-undeclared-file-touch": (
        "Production files outside the approved `design.md` / `tasks.md` plan were modified.",
        "(a) revert out-of-plan edits; (b) revise `design.md` & `tasks.md` to record the expanded blast radius",
    ),
}


class _BlueprintSDCReviewer:
    """Contextual 3D Spec / Design / Code compliance reviewer when a loop has an active Blueprint."""

    name = "blueprint-sdc"
    deterministic = True
    attributable = False
    reads_worktree = True
    context_inputs = ("diff", "blueprint_contracts", "worktree_sources")

    def __init__(self, blueprint_dir: Path) -> None:
        self.blueprint_dir = blueprint_dir

    def review(self, diff_text: str, repo_root: Path | None = None) -> list[Finding]:
        from ..blueprint import BlueprintPhaseGate, BlueprintWorkspace

        ws = BlueprintWorkspace(self.blueprint_dir)
        spec = ws.load_spec()
        plan = ws.load_plan()
        if spec is None or plan is None:
            return []
        _, findings = BlueprintPhaseGate.verify_sdc(
            spec=spec,
            design=ws.load_design(),
            plan=plan,
            trace=ws.load_traceability(),
            diff_text=diff_text,
            repo_root=repo_root,
        )
        return findings


def build_decision_brief(loop: LoopRecord, verdict: Verdict, round_no: int) -> str:
    """Phrase everything a human must decide as the *fewest* concrete questions."""
    pending = [
        f
        for f in loop.open_findings()
        if f.severity.rank >= Severity.MAJOR.rank and (f.category.needs_human or f.rework in ("spec", "design"))
    ]
    groups: dict[str, list[Finding]] = {}
    for f in pending:
        groups.setdefault(f.rule, []).append(f)

    lines = [
        f"# Decision Brief ({loop.id}, review round {round_no})",
        "",
        f"**Verdict: {verdict.value.upper()}** — automated repair is paused until the questions "
        "below are answered by an owner.",
        "",
    ]
    for n, (rule, items) in enumerate(groups.items(), start=1):
        question, options = _DECISIONS.get(
            rule,
            (
                f"Rule '{rule}' needs an owner's ruling: {items[0].message}",
                "(a) fix as suggested; (b) waive with a recorded justification",
            ),
        )
        spec = resolve_rule(rule)
        ref = f" · {spec.standard}" if spec and spec.standard else ""
        lines.append(f"## {n}. {question}{ref}")
        lines.append(f"- Options: {options}")
        for f in items[:8]:
            loc = f"{f.file}:{f.line}" if f.line else f.file or "diff"
            disputed = f" — disputed by author: {f.resolution}" if f.resolution.startswith("DISPUTED") else ""
            lines.append(f"- `{f.id}` `{loc}` — {f.message}{disputed}")
        if len(items) > 8:
            lines.append(f"- … and {len(items) - 8} more")
        lines.append("")
    lines.append(
        "Resolve with `neo-devops waive <loop> <finding-id> --by <you> --reason <why>` "
        "(accept as is), or fix the code and `neo-devops resume <loop> --by <you>`."
    )
    return "\n".join(lines) + "\n"


# ── Vanished-finding verification ───────────────────────────────────────────


def offending_code_unchanged(finding: Finding, diff_text: str) -> bool:
    """True if the finding's quoted code is still among the added lines of its file.

    Used on re-review: a generative reviewer that simply *did not repeat* a finding has not
    demonstrated a fix. Deterministic reviewers are exempt — their silence is real evidence.
    """
    if not finding.evidence or not is_llm_source(finding.source):
        return False
    quote = "".join(finding.evidence.split())
    if len(quote) < 4:
        return False
    return any(
        quote in "".join(dl.text.split())
        for dl in parse_diff(diff_text)
        if dl.added and dl.file == finding.file
    )
