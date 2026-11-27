"""Rule catalog — every finding can be traced to a rule, a dimension and a standard.

A review comment that cannot say *which rule it enforces* is an opinion, not a finding. The
catalog gives each deterministic rule (and each free-form rule name an LLM reviewer invents) a
stable identity:

* ``dimension``  — what kind of risk it is (security, correctness, contract, ...).
* ``standard``   — the public reference behind it (CWE id, language documentation, policy).
* ``triage``     — who may fix it: the AI fixer, or a human decision-maker.
* ``rework``     — if the *code* is not the real problem, which upstream stage owns the fix.

The catalog is also what lets SARIF consumers (code-scanning dashboards) group findings by
CWE instead of by opaque rule strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Triage values mirror ``FindingCategory`` without importing it (the models module imports us lazily).
LLM_FIXABLE = "llm_fixable"
SPEC_MISMATCH = "spec_mismatch"
USER_INPUT = "user_input"
HUMAN_DECISION = "human_decision"


@dataclass(frozen=True)
class RuleSpec:
    id: str
    dimension: str
    standard: str
    title: str
    triage: str = LLM_FIXABLE
    rework: str = "build"
    aliases: tuple[str, ...] = ()


def _s(
    id_: str,
    dimension: str,
    standard: str,
    title: str,
    *,
    triage: str = LLM_FIXABLE,
    rework: str = "build",
    aliases: tuple[str, ...] = (),
) -> RuleSpec:
    return RuleSpec(id_, dimension, standard, title, triage, rework, aliases)


_SPECS: tuple[RuleSpec, ...] = (
    # ── Deterministic floor (reviewers.StaticASTReviewer) ────────────────────
    _s("secret-hardcoded", "security", "CWE-798", "Hard-coded credential",
       aliases=("hardcoded-secret", "hardcoded-credential", "hardcoded-password", "secret-leak")),
    _s("secret-aws-key", "security", "CWE-798", "Cloud access key committed"),
    _s("secret-private-key", "security", "CWE-321", "Private key material committed"),
    _s("dynamic-exec", "security", "CWE-94", "Dynamic code execution via eval/exec",
       aliases=("code-injection", "eval-injection")),
    _s("shell-injection", "security", "CWE-78", "OS command injection",
       aliases=("command-injection", "os-command-injection")),
    _s("unsafe-deserialization", "security", "CWE-502", "Deserialization of untrusted data",
       aliases=("insecure-deserialization", "deserialization", "pickle")),
    _s("sql-concat", "security", "CWE-89", "SQL injection through string building",
       aliases=("sql-injection", "sqli")),
    _s("tls-verify-off", "security", "CWE-295", "TLS certificate verification disabled",
       aliases=("ssl-verify", "certificate-validation")),
    _s("cwe22-startswith-path", "security", "CWE-22", "Path confinement bypass via string prefix",
       aliases=("path-traversal", "directory-traversal", "zip-slip")),
    _s("bare-except-regex", "correctness", "CWE-390", "Bare except hides failures"),
    _s("silent-exception-swallow", "correctness", "CWE-390", "Exception swallowed without trace",
       aliases=("swallowed-exception", "empty-catch")),
    _s("mutable-default-arg", "correctness", "Python reference: default argument values",
       "Mutable default argument shares state across calls"),
    _s("python-syntax-error", "correctness", "Python reference: syntax", "Modified file does not parse"),
    _s("debug-leftover", "maintainability", "Clean-code hygiene", "Leftover debugger/debug print"),
    _s("todo-marker", "maintainability", "Clean-code hygiene", "Unresolved TODO/FIXME marker"),
    _s("test-weakening", "test-integrity", "Anti reward-hacking policy",
       "Tests weakened instead of the defect fixed", triage=SPEC_MISMATCH),
    # ── Security catalog (review.security.SecurityReviewer) ──────────────────
    _s("sec-weak-hash", "security", "CWE-328", "Weak hash for a security purpose",
       aliases=("weak-hash", "md5", "sha1-password")),
    _s("sec-weak-cipher", "security", "CWE-327", "Broken or risky cipher/mode",
       aliases=("weak-cipher", "weak-crypto", "ecb-mode", "insecure-cipher")),
    _s("sec-insecure-random", "security", "CWE-338", "Non-cryptographic RNG for a secret",
       aliases=("insecure-random", "weak-random", "predictable-token")),
    _s("sec-debug-enabled", "security", "CWE-489", "Debug mode enabled in shipped code",
       aliases=("debug-mode", "debug-enabled")),
    _s("sec-cors-wildcard", "security", "CWE-942", "Wildcard CORS origin",
       aliases=("cors-misconfiguration", "cors-wildcard", "permissive-cors")),
    _s("sec-jwt-unverified", "security", "CWE-347", "JWT signature not verified",
       aliases=("jwt-none", "jwt-verification", "unverified-signature")),
    _s("sec-xxe", "security", "CWE-611", "XML external entity expansion",
       aliases=("xxe", "xml-external-entity")),
    _s("sec-ssrf", "security", "CWE-918", "Server-side request forgery",
       aliases=("ssrf", "server-side-request-forgery")),
    _s("sec-sensitive-log", "security", "CWE-532", "Secret written to logs",
       aliases=("sensitive-log", "log-injection-secret", "logging-secret", "pii-in-log")),
    _s("sec-error-leak", "security", "CWE-209", "Internal error detail returned to client",
       aliases=("stack-trace-leak", "information-exposure", "error-leak")),
    _s("sec-open-redirect", "security", "CWE-601", "Open redirect",
       aliases=("open-redirect", "unvalidated-redirect")),
    _s("sec-cookie-flags", "security", "CWE-614", "Cookie without Secure/HttpOnly",
       aliases=("insecure-cookie", "cookie-flags")),
    _s("sec-template-injection", "security", "CWE-1336", "Template/expression injection",
       aliases=("template-injection", "ssti", "expression-injection")),
    _s("sec-archive-extract", "security", "CWE-22", "Archive extraction without member validation",
       aliases=("archive-extract", "tar-slip")),
    _s("sec-temp-file", "security", "CWE-377", "Predictable temporary file",
       aliases=("insecure-temp-file", "mktemp")),
    _s("sec-regex-dos", "security", "CWE-1333", "Regex built from interpolated input",
       aliases=("redos", "regex-dos")),
    _s("sec-xss-sink", "security", "CWE-79", "DOM/HTML injection sink",
       aliases=("xss", "cross-site-scripting", "dom-xss")),
    _s("sec-storage-secret", "security", "CWE-922", "Secret persisted in browser storage",
       aliases=("local-storage-secret",)),
    _s("sec-mybatis-dollar", "security", "CWE-89", "Un-parameterised SQL placeholder",
       aliases=("mybatis-injection",)),
    _s("sec-java-exec", "security", "CWE-78", "Runtime command execution"),
    _s("sec-java-deserialize", "security", "CWE-502", "Native Java deserialization"),
    # ── Change-contract catalog (review.structural) ──────────────────────────
    _s("scope-drift", "process", "Declared change scope", "Change touches files outside its declared scope",
       triage=USER_INPUT, aliases=("scope-creep", "out-of-scope")),
    _s("change-without-tests", "test-integrity", "Test-first discipline",
       "Production logic changed with no test evidence", aliases=("missing-tests", "no-tests")),
    _s("public-contract-change", "contract", "Public interface stability",
       "Public function signature changed", triage=HUMAN_DECISION, rework="design",
       aliases=("breaking-change", "signature-change", "api-change")),
    _s("public-api-removed", "contract", "Public interface stability",
       "Public function removed", triage=HUMAN_DECISION, rework="design",
       aliases=("api-removal", "removed-api")),
    _s("test-without-assertion", "test-integrity", "Assertion strength",
       "Test executes code but verifies nothing", aliases=("assertionless-test", "no-assertion")),
    _s("tautological-assertion", "test-integrity", "Assertion strength",
       "Assertion can never fail", aliases=("always-true-assert", "trivial-assertion")),
    _s("weak-assertion-only", "test-integrity", "Assertion strength",
       "Only existence/truthiness is asserted", aliases=("weak-assertion",)),
    # ── Clean-code & coding-standards catalog (devops.standards) ─────────────
    _s("cc-long-function", "maintainability", "Clean Code: single-responsibility function size",
       "Function exceeds clean-code length budget", aliases=("long-function", "god-function")),
    _s("cc-deep-nesting", "maintainability", "Clean Code: guard clauses & flat control flow",
       "Control-flow nesting depth hides the happy path", aliases=("deep-nesting", "arrow-code")),
    _s("cc-high-complexity", "maintainability", "McCabe cyclomatic complexity",
       "Function branch complexity exceeds threshold", aliases=("cyclomatic-complexity", "high-complexity")),
    _s("cc-too-many-params", "maintainability", "Clean Code: parameter object / data clump",
       "Function signature carries too many parameters", aliases=("too-many-arguments", "data-clump")),
    _s("cc-boolean-trap", "maintainability", "Clean Code: explicit split over flag parameters",
       "Multiple boolean flag parameters obscure call-site intent", aliases=("flag-argument", "boolean-trap")),
    _s("cc-untracked-debt", "maintainability", "Clean Code: bounded technical debt",
       "Shortcut/debt marker lacks a concrete revisit trigger", aliases=("untracked-debt", "unbounded-shortcut")),
    _s("py-wildcard-import", "maintainability", "PEP 8: explicit imports",
       "Wildcard import pollutes module namespace", aliases=("star-import", "wildcard-import")),
    _s("py-late-binding-closure", "correctness", "Python language reference: lexical closures",
       "Closure inside loop captures loop variable by reference", aliases=("late-binding", "loop-closure")),
    _s("py-async-blocking-call", "performance", "Python asyncio: non-blocking event loop",
       "Synchronous blocking call inside async coroutine stalls event loop",
       aliases=("blocking-in-async", "sync-in-async")),
    _s("py-reraise-without-context", "correctness", "PEP 3134: exception chaining",
       "Exception re-wrapped without 'from exc' severs root-cause traceback",
       aliases=("missing-raise-from", "lost-exception-chain")),
    _s("py-mutable-class-attr", "correctness", "PEP 8 / dataclasses: instance state isolation",
       "Mutable container on class body is shared across instances",
       aliases=("shared-class-attribute", "mutable-class-default")),
    _s("py-finally-jump", "correctness", "CERT Python: finally block completion",
       "Jump statement inside finally block silently discards active exceptions",
       aliases=("return-in-finally", "finally-swallow")),
    # ── Blueprint S/D/C compliance catalog (devops.blueprint) ────────────────
    _s("sdc-untested-requirement", "spec-compliance", "Blueprint S1: requirement test coverage",
       "Approved requirement lacks passing task or test verification",
       triage=SPEC_MISMATCH, aliases=("missing-requirement-test",)),
    _s("sdc-missing-interface", "design-compliance", "Blueprint D1: interface contract fidelity",
       "Planned public interface symbol missing from implementation",
       rework="design", aliases=("missing-planned-symbol",)),
    _s("sdc-undeclared-file-touch", "design-compliance", "Blueprint D3: declared blast radius",
       "Changed source file outside approved design/task file plan",
       triage=HUMAN_DECISION, rework="design", aliases=("design-scope-drift",)),
    _s("sdc-incomplete-traceability", "spec-compliance", "Blueprint S5: spec-design-code traceability",
       "Traceability matrix has unlinked requirement or missing evidence"),
)

CATALOG: dict[str, RuleSpec] = {s.id: s for s in _SPECS}

_PRIVACY_SPEC = RuleSpec(
    "privacy-dlp", "privacy", "DLP policy ([privacy] in devops.toml)", "Confidential/internal identifier"
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


_ALIAS_INDEX: list[tuple[frozenset[str], RuleSpec]] = sorted(
    (
        (frozenset(_tokens(alias)), spec)
        for spec in _SPECS
        for alias in (*spec.aliases, spec.id)
    ),
    key=lambda pair: -len(pair[0]),
)


def resolve_rule(rule: str, message: str = "") -> RuleSpec | None:
    """Map a rule id (or an LLM's free-form rule name) to its catalog entry.

    Exact ids win; otherwise an alias matches when *all* of its tokens appear in the rule name.
    Single-token aliases are never matched against free text, to avoid claiming a CWE from a
    stray word in a message.
    """
    if not rule:
        return None
    if rule in CATALOG:
        return CATALOG[rule]
    if rule.startswith("privacy-"):
        return _PRIVACY_SPEC
    rule_tokens = _tokens(rule)
    for alias_tokens, spec in _ALIAS_INDEX:
        if alias_tokens and alias_tokens <= rule_tokens:
            return spec
    if message:
        msg_tokens = _tokens(message)
        for alias_tokens, spec in _ALIAS_INDEX:
            if len(alias_tokens) >= 2 and alias_tokens <= msg_tokens:
                return spec
    return None


def catalog_summary() -> dict[str, int]:
    """Counts per dimension — surfaced by ``harness-audit`` so the catalog is inspectable."""
    out: dict[str, int] = {}
    for spec in _SPECS:
        out[spec.dimension] = out.get(spec.dimension, 0) + 1
    return dict(sorted(out.items()))
