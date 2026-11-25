"""Enterprise Data Loss Prevention (DLP) & Portability Privacy Guardrails.

Serves two critical roles in Harness L4 (Context Hygiene) and L6 (Mechanical Red Lines):
1. **Outbound Redaction (`redact_for_llm`)**: Scrubs workstation paths, internal
   corporate domains, proxy credentials, and organization-configured confidential terms
   before skill guides or diffs are sent to external LLM providers.
2. **Inbound / Commit Gate (`scan_diff_privacy` & `scan_repo_privacy`)**: Mechanically
   blocks commits, skills, or docs that leak internal hostnames, private endpoints,
   or author machine paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import Finding, FindingCategory, Severity

# Workstation absolute paths (Windows `C:\Users\...`, macOS `/Users/...`, Linux `/home/...`)
_WORKSTATION_PATH_RE = re.compile(
    r"(?:[A-Za-z]:\\Users\\[A-Za-z0-9_][A-Za-z0-9._-]*(?:\\[^\s\"'`<>|]+)*"
    r"|/Users/[A-Za-z0-9_][A-Za-z0-9._-]*(?:/[^\s\"'`<>|]+)*"
    r"|/home/[a-z0-9_][a-z0-9._-]*(?:/[^\s\"'`<>|]+)*)"
)

# Internal corporate / intranet domains & proxy URLs with embedded credentials
_INTERNAL_DOMAIN_RE = re.compile(
    r"(?i)\b(?:https?|wss?|ssh|git)://[^\s\"'`/]*\.(?:internal|intra|intranet|corp|lan|localdomain)\b[^\s\"'`]*"
)
_PROXY_AUTH_URL_RE = re.compile(
    r"(?i)\b(?:https?|socks5?)://[^/\s\"'`]+:[^@\s\"'`]+@[^\s\"'`]+"
)

# Placeholder / documentation exemption markers (per Harness L6 portability spec)
_PLACEHOLDER_EXEMPT_RE = re.compile(
    r"<[^>]+>|\bplaceholder\b|\bexample\b|占位|示例|/Users/me\b|/home/user\b",
    re.IGNORECASE,
)

_DEFAULT_SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "node_modules",
    ".venv",
    "venv",
    ".neoagent-devops",
    "workspace",
}

_SCANNABLE_SUFFIXES = {
    ".py",
    ".pyi",
    ".md",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".jsonc",
    ".sh",
    ".ps1",
    ".txt",
}


@dataclass
class PrivacyPolicyConfig:
    """Configurable DLP rules loaded from ``devops.toml`` ``[privacy]``."""

    enabled: bool = True
    redact_llm_prompts: bool = True
    block_workstation_paths: bool = True
    block_internal_urls: bool = True
    blocked_terms: list[str] = field(default_factory=list)
    blocked_regexes: list[str] = field(default_factory=list)


@dataclass
class PrivacyViolation:
    rule: str
    file: str
    line: int
    matched: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "file": self.file,
            "line": self.line,
            "matched": self.matched,
            "message": self.message,
        }


def _compile_custom_patterns(cfg: PrivacyPolicyConfig) -> list[tuple[str, re.Pattern[str]]]:
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for term in cfg.blocked_terms:
        clean = term.strip()
        if clean:
            compiled.append((f"blocked-term:{clean[:24]}", re.compile(re.escape(clean), re.IGNORECASE)))
    for idx, raw_pat in enumerate(cfg.blocked_regexes):
        if not raw_pat.strip():
            continue
        try:
            compiled.append((f"blocked-regex:{idx + 1}", re.compile(raw_pat)))
        except re.error:
            continue
    return compiled


def redact_for_llm(text: str, cfg: PrivacyPolicyConfig | None = None) -> str:
    """Sanitize outbound prompt/skill text so confidential identifiers never leave the host."""
    policy = cfg or PrivacyPolicyConfig()
    if not policy.enabled or not policy.redact_llm_prompts or not text:
        return text

    scrubbed = text
    if policy.block_proxy_or_urls if hasattr(policy, "block_proxy_or_urls") else policy.block_internal_urls:
        scrubbed = _PROXY_AUTH_URL_RE.sub("<REDACTED_AUTH_URL>", scrubbed)
        scrubbed = _INTERNAL_DOMAIN_RE.sub("<REDACTED_INTERNAL_URL>", scrubbed)
    if policy.block_workstation_paths:
        scrubbed = _WORKSTATION_PATH_RE.sub("<WORKSPACE_PATH>", scrubbed)
    for _, pat in _compile_custom_patterns(policy):
        scrubbed = pat.sub("<REDACTED_CONFIDENTIAL>", scrubbed)
    return scrubbed


def scan_line_privacy(
    line_text: str,
    file_path: str,
    line_no: int,
    cfg: PrivacyPolicyConfig | None = None,
) -> list[PrivacyViolation]:
    """Check a single line of code/docs for portability or enterprise privacy leaks."""
    policy = cfg or PrivacyPolicyConfig()
    if not policy.enabled or not line_text.strip():
        return []

    violations: list[PrivacyViolation] = []

    if policy.block_internal_urls:
        for m in _PROXY_AUTH_URL_RE.finditer(line_text):
            violations.append(
                PrivacyViolation(
                    rule="privacy-auth-url",
                    file=file_path,
                    line=line_no,
                    matched=m.group(0)[:40] + "...",
                    message="Embedded credentials in proxy/service URL must never be committed",
                )
            )
        for m in _INTERNAL_DOMAIN_RE.finditer(line_text):
            if not _PLACEHOLDER_EXEMPT_RE.search(line_text):
                violations.append(
                    PrivacyViolation(
                        rule="privacy-internal-domain",
                        file=file_path,
                        line=line_no,
                        matched=m.group(0)[:60],
                        message="Internal corporate/intranet URL detected; parameterize via environment config",
                    )
                )

    if policy.block_workstation_paths and not _PLACEHOLDER_EXEMPT_RE.search(line_text):
        for m in _WORKSTATION_PATH_RE.finditer(line_text):
            violations.append(
                PrivacyViolation(
                    rule="privacy-workstation-path",
                    file=file_path,
                    line=line_no,
                    matched=m.group(0)[:60],
                    message="Hardcoded workstation user path breaks portability and leaks author identity",
                )
            )

    for label, pat in _compile_custom_patterns(policy):
        m = pat.search(line_text)
        if m:
            violations.append(
                PrivacyViolation(
                    rule="privacy-confidential-term",
                    file=file_path,
                    line=line_no,
                    matched=m.group(0)[:40],
                    message=f"Confidential enterprise identifier matched ({label})",
                )
            )

    return violations


def violations_to_findings(violations: list[PrivacyViolation], source: str = "privacy-dlp") -> list[Finding]:
    return [
        Finding(
            rule=v.rule,
            message=f"{v.message} (matched: {v.matched})",
            severity=Severity.CRITICAL,
            category=FindingCategory.LLM_FIXABLE,
            file=v.file,
            line=v.line,
            source=source,
            suggestion="Replace confidential/internal values with neutral placeholders or environment variables.",
        )
        for v in violations
    ]


def scan_repository_privacy(
    repo_root: Path,
    cfg: PrivacyPolicyConfig | None = None,
) -> list[PrivacyViolation]:
    """Scan all tracked/source files in ``repo_root`` for enterprise privacy or path leaks."""
    root = repo_root.resolve()
    policy = cfg or PrivacyPolicyConfig()
    all_violations: list[PrivacyViolation] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = set(path.relative_to(root).parts)
        if rel_parts & _DEFAULT_SKIP_DIRS:
            continue
        if path.suffix.lower() not in _SCANNABLE_SUFFIXES:
            continue
        rel_str = str(path.relative_to(root)).replace("\\", "/")
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for idx, line in enumerate(content.splitlines(), 1):
            all_violations.extend(scan_line_privacy(line, rel_str, idx, policy))

    return all_violations
