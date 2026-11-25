"""Explainable change risk classification & approval policy matrix.

Autonomy scales inversely with blast radius:
  - LOW:    Can progress autonomously in UNATTENDED mode up to canary rollout.
  - MEDIUM: Requires human approval prior to production release.
  - HIGH:   Forces ATTENDED mode; requires human sign-off at review, release, and 100% rollout.
"""

from __future__ import annotations

import re
from functools import lru_cache

from .config import RiskPolicyConfig
from .models import LoopRecord, Mode, RiskTier


@lru_cache(maxsize=256)
def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


def match_any_glob(path: str, patterns: list[str]) -> str | None:
    norm = re.sub(r"^(?:\./)+", "", path.replace("\\", "/")).lstrip("/")
    for pat in patterns:
        if _glob_to_regex(pat).match(norm):
            return pat
    return None


def classify_change_risk(
    files: list[str],
    changed_lines: int,
    cfg: RiskPolicyConfig,
    diff_text: str = "",
) -> tuple[RiskTier, list[str]]:
    """Return ``(RiskTier, explainable_reasons)``."""
    tier = RiskTier.LOW
    reasons: list[str] = []

    def _bump(new_tier: RiskTier, why: str) -> None:
        nonlocal tier
        if new_tier.rank > tier.rank:
            tier = new_tier
        reasons.append(why)

    for f in files:
        high_hit = match_any_glob(f, cfg.high_paths)
        if high_hit:
            _bump(RiskTier.HIGH, f"'{f}' matched high-risk path pattern '{high_hit}'")
            continue
        med_hit = match_any_glob(f, cfg.medium_paths)
        if med_hit:
            _bump(RiskTier.MEDIUM, f"'{f}' matched medium-risk path pattern '{med_hit}'")

    if changed_lines >= cfg.high_lines:
        _bump(RiskTier.HIGH, f"Changed lines ({changed_lines}) >= high threshold ({cfg.high_lines})")
    elif changed_lines >= cfg.medium_lines:
        _bump(RiskTier.MEDIUM, f"Changed lines ({changed_lines}) >= medium threshold ({cfg.medium_lines})")

    # Content-based blast radius elevation (DDL / auth / crypto touches inside any file)
    if diff_text:
        if re.search(r"(?i)\b(DROP\s+TABLE|ALTER\s+TABLE|TRUNCATE\s+TABLE)\b", diff_text):
            _bump(RiskTier.HIGH, "Diff contains destructive SQL schema migration (DROP/ALTER/TRUNCATE)")
        if re.search(r"(?i)\b(CapabilityToken|SecretManager|api_key_middleware|jwt\.decode)\b", diff_text):
            if tier.rank < RiskTier.MEDIUM.rank:
                _bump(RiskTier.MEDIUM, "Diff modifies authentication/capability security boundary")

    return tier, reasons


def required_approvals(tier: RiskTier, mode: Mode) -> set[str]:
    if tier == RiskTier.HIGH:
        return {"review", "release", "full_rollout"}
    if tier == RiskTier.MEDIUM:
        return {"release"}
    return set() if mode == Mode.UNATTENDED else {"release"}


def missing_approvals(loop: LoopRecord, points: set[str]) -> list[str]:
    needed = required_approvals(loop.tier, loop.mode) & points
    return sorted(p for p in needed if not loop.has_approval(p))
