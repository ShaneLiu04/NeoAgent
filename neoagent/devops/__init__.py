"""NeoAgent AI DevOps Closed-Loop Engine.

Paves the entire engineering highway from a developer's feature request to
user adoption and feedback-driven iteration:
  INTAKE -> BUILD (TDD + Skills) -> REVIEW <-> AUTO-FIX -> PIPELINE <-> AUTO-FIX
  -> RELEASE (Canary + Health Gate + Auto-Rollback) -> OBSERVE -> LEARN -> Next Loop
"""

from .config import DEFAULT_DEVOPS_TOML, DevOpsConfig, load_devops_config
from .engine import (
    ESCALATED,
    GATE_FAILED,
    OK,
    USAGE,
    WAITING,
    DevOpsEngine,
)
from .ledger import AuditChainTamperedError, DevOpsLedger, IllegalTransition, LoopNotFound
from .models import (
    FailureKind,
    Finding,
    FindingCategory,
    FindingStatus,
    GateResult,
    LoopRecord,
    Mode,
    RiskTier,
    Severity,
    Stage,
    StepResult,
    findings_to_mr_comments,
    findings_to_sarif,
)

__all__ = [
    "DEFAULT_DEVOPS_TOML",
    "ESCALATED",
    "GATE_FAILED",
    "OK",
    "USAGE",
    "WAITING",
    "AuditChainTamperedError",
    "DevOpsConfig",
    "DevOpsEngine",
    "DevOpsLedger",
    "FailureKind",
    "Finding",
    "FindingCategory",
    "FindingStatus",
    "GateResult",
    "IllegalTransition",
    "LoopNotFound",
    "LoopRecord",
    "Mode",
    "RiskTier",
    "Severity",
    "Stage",
    "StepResult",
    "findings_to_mr_comments",
    "findings_to_sarif",
    "load_devops_config",
]
