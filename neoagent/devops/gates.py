"""Deterministic Quality Gates — Review, CI Pipeline, and Canary Health.

Gates turn "can this change advance?" into pure, auditable functions rather
than probabilistic LLM self-assessments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import CanaryHealthConfig, ConvergenceBudgetConfig, PipelinePolicyConfig
from .models import FindingStatus, GateResult, LoopRecord, Severity
from .risk import missing_approvals


def compute_star_rating(loop: LoopRecord) -> int:
    """Compute 1-5 star code quality rating based on open finding severity distribution."""
    open_f = loop.open_findings()
    crit = sum(1 for f in open_f if f.severity == Severity.CRITICAL)
    maj = sum(1 for f in open_f if f.severity == Severity.MAJOR)
    minor = sum(1 for f in open_f if f.severity == Severity.MINOR)
    if crit > 0 or maj >= 3:
        return 2
    if maj > 0 or minor > 5:
        return 3
    if minor == 0 and loop.review_rounds <= 1:
        return 5
    return 4


def review_gate(loop: LoopRecord, budget: ConvergenceBudgetConfig) -> GateResult:
    reasons: list[str] = []
    warnings: list[str] = []

    open_f = loop.open_findings()
    blockers = [f for f in open_f if f.severity.rank >= Severity.MAJOR.rank]
    minors = [f for f in open_f if f.severity == Severity.MINOR]

    for f in blockers:
        reasons.append(f"[{f.severity.value}] {f.id} {f.rule} @ {f.file}:{f.line} — {f.message}")

    if len(minors) > budget.max_minor_findings:
        reasons.append(
            f"Open minor findings ({len(minors)}) exceed budget ({budget.max_minor_findings})"
        )
    elif minors:
        warnings.append(f"{len(minors)} minor finding(s) remain open (non-blocking)")

    for f in loop.findings:
        if f.status == FindingStatus.WAIVED and not f.resolution.strip():
            reasons.append(f"Finding {f.id} was marked WAIVED without signed justification")

    stars = compute_star_rating(loop)
    if reasons:
        return GateResult("review", False, reasons, warnings, star_rating=stars)

    miss = missing_approvals(loop, {"review"})
    if miss:
        return GateResult(
            "review",
            False,
            ["High-risk change passed automated review; awaiting human review sign-off"],
            warnings,
            awaiting_approval="review",
            star_rating=stars,
        )
    return GateResult("review", True, [], warnings, star_rating=stars)


def pipeline_gate(report: dict[str, Any], cfg: PipelinePolicyConfig) -> GateResult:
    reasons: list[str] = []
    warnings: list[str] = []
    steps = report.get("steps", [])
    if not steps:
        return GateResult(
            "pipeline",
            False,
            ["Pipeline has zero configured steps — empty pipeline never passes gate"],
            star_rating=2,
        )

    flaky_count = 0
    for s in steps:
        if s.get("flaky"):
            flaky_count += 1
            warnings.append(f"Step '{s['name']}' only passed on retry (flaky)")
        if s.get("status") != "passed" and s.get("required", True):
            reasons.append(f"Required step '{s['name']}' failed (exit={s.get('exit_code')})")

    if flaky_count > cfg.max_flaky:
        reasons.append(f"Flaky steps ({flaky_count}) exceed allowed maximum ({cfg.max_flaky})")

    cov = report.get("coverage")
    if cfg.coverage_min > 0:
        if cov is None:
            reasons.append(
                f"Coverage >= {cfg.coverage_min:g}% required, but no step emitted coverage"
            )
        elif cov < cfg.coverage_min:
            reasons.append(f"Test coverage {cov:g}% < required {cfg.coverage_min:g}%")

    return GateResult("pipeline", not reasons, reasons, warnings, star_rating=5 if not reasons else 2)


# ── Canary Health Gate ────────────────────────────────────────────────────────


@dataclass
class HealthSample:
    requests: int
    error_rate: float
    p95_ms: float

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> HealthSample:
        try:
            return cls(int(d["requests"]), float(d["error_rate"]), float(d["p95_ms"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Malformed canary health sample (expected requests/error_rate/p95_ms): {d!r}"
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "error_rate": self.error_rate,
            "p95_ms": self.p95_ms,
        }


@dataclass
class HealthVerdict:
    status: str  # healthy | unhealthy | insufficient_data
    reasons: list[str]


def evaluate_canary_health(
    canary: HealthSample, baseline: HealthSample | None, th: CanaryHealthConfig
) -> HealthVerdict:
    """Evaluate canary telemetry against absolute SLOs and baseline ratios.

    Insufficient sample size ALWAYS returns ``insufficient_data`` — never promote blindly.
    """
    if canary.requests < th.min_requests:
        return HealthVerdict(
            "insufficient_data",
            [f"Sample size {canary.requests} < min_requests {th.min_requests}; holding stage"],
        )

    reasons: list[str] = []
    if canary.error_rate > th.max_error_rate:
        reasons.append(
            f"Error rate {canary.error_rate:.2%} > max_error_rate {th.max_error_rate:.2%}"
        )
    if baseline is not None:
        allowed_err = max(
            baseline.error_rate * th.error_rate_ratio, min(th.max_error_rate, 0.001)
        )
        if canary.error_rate > allowed_err:
            reasons.append(
                f"Error rate {canary.error_rate:.2%} exceeds {th.error_rate_ratio:g}x "
                f"baseline ({baseline.error_rate:.2%})"
            )
        if baseline.p95_ms > 0 and canary.p95_ms > baseline.p95_ms * th.p95_ratio:
            reasons.append(
                f"p95 latency {canary.p95_ms:.0f}ms exceeds {th.p95_ratio:g}x "
                f"baseline ({baseline.p95_ms:.0f}ms)"
            )
    return HealthVerdict("unhealthy" if reasons else "healthy", reasons)
