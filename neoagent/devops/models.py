"""Domain models for the AI DevOps closed-loop state machine + SARIF/MR exporters."""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from typing import Any


class Stage(str, enum.Enum):
    """Strict lifecycle stages from feature intake to post-release learning."""

    INTAKE = "intake"          # Requirement spec, risk classification, skill routing
    BUILD = "build"            # TDD-first AI coding in isolated worktree/workspace
    REVIEW = "review"          # Static AST + BugPattern + Parallel Skill-LLM review
    PIPELINE = "pipeline"      # Automated build, lint, typecheck, test, coverage
    RELEASE = "release"        # Progressive canary rollout + health gate + auto-rollback
    OBSERVE = "observe"        # Post-release telemetry & user feedback observation window
    LEARN = "learn"            # Feedback clustering, regression triage, next-loop spawning
    DONE = "done"              # Closed loop complete
    ROLLED_BACK = "rolled_back"
    ESCALATED = "escalated"    # Convergence budget exhausted or human decision required


#: Allowed state transitions — mechanically prevents AI from skipping review or CI.
TRANSITIONS: dict[Stage, set[Stage]] = {
    Stage.INTAKE: {Stage.BUILD, Stage.ESCALATED},
    Stage.BUILD: {Stage.REVIEW, Stage.ESCALATED},
    Stage.REVIEW: {Stage.BUILD, Stage.PIPELINE, Stage.ESCALATED},
    Stage.PIPELINE: {Stage.BUILD, Stage.RELEASE, Stage.ESCALATED},
    Stage.RELEASE: {Stage.OBSERVE, Stage.ROLLED_BACK, Stage.ESCALATED},
    Stage.OBSERVE: {Stage.LEARN, Stage.ESCALATED},
    Stage.ROLLED_BACK: {Stage.LEARN},
    Stage.LEARN: {Stage.DONE},
    Stage.DONE: set(),
    Stage.ESCALATED: set(),  # Only human `resume` may exit ESCALATED
}

RESUMABLE: set[Stage] = {
    Stage.INTAKE,
    Stage.BUILD,
    Stage.REVIEW,
    Stage.PIPELINE,
    Stage.RELEASE,
    Stage.OBSERVE,
}


class RiskTier(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"low": 0, "medium": 1, "high": 2}[self.value]


class Mode(str, enum.Enum):
    ATTENDED = "attended"      # Human-on-the-loop checkpoints at required gates
    UNATTENDED = "unattended"  # Autonomous progression up to risk-policy boundaries


class Severity(str, enum.Enum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"

    @property
    def rank(self) -> int:
        return {"critical": 3, "major": 2, "minor": 1, "info": 0}[self.value]

    @property
    def sarif_level(self) -> str:
        return {
            "critical": "error",
            "major": "error",
            "minor": "warning",
            "info": "note",
        }[self.value]

    @classmethod
    def parse(cls, value: str | None, default: Severity | None = None) -> Severity:
        fallback = default or cls.MINOR
        if not value:
            return fallback
        norm = str(value).strip().lower()
        aliases = {
            "fatal": "critical",
            "blocker": "critical",
            "high": "critical",
            "error": "major",
            "important": "major",
            "medium": "major",
            "warning": "minor",
            "low": "minor",
            "suggestion": "info",
            "nit": "info",
        }
        norm = aliases.get(norm, norm)
        try:
            return cls(norm)
        except ValueError:
            return fallback


class FindingStatus(str, enum.Enum):
    OPEN = "open"
    FIXED = "fixed"      # Verified absent on re-review by independent engine
    WAIVED = "waived"    # Explicitly waived by named human with justification


class FindingCategory(str, enum.Enum):
    """Actionability taxonomy: decides whether an AI may repair a finding or a human must.

    Routing is mechanical (see ``review.protocol``): auto-fixable categories go back to BUILD
    with a repair brief; human-class categories escalate with a minimal decision brief instead of
    letting the fixer guess at business facts or architectural trade-offs.
    """

    LLM_FIXABLE = "llm_fixable"          # Deterministic/local code issue AI can repair
    ENV_BLOCKER = "env_blocker"          # Missing daemon/network/secret — halt, don't thrash code
    SPEC_MISMATCH = "spec_mismatch"      # Business expectation mismatch — never weaken assertions
    HUMAN_DECISION = "human_decision"    # Architecture/security waiver needing engineer judgment
    USER_INPUT = "user_input"            # A business fact, priority or threshold only the owner knows

    @property
    def auto_fixable(self) -> bool:
        return self in (FindingCategory.LLM_FIXABLE, FindingCategory.SPEC_MISMATCH)

    @property
    def needs_human(self) -> bool:
        return not self.auto_fixable


class FailureKind(str, enum.Enum):
    """Pipeline failure classification: code defect vs environment vs spec mismatch."""

    CODE_DEFECT = "code_defect"                      # Syntax/import/type/lint/unit bug -> auto-fix
    ENV_BLOCKER = "env_blocker"                      # Timeout/missing binary/network/DB down -> escalate
    SPEC_ASSERTION_MISMATCH = "spec_assertion_mismatch"  # Fix implementation, NEVER alter assertions


@dataclass
class Finding:
    rule: str
    message: str
    severity: Severity = Severity.MINOR
    file: str = ""
    line: int = 0
    source: str = "static"
    category: FindingCategory = FindingCategory.LLM_FIXABLE
    suggestion: str = ""
    status: FindingStatus = FindingStatus.OPEN
    resolution: str = ""
    first_seen_round: int = 1
    reopened_count: int = 0
    id: str = ""
    #: Citation of the rule's origin (e.g. ``CWE-89``); filled from the rule catalog.
    standard: str = ""
    #: The exact offending source line, so a finding can be checked against the diff.
    evidence: str = ""
    #: ``introduced`` (this change) | ``carried_over`` (pre-existing, merely touched) | ``""``.
    attribution: str = ""
    #: Where an upstream fix belongs when the code is not the real problem: build | spec | design.
    rework: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = "F-" + self.fingerprint()[:8]

    def fingerprint(self) -> str:
        """Line-number-independent identity so code edits shifting lines still match."""
        norm_msg = " ".join(self.message.lower().split())
        raw = f"{self.rule}|{self.file.replace(chr(92), '/')}|{norm_msg}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rule": self.rule,
            "message": self.message,
            "severity": self.severity.value,
            "file": self.file,
            "line": self.line,
            "source": self.source,
            "category": self.category.value,
            "suggestion": self.suggestion,
            "status": self.status.value,
            "resolution": self.resolution,
            "first_seen_round": self.first_seen_round,
            "reopened_count": self.reopened_count,
            "standard": self.standard,
            "evidence": self.evidence,
            "attribution": self.attribution,
            "rework": self.rework,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Finding:
        cat_raw = str(d.get("category", FindingCategory.LLM_FIXABLE.value))
        try:
            cat = FindingCategory(cat_raw)
        except ValueError:
            cat = FindingCategory.LLM_FIXABLE
        return cls(
            rule=str(d["rule"]),
            message=str(d["message"]),
            severity=Severity.parse(d.get("severity")),
            file=str(d.get("file", "")),
            line=int(d.get("line", 0) or 0),
            source=str(d.get("source", "static")),
            category=cat,
            suggestion=str(d.get("suggestion", "")),
            status=FindingStatus(d.get("status", "open")),
            resolution=str(d.get("resolution", "")),
            first_seen_round=int(d.get("first_seen_round", 1)),
            reopened_count=int(d.get("reopened_count", 0)),
            id=str(d.get("id", "")),
            standard=str(d.get("standard", "")),
            evidence=str(d.get("evidence", "")),
            attribution=str(d.get("attribution", "")),
            rework=str(d.get("rework", "")),
        )


@dataclass
class GateResult:
    gate: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    awaiting_approval: str | None = None
    star_rating: int = 4  # 1-5 star code quality score

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "passed": self.passed,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "awaiting_approval": self.awaiting_approval,
            "star_rating": self.star_rating,
        }


@dataclass
class StepResult:
    code: int
    stage: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "stage": self.stage,
            "message": self.message,
            **self.data,
        }


@dataclass
class LoopRecord:
    """Persistent state of one feature/bugfix delivery closed loop."""

    id: str
    title: str
    tier: RiskTier = RiskTier.LOW
    mode: Mode = Mode.ATTENDED
    stage: Stage = Stage.INTAKE
    created_at: float = 0.0
    updated_at: float = 0.0
    parent_id: str | None = None
    origin: str = "engineer"          # engineer | feedback | rollback | webhook
    spec_ref: str = ""
    base: str = "main"
    worktree_path: str = ""
    files: list[str] = field(default_factory=list)
    activated_skills: list[str] = field(default_factory=list)
    review_rounds: int = 0
    pipeline_fixes: int = 0
    total_cost_usd: float = 0.0
    findings: list[Finding] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    escalated_from: str | None = None
    escalation_reason: str = ""

    def open_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.status == FindingStatus.OPEN]

    def has_approval(self, point: str) -> bool:
        return any(a.get("point") == point for a in self.approvals)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "tier": self.tier.value,
            "mode": self.mode.value,
            "stage": self.stage.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "parent_id": self.parent_id,
            "origin": self.origin,
            "spec_ref": self.spec_ref,
            "base": self.base,
            "worktree_path": self.worktree_path,
            "files": list(self.files),
            "activated_skills": list(self.activated_skills),
            "review_rounds": self.review_rounds,
            "pipeline_fixes": self.pipeline_fixes,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "findings": [f.to_dict() for f in self.findings],
            "approvals": list(self.approvals),
            "history": list(self.history),
            "evidence": self.evidence,
            "escalated_from": self.escalated_from,
            "escalation_reason": self.escalation_reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LoopRecord:
        return cls(
            id=d["id"],
            title=d["title"],
            tier=RiskTier(d.get("tier", "low")),
            mode=Mode(d.get("mode", "attended")),
            stage=Stage(d.get("stage", "intake")),
            created_at=float(d.get("created_at", 0.0)),
            updated_at=float(d.get("updated_at", 0.0)),
            parent_id=d.get("parent_id"),
            origin=str(d.get("origin", "engineer")),
            spec_ref=str(d.get("spec_ref", "")),
            base=str(d.get("base", "main")),
            worktree_path=str(d.get("worktree_path", "")),
            files=list(d.get("files", [])),
            activated_skills=list(d.get("activated_skills", [])),
            review_rounds=int(d.get("review_rounds", 0)),
            pipeline_fixes=int(d.get("pipeline_fixes", 0)),
            total_cost_usd=float(d.get("total_cost_usd", 0.0)),
            findings=[Finding.from_dict(x) for x in d.get("findings", [])],
            approvals=list(d.get("approvals", [])),
            history=list(d.get("history", [])),
            evidence=dict(d.get("evidence", {})),
            escalated_from=d.get("escalated_from"),
            escalation_reason=str(d.get("escalation_reason", "")),
        )


# ── Frontier Tooling Exporters: SARIF 2.1.0 & Git MR/PR Inline Comments ──────


def findings_to_sarif(findings: list[Finding], tool_name: str = "NeoAgent-DevOps") -> dict[str, Any]:
    """Export findings as OASIS SARIF 2.1.0 JSON for GitHub/GitLab/Gitea integration."""
    from .review.catalog import resolve_rule

    rules_seen: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []

    for f in findings:
        if f.status != FindingStatus.OPEN:
            continue
        if f.rule not in rules_seen:
            spec = resolve_rule(f.rule)
            entry: dict[str, Any] = {
                "id": f.rule,
                "shortDescription": {"text": spec.title if spec else f.rule},
                "defaultConfiguration": {"level": f.severity.sarif_level},
            }
            tags = [t for t in ((spec.standard if spec else ""), f.standard) if t]
            if spec:
                tags.append(spec.dimension)
            if tags:
                entry["properties"] = {"tags": sorted(set(tags))}
            rules_seen[f.rule] = entry
        loc: dict[str, Any] = {
            "physicalLocation": {
                "artifactLocation": {"uri": f.file.replace("\\", "/") or "unknown"},
            }
        }
        if f.line > 0:
            loc["physicalLocation"]["region"] = {"startLine": f.line}

        msg_text = f.message + (f" | Suggestion: {f.suggestion}" if f.suggestion else "")
        results.append(
            {
                "ruleId": f.rule,
                "level": f.severity.sarif_level,
                "fingerprints": {"neoagent/v1": f.fingerprint()},
                "message": {"text": msg_text},
                "locations": [loc],
                "properties": {
                    k: v
                    for k, v in (
                        ("attribution", f.attribution),
                        ("standard", f.standard),
                        ("category", f.category.value),
                    )
                    if v
                },
            }
        )

    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": tool_name,
                        "version": "3.0.0",
                        "rules": list(rules_seen.values()),
                    }
                },
                "results": results,
            }
        ],
    }


def findings_to_mr_comments(findings: list[Finding], max_comments: int = 10) -> list[dict[str, Any]]:
    """Format open findings as concise GitLab / GitHub MR/PR inline discussions.

    Prioritized by severity, pinned to exact changed file + line, with actionable fix advice.
    """
    open_sorted = sorted(
        (f for f in findings if f.status == FindingStatus.OPEN and f.file),
        key=lambda x: (-x.severity.rank, x.file, x.line),
    )
    comments: list[dict[str, Any]] = []
    for f in open_sorted[:max_comments]:
        body = f"[{f.severity.value.upper()} · {f.rule}] {f.message}"
        if f.suggestion:
            body += f" 建议：{f.suggestion}"
        comments.append(
            {
                "file": f.file.replace("\\", "/"),
                "line": max(1, f.line),
                "severity": f.severity.value,
                "rule": f.rule,
                "body": body,
            }
        )
    return comments
