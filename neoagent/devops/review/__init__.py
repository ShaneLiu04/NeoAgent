"""Governed Review Protocol — native, vendor-neutral, offline-capable code review governance.

See ``docs/REVIEW_PROTOCOL.md`` for the design. Public surface:

* :class:`ReviewProtocol` / :class:`Verdict` — orchestration, attestation, verdict routing.
* :func:`ground_findings` — drop LLM findings that do not point at real diff lines.
* :func:`attribute_findings` / :func:`invert_diff` — introduced vs carried-over vs fixed.
* :class:`SecurityReviewer`, :class:`ScopeDriftReviewer`, :class:`TestEvidenceReviewer`,
  :class:`ContractDriftReviewer`, :class:`WeakTestReviewer` — deterministic reviewers.
* :func:`resolve_rule` / ``CATALOG`` — rule → dimension → standard mapping.
"""

from .attribution import attribute_findings, invert_diff
from .catalog import CATALOG, RuleSpec, catalog_summary, resolve_rule
from .grounding import ground_findings, is_llm_source
from .protocol import (
    ReviewOutcome,
    ReviewProtocol,
    Verdict,
    build_decision_brief,
    decide_verdict,
    offending_code_unchanged,
)
from .security import SecurityReviewer
from .structural import (
    ContractDriftReviewer,
    ScopeDriftReviewer,
    TestEvidenceReviewer,
    WeakTestReviewer,
)

__all__ = [
    "CATALOG",
    "ContractDriftReviewer",
    "ReviewOutcome",
    "ReviewProtocol",
    "RuleSpec",
    "ScopeDriftReviewer",
    "SecurityReviewer",
    "TestEvidenceReviewer",
    "Verdict",
    "WeakTestReviewer",
    "attribute_findings",
    "build_decision_brief",
    "catalog_summary",
    "decide_verdict",
    "ground_findings",
    "invert_diff",
    "is_llm_source",
    "offending_code_unchanged",
    "resolve_rule",
]
