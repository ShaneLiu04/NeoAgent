"""Post-Release User Feedback & Telemetry Triage -> Next-Iteration Loop Spawner.

Closes the product-engineering loop:
  Ingest (JSONL / CSV / Webhook / Rollback alerts)
  -> CJK Bigram + English Token Jaccard Clustering
  -> Release-Window Regression Detection
  -> Reach * Impact * Source-Confidence Scoring
  -> Deterministic 5-Way Decision:
       * HOTFIX          -> Immediately spawns high-priority child Loop
       * NEXT_ITERATION  -> Spawns next-sprint child Loop + draft SRS skeleton
       * DOCS            -> Usage/documentation gap (do not bloat code!)
       * BACKLOG         -> Low-reach signal held for observation
       * KEEP            -> Positive user signal preserved as regression baseline
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import FeedbackPolicyConfig

SOURCE_CONFIDENCE: dict[str, float] = {
    "monitoring": 1.0,
    "alert": 1.0,
    "ticket": 0.9,
    "chatops": 0.75,
    "im": 0.75,
    "chat": 0.7,
    "survey": 0.6,
}

KIND_IMPACT: dict[str, float] = {
    "bug": 3.0,
    "performance": 2.5,
    "feature": 2.0,
    "ux": 1.5,
    "question": 0.5,
    "praise": 0.0,
}


@dataclass
class FeedbackItem:
    text: str
    source: str = "ticket"
    severity_hint: str = ""
    user_impact: int = 1
    ts: float = 0.0
    version: str = ""
    id: str = ""


@dataclass
class FeedbackCluster:
    items: list[FeedbackItem] = field(default_factory=list)
    kind: str = "question"
    severity: str = "minor"
    reach: int = 0
    score: float = 0.0
    suspected_regression: bool = False
    decision: str = "BACKLOG"
    rationale: str = ""
    tokens: set[str] = field(default_factory=set)

    @property
    def title(self) -> str:
        best = max(self.items, key=lambda i: (i.user_impact, -len(i.text)))
        return best.text.strip().replace("\n", " ")[:80]

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "kind": self.kind,
            "severity": self.severity,
            "reach": self.reach,
            "score": round(self.score, 2),
            "suspected_regression": self.suspected_regression,
            "decision": self.decision,
            "rationale": self.rationale,
            "size": len(self.items),
        }


def _parse_ts(val: Any) -> float:
    if val in (None, ""):
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    try:
        return float(s)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def feedback_item_from_dict(d: dict[str, Any], idx: int = 0) -> FeedbackItem | None:
    text = str(d.get("text") or d.get("content") or d.get("message") or "").strip()
    if not text:
        return None
    try:
        impact = max(1, int(float(d.get("user_impact") or d.get("count") or 1)))
    except ValueError:
        impact = 1
    return FeedbackItem(
        text=text,
        source=str(d.get("source") or "ticket").lower(),
        severity_hint=str(d.get("severity") or "").lower(),
        user_impact=impact,
        ts=_parse_ts(d.get("ts") or d.get("time") or d.get("created_at")),
        version=str(d.get("version") or ""),
        id=str(d.get("id") or f"fb-{idx}"),
    )


def parse_feedback_payload(raw: str, suffix: str = ".jsonl") -> list[FeedbackItem]:
    rows: list[dict[str, Any]] = []
    stripped = raw.strip()
    if suffix == ".csv":
        rows = list(csv.DictReader(io.StringIO(raw)))
    elif stripped.startswith("["):
        arr = json.loads(stripped)
        rows = [r for r in arr if isinstance(r, dict)]
    else:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                obj = {"text": line}
            if isinstance(obj, dict):
                rows.append(obj)
    return [it for idx, row in enumerate(rows) if (it := feedback_item_from_dict(row, idx))]


def load_feedback_file(path: Path) -> list[FeedbackItem]:
    raw = path.read_text(encoding="utf-8-sig")
    return parse_feedback_payload(raw, path.suffix.lower())


_STOPWORDS = {
    "the", "a", "an", "is", "are", "to", "of", "and", "in", "on", "it", "this", "that", "my", "for", "with",
    "的", "了", "是", "我", "在", "有", "和", "就", "都", "也", "很", "吗", "呢", "啊",
}
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_WORD_RE = re.compile(r"[a-z0-9_]{2,}")


def tokenize_bilingual(text: str) -> set[str]:
    lowered = text.lower()
    tokens: set[str] = set()
    for run in _CJK_RE.findall(lowered):
        cleaned = "".join(ch for ch in run if ch not in _STOPWORDS)
        if len(cleaned) == 1:
            tokens.add(cleaned)
        tokens.update(cleaned[i : i + 2] for i in range(len(cleaned) - 1))
    for w in _WORD_RE.findall(lowered):
        if w not in _STOPWORDS:
            tokens.add(w)
    return tokens


def jaccard_similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cluster_feedback(items: list[FeedbackItem], threshold: float = 0.3) -> list[FeedbackCluster]:
    clusters: list[FeedbackCluster] = []
    member_sets: list[list[set[str]]] = []
    for it in items:
        toks = tokenize_bilingual(it.text)
        best_idx, best_sim = -1, 0.0
        for ci, members in enumerate(member_sets):
            sim = max(jaccard_similarity(toks, m) for m in members)
            if sim >= threshold and sim > best_sim:
                best_idx, best_sim = ci, sim
        if best_idx >= 0:
            clusters[best_idx].items.append(it)
            clusters[best_idx].tokens |= toks
            member_sets[best_idx].append(toks)
        else:
            clusters.append(FeedbackCluster(items=[it], tokens=set(toks)))
            member_sets.append([toks])
    return clusters


_KIND_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("performance", re.compile(r"(?i)慢|卡顿|卡死|延迟|加载.*久|\bslow\b|\blag(gy)?\b|latency|sluggish|timeout")),
    (
        "bug",
        re.compile(
            r"(?i)崩溃|闪退|报错|错误|失败|无法|不能用|打不开|异常|白屏|回滚|"
            r"bug|crash|error|fail|exception|broken|\b5\d\d\b|doesn'?t work|not working"
        ),
    ),
    (
        "feature",
        re.compile(
            r"(?i)希望|建议|能不能|能否|增加|支持.*吗|想要|需要.*功能|"
            r"feature|request|wish|would be nice|please add|add support"
        ),
    ),
    ("ux", re.compile(r"(?i)难用|不直观|找不到|看不懂|太复杂|不清楚|confusing|hard to (find|use)|unclear")),
    ("question", re.compile(r"(?i)怎么|如何|为什么|请问|how (to|do|can)|\bwhy\b|\?|？")),
    ("praise", re.compile(r"(?i)好用|很棒|喜欢|赞|不错|丝滑|great|love|awesome|thanks|nice")),
]

_CRITICAL_RE = re.compile(
    r"(?i)崩溃|闪退|数据丢失|丢数据|宕机|无法登录|登不上|资损|泄露|越权|回滚|"
    r"crash|data loss|outage|cannot log ?in|can'?t log ?in|security|leak|rolled back"
)


def classify_feedback_kind(text: str) -> str:
    for kind, pat in _KIND_RULES:
        if pat.search(text):
            return kind
    return "question"


def classify_cluster_severity(items: list[FeedbackItem], kind: str) -> str:
    if kind not in ("bug", "performance"):
        return "minor" if kind != "praise" else "info"
    for it in items:
        if _CRITICAL_RE.search(it.text) or it.severity_hint in ("critical", "blocker", "p0", "sev1"):
            return "critical"
    if any(it.severity_hint in ("major", "high", "p1", "sev2") for it in items):
        return "major"
    return "major" if kind == "bug" else "minor"


def _pick_dominant_kind(votes: dict[str, int]) -> str:
    total = sum(votes.values())
    for k in ("bug", "performance"):
        if votes.get(k, 0) * 2 >= total:
            return k
    return max(votes, key=lambda k: (votes[k], KIND_IMPACT.get(k, 0.0)))


def _detect_regression(
    cluster: FeedbackCluster, release_ts: float | None, released_version: str
) -> bool:
    if cluster.kind not in ("bug", "performance"):
        return False
    versions = {it.version for it in cluster.items if it.version}
    if released_version and versions:
        return versions == {released_version}
    if release_ts:
        stamped = [it.ts for it in cluster.items if it.ts]
        return bool(stamped) and min(stamped) >= release_ts
    return False


def triage_feedback(
    items: list[FeedbackItem],
    cfg: FeedbackPolicyConfig,
    *,
    release_ts: float | None = None,
    released_version: str = "",
) -> list[FeedbackCluster]:
    clusters = cluster_feedback(items, cfg.cluster_threshold)
    for c in clusters:
        votes: dict[str, int] = {}
        for it in c.items:
            k = classify_feedback_kind(it.text)
            votes[k] = votes.get(k, 0) + it.user_impact
        c.kind = _pick_dominant_kind(votes)
        c.severity = classify_cluster_severity(c.items, c.kind)
        c.reach = sum(it.user_impact for it in c.items)
        avg_conf = sum(SOURCE_CONFIDENCE.get(it.source, 0.5) for it in c.items) / len(c.items)
        impact = KIND_IMPACT.get(c.kind, 1.0) * (1.5 if c.severity == "critical" else 1.0)
        c.score = c.reach * impact * avg_conf
        c.suspected_regression = _detect_regression(c, release_ts, released_version)

        if (
            c.kind in ("bug", "performance")
            and c.suspected_regression
            and (c.severity == "critical" or c.reach >= cfg.hotfix_min_reach)
        ):
            c.decision = "HOTFIX"
            c.rationale = f"Post-release regression affecting {c.reach} user(s) ({c.severity})"
        elif c.kind in ("bug", "performance"):
            c.decision = "NEXT_ITERATION"
            c.rationale = f"Defect/latency issue affecting {c.reach} user(s)"
        elif c.kind in ("feature", "ux"):
            if c.reach >= 2:
                c.decision = "NEXT_ITERATION"
                c.rationale = f"Validated demand from {c.reach} users"
            else:
                c.decision = "BACKLOG"
                c.rationale = "Single-user request; parked in backlog for signal validation"
        elif c.kind == "question":
            c.decision = "DOCS"
            c.rationale = "User onboarding/documentation gap — update docs, do not complicate code"
        else:
            c.decision = "KEEP"
            c.rationale = "Positive user validation — protect behavior in regression suite"

    clusters.sort(key=lambda c: (c.decision != "HOTFIX", -c.score))
    return clusters


def make_rollback_feedback_item(version: str, reason: str, ts: float = 0.0) -> FeedbackItem:
    return FeedbackItem(
        text=f"Canary release {version} automatically rolled back: {reason}",
        source="monitoring",
        severity_hint="critical",
        user_impact=10,
        ts=ts,
        version=version,
        id="canary-rollback",
    )


def render_triage_report(loop_id: str, title: str, clusters: list[FeedbackCluster]) -> str:
    lines = [f"# Next-Iteration Roadmap & Triage Report (Parent {loop_id}: {title})", ""]
    sections = [
        ("HOTFIX", "Immediate Hotfix Loops"),
        ("NEXT_ITERATION", "Next-Iteration Feature/Fix Loops"),
        ("DOCS", "Documentation & UX Guidance Updates"),
        ("BACKLOG", "Backlog Watchlist"),
        ("KEEP", "Positive Signals (Protect in Regression Tests)"),
    ]
    for key, heading in sections:
        group = [c for c in clusters if c.decision == key]
        if not group:
            continue
        lines.append(f"## {heading} ({len(group)})")
        lines.append("")
        lines.append("| Theme | Kind | Severity | Reach | Score | Rationale |")
        lines.append("|---|---|---|---|---|---|")
        for c in group:
            reg = " (regression)" if c.suspected_regression else ""
            safe_title = c.title.replace("|", "/")
            lines.append(
                f"| {safe_title} | {c.kind}{reg} | {c.severity} | {c.reach} | {c.score:.1f} | {c.rationale} |"
            )
        lines.append("")
    if len(lines) == 2:
        lines.append("No actionable feedback items ingested.")
    return "\n".join(lines) + "\n"


def render_srs_draft(child_loop_id: str, parent_id: str, cluster: FeedbackCluster) -> str:
    """Auto-generate an SDD-compliant ``srs-draft.md`` for spawned child loops."""
    samples = "\n".join(f"- [{it.source}] {it.text}" for it in cluster.items[:5])
    return (
        f"# Software Requirements Specification Draft ({child_loop_id})\n\n"
        f"- **Origin**: Spawned from `{parent_id}` via online feedback triage (`{cluster.decision}`)\n"
        f"- **Category**: `{cluster.kind}` | **Severity**: `{cluster.severity}` | **User Reach**: {cluster.reach}\n"
        f"- **Rationale**: {cluster.rationale}\n\n"
        f"## 1. User Evidence\n{samples}\n\n"
        f"## 2. Acceptance Criteria (Testable)\n"
        f"- [ ] AC-1: Reproduce or cover '{cluster.title}' with an automated failing test first (RED).\n"
        f"- [ ] AC-2: Implement minimal clean fix/feature passing all existing and new tests (GREEN).\n"
        f"- [ ] AC-3: Zero Critical/Major review findings and canary health SLOs remain green.\n"
    )
