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
        "stages": {s.value: sum(1 for lp in loops if lp.stage == s) for s in Stage},
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
        f"## Active Stage Distribution\n"
        f"{stage_summary}\n"
    )
