"""DORA Four Keys + AI Engineering Autonomy Metrics.

Computed purely from immutable Loop history & hash-chained ledger records —
100% auditable and reproducible without external BI databases.
"""

from __future__ import annotations

from statistics import mean, median
from typing import Any

from .models import LoopRecord, Stage


def _first_stage_ts(loop: LoopRecord, target: Stage) -> float | None:
    for h in loop.history:
        if h.get("to") == target.value:
            return float(h["at"])
    return None


def _reached(loop: LoopRecord, target: Stage) -> bool:
    return _first_stage_ts(loop, target) is not None


def _to_hours(seconds: float) -> float:
    return round(seconds / 3600.0, 2)


def compute_devops_metrics(
    loops: list[LoopRecord],
    *,
    now: float | None = None,
    chain_verified: bool | None = None,
) -> dict[str, Any]:
    by_id = {lp.id: lp for lp in loops}
    shipped = [lp for lp in loops if _reached(lp, Stage.OBSERVE)]
    rolled = [lp for lp in loops if _reached(lp, Stage.ROLLED_BACK)]
    released = shipped + [lp for lp in rolled if lp not in shipped]

    # 1. DORA Metrics
    lead_times = [
        _first_stage_ts(lp, Stage.OBSERVE) - lp.created_at  # type: ignore[operator]
        for lp in shipped
    ]
    hotfix_parents = {
        lp.parent_id
        for lp in loops
        if lp.parent_id and lp.evidence.get("kind") == "hotfix"
    }
    failed_ids = {lp.id for lp in rolled} | {p for p in hotfix_parents if p in by_id}
    cfr = (len(failed_ids) / len(released)) if released else None

    mttr_samples: list[float] = []
    for lp in rolled:
        t_rel = _first_stage_ts(lp, Stage.RELEASE)
        t_rb = _first_stage_ts(lp, Stage.ROLLED_BACK)
        if t_rel is not None and t_rb is not None:
            mttr_samples.append(max(0.0, t_rb - t_rel))
    for lp in loops:
        if lp.evidence.get("kind") == "hotfix" and lp.parent_id in by_id:
            t_rel = _first_stage_ts(by_id[lp.parent_id], Stage.RELEASE)
            t_fix = _first_stage_ts(lp, Stage.OBSERVE)
            if t_rel is not None and t_fix is not None:
                mttr_samples.append(max(0.0, t_fix - t_rel))

    deploys_per_week = None
    if shipped:
        end_ts = now or max(lp.updated_at for lp in loops)
        # A one-day floor keeps a burst of same-hour releases from extrapolating to absurd rates.
        span_s = max(86400.0, end_ts - min(lp.created_at for lp in loops))
        deploys_per_week = round(len(shipped) / (span_s / (7 * 86400)), 2)

    # 2. AI Engineering Companion Autonomy & Self-Heal Metrics
    reached_pipe = [lp for lp in loops if _reached(lp, Stage.PIPELINE)]
    escalated = [lp for lp in loops if _reached(lp, Stage.ESCALATED)]
    auto_converged = [lp for lp in reached_pipe if not _reached(lp, Stage.ESCALATED)]
    needed_repair = [lp for lp in loops if (lp.review_rounds > 0 or lp.pipeline_fixes > 0)]
    self_healed = [
        lp for lp in needed_repair if _reached(lp, Stage.RELEASE) and not _reached(lp, Stage.ESCALATED)
    ]
    fb_shipped = [
        lp for lp in loops if lp.origin in ("feedback", "rollback") and _reached(lp, Stage.OBSERVE)
    ]
    fb_cycles = [
        _first_stage_ts(lp, Stage.OBSERVE) - lp.created_at  # type: ignore[operator]
        for lp in fb_shipped
    ]
    total_cost = round(sum(lp.total_cost_usd for lp in loops), 6)
    review_quality = _review_quality(loops)

    return {
        "loops_total": len(loops),
        "audit_chain_verified": chain_verified,
        "dora": {
            "lead_time_hours_median": _to_hours(median(lead_times)) if lead_times else None,
            "deploys_per_week": deploys_per_week,
            "change_failure_rate": round(cfr, 3) if cfr is not None else None,
            "mttr_hours_mean": _to_hours(mean(mttr_samples)) if mttr_samples else None,
        },
        "autonomy": {
            "avg_review_rounds": round(mean(lp.review_rounds for lp in reached_pipe), 2) if reached_pipe else None,
            "avg_pipeline_fixes": round(mean(lp.pipeline_fixes for lp in reached_pipe), 2) if reached_pipe else None,
            "auto_converge_rate": round(len(auto_converged) / len(reached_pipe), 3) if reached_pipe else None,
            "self_heal_rate": round(len(self_healed) / len(needed_repair), 3) if needed_repair else None,
            "escalation_rate": round(len(escalated) / len(loops), 3) if loops else None,
            "rollback_count": len(rolled),
            "feedback_to_fix_hours_median": _to_hours(median(fb_cycles)) if fb_cycles else None,
            "total_ai_cost_usd": total_cost,
        },
        "review_quality": review_quality,
        "stages": {s.value: sum(1 for lp in loops if lp.stage == s) for s in Stage},
    }


def _review_quality(loops: list[LoopRecord]) -> dict[str, Any]:
    """How trustworthy the reviews themselves were (Governed Review Protocol aggregates)."""
    rounds = [r for lp in loops for r in lp.evidence.get("review_protocol", [])]
    atts = [a for lp in loops for a in lp.evidence.get("review_attestations", [])]
    if not rounds:
        return {"rounds": 0}
    introduced = sum(r.get("introduced", 0) for r in rounds)
    carried = sum(r.get("carried_over", 0) for r in rounds)
    coverage = [r["citation_coverage"] for r in rounds if r.get("citation_coverage") is not None]
    verdicts: dict[str, int] = {}
    for r in rounds:
        verdicts[r.get("verdict", "?")] = verdicts.get(r.get("verdict", "?"), 0) + 1
    return {
        "rounds": len(rounds),
        "isolated_rate": round(sum(1 for a in atts if a.get("isolated")) / len(atts), 3) if atts else None,
        "introduced": introduced,
        "carried_over": carried,
        "fixed_by_change": sum(r.get("fixed_by_change", 0) for r in rounds),
        "blame_precision": round(introduced / (introduced + carried), 3) if introduced + carried else None,
        "ungrounded_dropped": sum(r.get("ungrounded_dropped", 0) for r in rounds),
        "avg_citation_coverage": round(mean(coverage), 3) if coverage else None,
        "verdicts": verdicts,
    }


def render_devops_metrics_markdown(m: dict[str, Any]) -> str:
    def _fmt(v: Any, suffix: str = "") -> str:
        return "n/a" if v is None else f"{v}{suffix}"

    d, a = m["dora"], m["autonomy"]
    chain = m.get("audit_chain_verified")
    chain_str = "VERIFIED (SHA-256 intact)" if chain else ("UNVERIFIED" if chain is None else "TAMPERED!")
    stage_summary = " | ".join(f"{k}:{v}" for k, v in m["stages"].items() if v) or "empty"
    return (
        f"# NeoAgent AI DevOps Companion Metrics ({m['loops_total']} Loop(s))\n"
        f"- **Audit Ledger Integrity**: {chain_str}\n\n"
        f"## DORA Engineering Velocity & Stability\n"
        f"- **Lead Time for Changes (median)**: {_fmt(d['lead_time_hours_median'], ' h')}\n"
        f"- **Deployment Frequency**: {_fmt(d['deploys_per_week'], ' / week')}\n"
        f"- **Change Failure Rate (CFR)**: {_fmt(d['change_failure_rate'])}\n"
        f"- **Mean Time to Recovery (MTTR)**: {_fmt(d['mttr_hours_mean'], ' h')}\n\n"
        f"## AI Companion Autonomy & Self-Healing\n"
        f"- **Autonomous Convergence Rate**: {_fmt(a['auto_converge_rate'])}\n"
        f"- **Bug/CI Self-Heal Success Rate**: {_fmt(a['self_heal_rate'])}\n"
        f"- **Avg Review-Fix Rounds**: {_fmt(a['avg_review_rounds'])}\n"
        f"- **Avg Pipeline-Fix Rounds**: {_fmt(a['avg_pipeline_fixes'])}\n"
        f"- **Human Escalation Rate**: {_fmt(a['escalation_rate'])}\n"
        f"- **Canary Auto-Rollbacks**: {a['rollback_count']}\n"
        f"- **Feedback-to-Production Cycle (median)**: {_fmt(a['feedback_to_fix_hours_median'], ' h')}\n"
        f"- **Cumulative AI Spend**: ${a['total_ai_cost_usd']:.4f}\n\n"
        f"{_render_review_quality(m.get('review_quality') or {})}"
        f"## Active Stage Distribution\n"
        f"{stage_summary}\n"
    )


def _render_review_quality(q: dict[str, Any]) -> str:
    if not q.get("rounds"):
        return ""
    def _f(v: Any) -> str:
        return "n/a" if v is None else str(v)

    return (
        "## Review Trustworthiness (Governed Review Protocol)\n"
        f"- **Reviews attested**: {q['rounds']} round(s), isolation rate {_f(q['isolated_rate'])}\n"
        f"- **Blame precision** (introduced / all reported): {_f(q['blame_precision'])} "
        f"({q['introduced']} introduced, {q['carried_over']} carried over, "
        f"{q['fixed_by_change']} fixed by the changes)\n"
        f"- **Ungrounded LLM findings dropped**: {q['ungrounded_dropped']}\n"
        f"- **Blocking findings with a cited standard**: {_f(q['avg_citation_coverage'])}\n"
        f"- **Verdicts**: {q['verdicts']}\n\n"
    )
