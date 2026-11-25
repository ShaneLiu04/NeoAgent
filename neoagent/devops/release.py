"""Progressive Canary Release Controller with Mandatory Rollback Guarantee.

Core SRE invariants:
1. "No rollback command, no right to deploy" — refuses live release if
   ``rollback_cmd`` is unconfigured.
2. Shell-injection-free argument tokenization (``shlex.split`` BEFORE ``str.format``).
3. Telemetry outage = immediate rollback (never fly blind).
4. Insufficient sample size = ``held`` (pause rollout, never assume healthy).
5. Re-entrant checkpointing: resumes from ``completed_percent`` after human
   ``full_rollout`` approval without re-running earlier canary stages.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ReleasePolicyConfig
from .gates import HealthSample, evaluate_canary_health

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}$")
POLLS_PER_STAGE = 3


class ReleaseError(Exception):
    """Raised on invalid release version or deployment configuration failure."""


def validate_release_version(version: str) -> str:
    if not _VERSION_RE.match(version or ""):
        raise ReleaseError(
            f"Illegal release version {version!r} (only [A-Za-z0-9._+-] allowed, blocks injection)"
        )
    return version


@dataclass
class ReleaseHooks:
    deploy: Callable[[str, int], None]
    rollback: Callable[[str], None]
    fetch_health: Callable[[str, int], HealthSample]
    fetch_baseline: Callable[[], HealthSample | None]
    sleep: Callable[[float], None] = time.sleep


@dataclass
class ReleaseState:
    version: str
    status: str = "in_progress"  # in_progress | promoted | rolled_back | awaiting_approval | held
    completed_percent: int = 0
    stages: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "status": self.status,
            "completed_percent": self.completed_percent,
            "stages": self.stages,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReleaseState:
        return cls(
            version=str(d["version"]),
            status=str(d.get("status", "in_progress")),
            completed_percent=int(d.get("completed_percent", 0)),
            stages=list(d.get("stages", [])),
            reason=str(d.get("reason", "")),
        )


def _trigger_rollback(
    state: ReleaseState, hooks: ReleaseHooks, reason: str
) -> ReleaseState:
    try:
        hooks.rollback(state.version)
        state.reason = reason
    except Exception as rb_err:
        state.reason = f"{reason}; CRITICAL: rollback also failed ({rb_err}) — page on-call!"
    state.status = "rolled_back"
    return state


def run_progressive_release(
    cfg: ReleasePolicyConfig,
    state: ReleaseState,
    hooks: ReleaseHooks,
    *,
    full_rollout_approved: bool = True,
) -> ReleaseState:
    validate_release_version(state.version)
    baseline = hooks.fetch_baseline()
    state.status = "in_progress"
    state.reason = ""

    for percent, soak_seconds in cfg.stages:
        if percent <= state.completed_percent:
            continue
        if percent == 100 and not full_rollout_approved:
            state.status = "awaiting_approval"
            state.reason = "Awaiting human sign-off before 100% rollout (approve --point full_rollout)"
            return state

        try:
            hooks.deploy(state.version, percent)
        except Exception as dep_err:
            return _trigger_rollback(
                state, hooks, f"Deployment at {percent}% failed: {dep_err}"
            )

        verdict_status = "insufficient_data"
        reasons: list[str] = []
        for _ in range(POLLS_PER_STAGE):
            hooks.sleep(soak_seconds / POLLS_PER_STAGE)
            try:
                sample = hooks.fetch_health(state.version, percent)
            except Exception as met_err:
                return _trigger_rollback(
                    state, hooks, f"Health telemetry unreachable at {percent}%: {met_err}"
                )

            verdict = evaluate_canary_health(sample, baseline, cfg.health)
            verdict_status, reasons = verdict.status, verdict.reasons
            if verdict.status == "unhealthy":
                state.stages.append(
                    {
                        "percent": percent,
                        "verdict": "unhealthy",
                        "sample": sample.to_dict(),
                        "reasons": reasons,
                    }
                )
                return _trigger_rollback(state, hooks, "; ".join(reasons))
            if verdict.status == "healthy":
                state.stages.append(
                    {
                        "percent": percent,
                        "verdict": "healthy",
                        "sample": sample.to_dict(),
                        "reasons": [],
                    }
                )
                break
        else:
            state.stages.append(
                {"percent": percent, "verdict": verdict_status, "reasons": reasons}
            )
            state.status = "held"
            state.reason = f"Canary held at {percent}% due to insufficient traffic: {'; '.join(reasons)}"
            return state

        state.completed_percent = percent

    state.status = "promoted"
    return state


def _exec_tokenized(template: str, cwd: Path, **fmt: Any) -> subprocess.CompletedProcess[str]:
    tokens = shlex.split(template, posix=(os.name != "nt"))
    formatted = [t.format(**fmt) for t in tokens]
    return subprocess.run(
        formatted,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )


def build_command_release_hooks(
    cfg: ReleasePolicyConfig,
    cwd: Path,
    *,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> ReleaseHooks:
    if not dry_run and not cfg.rollback_cmd.strip():
        raise ReleaseError(
            "Release refused: release.rollback_cmd is empty. No rollback capability, no deploy permission."
        )

    def deploy(version: str, percent: int) -> None:
        if dry_run:
            return
        if not cfg.deploy_cmd.strip():
            raise ReleaseError("release.deploy_cmd is not configured")
        r = _exec_tokenized(cfg.deploy_cmd, cwd, version=version, percent=percent)
        if r.returncode != 0:
            raise ReleaseError(f"deploy exited {r.returncode}: {(r.stderr or r.stdout)[-300:]}")

    def rollback(version: str) -> None:
        if dry_run:
            return
        r = _exec_tokenized(cfg.rollback_cmd, cwd, version=version)
        if r.returncode != 0:
            raise ReleaseError(f"rollback exited {r.returncode}: {(r.stderr or r.stdout)[-300:]}")

    def fetch_health(version: str, percent: int) -> HealthSample:
        if dry_run and not cfg.metrics_cmd.strip():
            return HealthSample(requests=max(500, cfg.health.min_requests), error_rate=0.001, p95_ms=95.0)
        if not cfg.metrics_cmd.strip():
            raise ReleaseError("release.metrics_cmd is not configured")
        r = _exec_tokenized(cfg.metrics_cmd, cwd, version=version, percent=percent)
        if r.returncode != 0:
            raise ReleaseError(f"metrics_cmd exited {r.returncode}")
        return HealthSample.from_dict(json.loads(r.stdout))

    def fetch_baseline() -> HealthSample | None:
        if dry_run and not cfg.metrics_cmd.strip():
            return HealthSample(requests=10_000, error_rate=0.002, p95_ms=100.0)
        if not cfg.metrics_cmd.strip():
            return None
        try:
            r = _exec_tokenized(cfg.metrics_cmd, cwd, version="baseline", percent=0)
            return HealthSample.from_dict(json.loads(r.stdout)) if r.returncode == 0 else None
        except Exception:
            return None

    return ReleaseHooks(deploy, rollback, fetch_health, fetch_baseline, sleep=sleep)
