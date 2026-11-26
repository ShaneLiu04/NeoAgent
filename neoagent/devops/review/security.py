"""Catalog-driven deterministic security reviewer (quick scan + deep audit).

Two modes share one rule table:

* **quick** (default) — only *added* lines are examined. Fast enough for every commit, and by
  construction it can only blame the author of the change.
* **deep** — additionally audits the *whole* of every touched file. Findings on lines the change
  did not touch are tagged ``attribution="preexisting"``; the protocol files them as security
  debt instead of blocking the merge. This is the pre-release audit: "what was already wrong in
  the files we are about to ship?"

Rules are precision-first: each regex targets a concrete dangerous construct, optionally needs
same-line context (e.g. ``random.choice`` only matters near a token/secret), and carries a CWE
reference via the catalog so consumers can group by weakness class.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..models import Finding, FindingCategory, Severity
from ..reviewers import is_test_file, parse_diff
from .catalog import resolve_rule

_PY = frozenset({".py"})
_JS = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".vue", ".html"})
_JAVA = frozenset({".java", ".kt"})
_CODE = _PY | _JS | _JAVA
_XML = frozenset({".xml"})

_SECRET_CTX = re.compile(r"(?i)pass(?:word|wd)?|pwd|secret|token|sign|auth|hmac|credential|api_?key")
_RNG_CTX = re.compile(r"(?i)token|secret|passw|nonce|salt|session|otp|api_?key|csrf|reset|verif|code")


@dataclass(frozen=True)
class _SecRule:
    rule: str
    pattern: re.Pattern[str]
    severity: Severity
    message: str
    suggestion: str
    suffixes: frozenset[str]
    require: re.Pattern[str] | None = None
    exclude: re.Pattern[str] | None = None
    escalate: re.Pattern[str] | None = None
    escalated: Severity = Severity.MAJOR


def _r(
    rule: str,
    pattern: str,
    severity: Severity,
    message: str,
    suggestion: str,
    suffixes: frozenset[str],
    **kw: object,
) -> _SecRule:
    return _SecRule(rule, re.compile(pattern), severity, message, suggestion, suffixes, **kw)  # type: ignore[arg-type]


_RULES: tuple[_SecRule, ...] = (
    _r("sec-weak-hash",
       r"""\b(?:hashlib\.(?:md5|sha1)\s*\(|MessageDigest\.getInstance\(\s*["'](?:MD5|SHA-?1)["']|createHash\(\s*["'](?:md5|sha1)["'])""",
       Severity.MINOR, "MD5/SHA-1 is collision-prone and unsuitable for security purposes",
       "Use SHA-256+ for integrity, and bcrypt/scrypt/Argon2 for passwords; for non-security checksums pass usedforsecurity=False.",
       _CODE, exclude=re.compile(r"usedforsecurity\s*=\s*False"), escalate=_SECRET_CTX, escalated=Severity.MAJOR),
    _r("sec-weak-cipher",
       r"""\b(?:DES3?|TripleDES|ARC4|RC4|Blowfish)\b|MODE_ECB|["']AES/ECB|["']DES(?:ede)?/""",
       Severity.MAJOR, "Broken cipher or ECB mode leaks plaintext structure",
       "Use AES-256-GCM (or ChaCha20-Poly1305) with a unique nonce per message.", _CODE),
    _r("sec-insecure-random",
       r"""\brandom\.(?:random|randint|randrange|choice|choices|getrandbits|sample)\s*\(|\bMath\.random\s*\(""",
       Severity.MAJOR, "Non-cryptographic RNG used where the value must be unpredictable",
       "Use secrets.token_urlsafe()/secrets.choice() (Python) or crypto.getRandomValues() (JS).",
       _PY | _JS, require=_RNG_CTX),
    _r("sec-debug-enabled",
       r"""\.run\([^)]*\bdebug\s*=\s*True|^\s*DEBUG\s*=\s*True\b|\bapp\.debug\s*=\s*True""",
       Severity.MAJOR, "Debug mode exposes an interactive console and stack traces",
       "Drive debug flags from environment configuration and default them to off.", _PY),
    _r("sec-cors-wildcard",
       r"""Access-Control-Allow-Origin["']?\s*[:,=]\s*["']\*["']|allow_origins\s*=\s*\[\s*["']\*["']|CORS_ORIGIN_ALLOW_ALL\s*=\s*True|origin\s*:\s*["']\*["']""",
       Severity.MAJOR, "Wildcard CORS origin lets any site read authenticated responses",
       "Allow an explicit origin list; never combine '*' with credentials.", _CODE),
    _r("sec-jwt-unverified",
       r"""algorithms?\s*=\s*\[?\s*["']none["']|verify_signature["']?\s*[:=]\s*False|jwt\.decode\([^)]*\bverify\s*=\s*False""",
       Severity.CRITICAL, "JWT accepted without signature verification",
       "Always verify the signature with a pinned algorithm list; reject alg=none.", _CODE),
    _r("sec-xxe",
       r"""resolve_entities\s*=\s*True|load_dtd\s*=\s*True|no_network\s*=\s*False|setFeature\([^)]*disallow-doctype-decl[^)]*false""",
       Severity.MAJOR, "XML parser configured to expand external entities",
       "Disable DTD/external entities (defusedxml in Python; secure-processing features in Java).",
       _PY | _JAVA),
    _r("sec-ssrf",
       r"""\b(?:requests|httpx|urllib\.request|aiohttp)\.\w+\(\s*(?:request\.(?:args|form|json|values|GET|POST|query_params)|req\.(?:query|body|params)|user_url|input_url)|\b(?:fetch|axios\.\w+)\(\s*req\.(?:query|body|params)""",
       Severity.MAJOR, "Outbound request to a user-controlled URL (SSRF)",
       "Validate scheme and host against an allow-list, block private ranges, and disable redirects.",
       _PY | _JS),
    _r("sec-sensitive-log",
       r"""\b(?:log(?:ger)?\.\w+|logging\.\w+|print|console\.\w+)\s*\([^)]*(?:\{[^}]*(?:passw(?:or)?d|secret|token|api_?key|authorization|credential)[^}]*\}|%s[^)]*,\s*\w*(?:passw(?:or)?d|secret|token|api_?key|authorization)\w*\s*\)|\+\s*\w*(?:passw(?:or)?d|secret|token|api_?key)\w*)""",
       Severity.MAJOR, "Secret value interpolated into a log/print statement",
       "Log an identifier or a masked value (e.g. last 4 chars), never the secret itself.", _CODE),
    _r("sec-error-leak",
       r"""\b(?:return|jsonify|Response|make_response|HTTPException|JSONResponse|res\.(?:send|json))\b[^\n]*(?:traceback\.format_exc\(\)|\be\.stack\b|\.printStackTrace)""",
       Severity.MAJOR, "Stack trace returned to the client",
       "Return a generic error body with a correlation id; log the trace server-side.", _CODE),
    _r("sec-error-leak",
       r"""\b(?:jsonify|JSONResponse|HTTPException|make_response)\b[^\n]*\bstr\(\s*(?:e|exc|err|ex|error)\s*\)""",
       Severity.MINOR, "Raw exception text returned to the client",
       "Return a generic message; keep the exception detail in server logs.", _PY),
    _r("sec-open-redirect",
       r"""\bredirect\(\s*request\.(?:args|GET|form|values|query_params)|\bres\.redirect\(\s*req\.(?:query|body|params)""",
       Severity.MAJOR, "Redirect target taken directly from user input",
       "Redirect only to relative paths or an allow-listed host.", _PY | _JS),
    _r("sec-cookie-flags",
       r"""\bset_cookie\(""",
       Severity.MINOR, "Cookie set without both Secure and HttpOnly",
       "Pass secure=True, httponly=True and an explicit samesite policy.", _PY,
       exclude=re.compile(r"(?is)^(?=.*secure\s*=\s*True)(?=.*httponly\s*=\s*True)")),
    _r("sec-template-injection",
       r"""\brender_template_string\(\s*(?:f["']|request\.|[^"')]+\+)|\bTemplate\(\s*(?:request|user|f["'])|\bfrom_string\(\s*(?:request|f["'])""",
       Severity.MAJOR, "User input flows into a template compiled at runtime",
       "Render a fixed template and pass user data as context variables.", _PY),
    _r("sec-archive-extract",
       r"""\.extractall\(""",
       Severity.MAJOR, "Archive extracted without validating member paths (zip/tar slip)",
       "Extract with filter='data' (Python 3.12+) or validate each member stays inside the target.",
       _PY, exclude=re.compile(r"filter\s*=")),
    _r("sec-temp-file",
       r"""\btempfile\.mktemp\(""",
       Severity.MAJOR, "mktemp() returns a predictable name and is racy",
       "Use tempfile.mkstemp() or NamedTemporaryFile().", _PY),
    _r("sec-regex-dos",
       r"""\bre\.(?:compile|match|search|fullmatch|findall|sub)\(\s*f["']|\bnew RegExp\(\s*(?:req\.|request\.|user)""",
       Severity.MINOR, "Regular expression built from interpolated input (ReDoS / injection)",
       "Escape the input with re.escape() or use a fixed pattern with length limits.", _PY | _JS),
    _r("sec-xss-sink",
       r"""\.innerHTML\s*=(?!=)|dangerouslySetInnerHTML|\bdocument\.write\(|insertAdjacentHTML\(""",
       Severity.MAJOR, "Raw HTML sink — XSS if the value is not sanitised",
       "Assign textContent, or sanitise with a vetted library (e.g. DOMPurify) before insertion.", _JS,
       exclude=re.compile(r"(?i)DOMPurify|sanitize")),
    _r("sec-storage-secret",
       r"""(?:localStorage|sessionStorage)\.setItem\(\s*["'][^"']*(?:token|passw|secret|api_?key)""",
       Severity.MINOR, "Secret persisted in script-readable browser storage",
       "Keep tokens in HttpOnly cookies; avoid persisting secrets client-side.", _JS),
    _r("sec-mybatis-dollar",
       r"""(?i)\b(?:where|and|or|order\s+by|from|into|values|set|like)\b[^\n]*\$\{""",
       Severity.MAJOR, "${} interpolates raw text into SQL (injection)",
       "Use #{} bound parameters; whitelist dynamic identifiers.", _XML),
    _r("sec-java-exec",
       r"""Runtime\.getRuntime\(\)\.exec\([^)]*\+|new ProcessBuilder\([^)]*\+""",
       Severity.MAJOR, "Command built by string concatenation",
       "Pass arguments as a fixed list and validate any user-supplied value against an allow-list.", _JAVA),
    _r("sec-java-deserialize",
       r"""new ObjectInputStream\(|\bXMLDecoder\(|enableDefaultTyping\(""",
       Severity.MAJOR, "Native deserialization of potentially untrusted data",
       "Use a data-only format (JSON with a fixed DTO) and disable polymorphic default typing.", _JAVA),
)

_LOGIC_COMMENT = ("#", "//", "/*", "*", "<!--")


def _to_finding(
    rule: _SecRule, file: str, line: int, text: str, severity: Severity, snippet: str = ""
) -> Finding:
    spec = resolve_rule(rule.rule)
    # The matched token is part of the message so two different offences of one rule in one file
    # stay two findings (identity = rule + file + message), yet survive line shifts.
    detail = " ".join(snippet.split())[:48]
    return Finding(
        rule=rule.rule,
        message=f"{rule.message} [{detail}]" if detail else rule.message,
        severity=severity,
        file=file,
        line=line,
        source="security",
        suggestion=rule.suggestion,
        evidence=text.strip()[:200],
        standard=spec.standard if spec else "",
        rework=spec.rework if spec else "",
        category=FindingCategory(spec.triage if spec else "llm_fixable"),
    )


class SecurityReviewer:
    """Deterministic, CWE-referenced security rules over added lines (and, in deep mode, whole files)."""

    name = "security"
    deterministic = True
    attributable = True
    reads_worktree = False
    context_inputs = ("diff",)

    def __init__(self, deep: bool = False, max_files: int = 25, max_lines: int = 4000) -> None:
        self.deep = deep
        self.max_files = max_files
        self.max_lines = max_lines
        # Deep mode reads whole files from the worktree.
        self.reads_worktree = deep
        if deep:
            self.context_inputs = ("diff", "worktree_files")

    def scan_line(self, file: str, line: int, text: str) -> list[Finding]:
        stripped = text.strip()
        if not stripped or stripped.startswith(_LOGIC_COMMENT):
            return []
        suffix = Path(file).suffix.lower()
        out: list[Finding] = []
        for rule in _RULES:
            if suffix not in rule.suffixes:
                continue
            hit = rule.pattern.search(text)
            if hit is None:
                continue
            if rule.exclude is not None and rule.exclude.search(text):
                continue
            if rule.require is not None and not rule.require.search(text):
                continue
            sev = rule.severity
            if rule.escalate is not None and rule.escalate.search(text):
                sev = rule.escalated if rule.escalated.rank > sev.rank else sev
            out.append(_to_finding(rule, file, line, text, sev, hit.group(0)))
        return out

    def review(self, diff_text: str, repo_root: Path | None = None) -> list[Finding]:
        findings: list[Finding] = []
        added: dict[str, set[int]] = {}
        for dl in parse_diff(diff_text):
            if not dl.added or not dl.file or is_test_file(dl.file):
                continue
            added.setdefault(dl.file, set()).add(dl.line)
            findings.extend(self.scan_line(dl.file, dl.line, dl.text))

        if self.deep and repo_root is not None:
            findings.extend(self._audit_files(repo_root, added))

        seen: dict[str, Finding] = {}
        for f in findings:
            key = f"{f.rule}|{f.file}|{f.line}"
            if key not in seen or f.severity.rank > seen[key].severity.rank:
                seen[key] = f
        return list(seen.values())

    def _audit_files(self, repo_root: Path, added: dict[str, set[int]]) -> list[Finding]:
        out: list[Finding] = []
        root = repo_root.resolve()
        for rel, touched in list(added.items())[: self.max_files]:
            full = (root / rel).resolve()
            if not full.is_file() or not full.is_relative_to(root):
                continue
            try:
                lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for idx, text in enumerate(lines[: self.max_lines], start=1):
                if idx in touched:
                    continue
                for f in self.scan_line(rel, idx, text):
                    f.attribution = "preexisting"
                    out.append(f)
        return out
