"""Progressive-disclosure SKILL.md registry, Harness F1-F4 validator, and stage router.

Implements the 3-tier progressive disclosure pattern for modular engineering skills:
  - Tier 1 (Catalog): ~40 tokens/skill (name + 1-line summary) for broad discovery
  - Tier 2 (Activation): Full ``SKILL.md`` body + ``interface.json`` contract
  - Tier 3 (Reference): On-demand ``references/*.md`` rubrics & language standards

This prevents blowing up the 40% LLM context budget when mounting a large
skill repository.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.config import get_config

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)
_AUTHOR_PATH_RE = re.compile(
    r"(?:[A-Za-z]:\\Users\\[^\s\"'`]+|/Users/[^\s\"'`]+|/home/[a-z0-9_-]+/[^\s\"'`]+)"
)


def parse_frontmatter(raw_markdown: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from SKILL.md without failing on unquoted colons."""
    m = _FRONTMATTER_RE.match(raw_markdown)
    if not m:
        return {}, raw_markdown

    fm_text, body = m.group(1), m.group(2)
    try:
        import yaml  # type: ignore[import-untyped]

        parsed = yaml.safe_load(fm_text)
        if isinstance(parsed, dict):
            return parsed, body
    except Exception:
        pass

    # Resilient line-by-line fallback for SKILL.md files with unescaped colons
    meta: dict[str, Any] = {}
    current_key: str | None = None
    for line in fm_text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")) and ":" in line:
            key, val = line.split(":", 1)
            current_key = key.strip()
            meta[current_key] = val.strip().strip("'\"")
        elif current_key and isinstance(meta.get(current_key), str):
            meta[current_key] = (meta[current_key] + " " + line.strip()).strip()
    return meta, body


@dataclass
class SkillValidationReport:
    """Harness L6 mechanical check (F1-F4 + hardcoded author path scan)."""

    has_frontmatter: bool
    has_name: bool
    name_matches_dir: bool
    has_description: bool
    hardcoded_paths: list[str] = field(default_factory=list)
    line_count: int = 0

    @property
    def passed(self) -> bool:
        return (
            self.has_frontmatter
            and self.has_name
            and self.name_matches_dir
            and self.has_description
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "F1_frontmatter": self.has_frontmatter,
            "F2_name": self.has_name,
            "F3_name_matches_dir": self.name_matches_dir,
            "F4_description": self.has_description,
            "hardcoded_paths": self.hardcoded_paths,
            "line_count": self.line_count,
        }


@dataclass
class SkillDescriptor:
    """Metadata and lazy accessors for a single discovered SKILL.md."""

    name: str
    description: str
    skill_md_path: Path
    category: str = "general"
    version: str = "1.0.0"
    triggers: list[str] = field(default_factory=list)
    interface: dict[str, Any] = field(default_factory=dict)

    @property
    def root_dir(self) -> Path:
        return self.skill_md_path.parent

    @property
    def references_dir(self) -> Path:
        return self.root_dir / "references"

    @property
    def scripts_dir(self) -> Path:
        return self.root_dir / "scripts"

    def list_references(self) -> list[str]:
        if not self.references_dir.is_dir():
            return []
        return sorted(
            str(p.relative_to(self.references_dir)).replace("\\", "/")
            for p in self.references_dir.rglob("*")
            if p.is_file()
        )

    def read_body(self, max_chars: int = 8_000) -> str:
        """Tier 2 activation: read SKILL.md body on demand."""
        raw = self.skill_md_path.read_text(encoding="utf-8", errors="replace")
        _, body = parse_frontmatter(raw)
        text = body.strip()
        if len(text) > max_chars:
            return text[:max_chars] + "\n\n... [SKILL.md truncated for context budget]"
        return text

    def read_reference(self, relative_ref: str, max_chars: int = 8_000) -> str:
        """Tier 3 activation: lazy-load a specific file under references/."""
        base = self.references_dir.resolve()
        cleaned = relative_ref.lstrip("/\\")
        target = (base / cleaned).resolve()
        if not target.is_relative_to(base):
            raise PermissionError(f"Reference path escapes skill directory: {relative_ref}")
        if not target.is_file():
            raise FileNotFoundError(f"Reference '{relative_ref}' not found in skill '{self.name}'")
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            return text[:max_chars] + "\n\n... [reference truncated for context budget]"
        return text

    def validate_harness(self) -> SkillValidationReport:
        """Run Harness L6 F1-F4 checks against this skill."""
        raw = self.skill_md_path.read_text(encoding="utf-8", errors="replace")
        has_fm = bool(_FRONTMATTER_RE.match(raw))
        fm, _ = parse_frontmatter(raw)
        raw_name = str(fm.get("name", "")).strip()
        raw_desc = str(fm.get("description", "")).strip()
        dir_name = self.root_dir.name
        bad_paths = sorted(set(_AUTHOR_PATH_RE.findall(raw)))[:10]
        return SkillValidationReport(
            has_frontmatter=has_fm,
            has_name=bool(raw_name),
            name_matches_dir=bool(raw_name) and (raw_name == dir_name),
            has_description=bool(raw_desc),
            hardcoded_paths=bad_paths,
            line_count=len(raw.splitlines()),
        )

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "version": self.version,
            "description": self.description[:220],
            "references_count": len(self.list_references()),
            "has_interface": bool(self.interface),
        }


# Vendor-neutral lifecycle stage -> canonical skill & capability routing
STAGE_SKILL_PLAYBOOK: dict[str, list[str]] = {
    "intake": [
        "spec-driven-specify",
        "spec-driven-design",
        "project-initializer",
        "brainstorming",
    ],
    "build": [
        "tdd-discipline",
        "clean-code",
        "minimal-code",
        "subagent-driven-development",
        "secure-coding",
        "test-driven-development",
    ],
    "review": [
        "code-review",
        "bug-pattern-checker",
        "secure-coding",
        "web-security-review",
    ],
    "pipeline": [
        "api-test-debug",
        "systematic-debugging",
        "verification-before-completion",
        "ut-coverage-booster",
    ],
    "release": [
        "progressive-release",
        "finishing-a-development-branch",
        "spec-reliability",
    ],
    "observe": [
        "spec-reliability",
        "systematic-debugging",
    ],
    "learn": [
        "postmortem-learn",
        "skill-creator",
        "bug-pattern-checker",
        "unified-memory",
    ],
}

STAGE_DYNAMIC_KEYWORDS: dict[str, str] = {
    "intake": "requirements specification architecture scaffold",
    "build": "tdd clean code implementation secure coding",
    "review": "code review security audit bug pattern",
    "pipeline": "test debug coverage ci failure",
    "release": "release canary reliability deployment",
    "observe": "quality report telemetry alert monitoring",
    "learn": "retrospective evolution memory checklist",
}

EXTENSION_SKILL_MAP: dict[str, list[str]] = {
    ".py": ["python-coding-standards", "code-review"],
    ".pyi": ["python-coding-standards"],
    ".c": ["c-coding-standards", "code-review", "embedded-development"],
    ".h": ["c-coding-standards", "cpp-coding-standards"],
    ".cpp": ["cpp-coding-standards", "code-review"],
    ".cc": ["cpp-coding-standards", "code-review"],
    ".hpp": ["cpp-coding-standards"],
    ".java": ["java-coding-standards", "secure-coding", "web-security-review", "code-review"],
    ".sql": ["sql-coding-standards", "secure-coding", "web-security-review"],
    ".ts": ["typescript-coding-standards", "web-security-review", "secure-coding"],
    ".tsx": ["typescript-coding-standards", "web-security-review", "secure-coding"],
    ".vue": ["web-security-review", "secure-coding"],
    ".js": ["javascript-coding-standards", "web-security-review", "secure-coding"],
    ".go": ["go-coding-standards", "secure-coding", "code-review"],
    ".rs": ["rust-coding-standards", "secure-coding", "code-review"],
}

EXTENSION_DYNAMIC_KEYWORDS: dict[str, str] = {
    ".py": "python pytest",
    ".c": "c embedded",
    ".cpp": "cpp c++",
    ".java": "java spring backend",
    ".sql": "sql database ddl",
    ".ts": "typescript react web security",
    ".tsx": "typescript react web security",
    ".vue": "vue frontend web security",
}


class SkillRegistry:
    """Discovers, indexes, and routes enterprise SKILL.md packages."""

    def __init__(self, roots: list[Path | str] | None = None) -> None:
        self.roots: list[Path] = []
        if roots is not None:
            self.roots = [Path(r).resolve() for r in roots if r and Path(r).is_dir()]
        else:
            self.roots = self._discover_default_roots()
        self._skills: dict[str, SkillDescriptor] = {}
        self.refresh()

    @staticmethod
    def _discover_default_roots() -> list[Path]:
        cfg = get_config()
        candidates: list[Path] = []
        if cfg.devops.skill_repo_dir:
            candidates.append(Path(cfg.devops.skill_repo_dir))
        env_extra = os.getenv("NEOAGENT_EXTRA_SKILL_DIRS", "")
        for part in env_extra.split(os.pathsep):
            if part.strip():
                candidates.append(Path(part.strip()))
        candidates.append(cfg.skills_dir)
        candidates.append(Path.cwd() / ".opencode" / "skills")
        candidates.append(Path.cwd() / ".cursor" / "skills")

        seen: set[Path] = set()
        valid: list[Path] = []
        for c in candidates:
            try:
                resolved = c.resolve()
            except OSError:
                continue
            if resolved.is_dir() and resolved not in seen:
                seen.add(resolved)
                valid.append(resolved)
        return valid

    def refresh(self) -> int:
        """Scan all configured roots for SKILL.md files (excluding backup variants)."""
        self._skills.clear()
        for root in self.roots:
            for skill_md in sorted(root.rglob("SKILL.md")):
                parts_lower = {p.lower() for p in skill_md.parts}
                # Skip original backup variants created by harness-skill-creator Optimize mode
                if "variants" in parts_lower and "original" in parts_lower:
                    continue
                try:
                    desc = self._load_descriptor(skill_md, root)
                except Exception:
                    continue
                # First discovered root wins (explicit repo > workspace fallback)
                self._skills.setdefault(desc.name, desc)
        return len(self._skills)

    @staticmethod
    def _load_descriptor(skill_md: Path, root: Path) -> SkillDescriptor:
        raw = skill_md.read_text(encoding="utf-8", errors="replace")
        fm, body = parse_frontmatter(raw)
        dir_name = skill_md.parent.name
        name = str(fm.get("name") or dir_name).strip()

        description = str(fm.get("description") or "").strip()
        if not description:
            for line in body.splitlines():
                s = line.strip().lstrip("#").strip()
                if s:
                    description = s[:240]
                    break

        rel_parts = skill_md.relative_to(root).parts
        category = rel_parts[0] if len(rel_parts) > 1 else "workspace"
        version = str(fm.get("version") or "1.0.0")

        triggers: list[str] = []
        meta = fm.get("metadata")
        if isinstance(meta, dict) and meta.get("triggers"):
            triggers = [t.strip() for t in str(meta["triggers"]).split(",") if t.strip()]

        interface: dict[str, Any] = {}
        iface_path = skill_md.parent / "interface.json"
        if iface_path.is_file():
            try:
                interface = json.loads(iface_path.read_text(encoding="utf-8"))
            except Exception:
                interface = {}

        return SkillDescriptor(
            name=name,
            description=description,
            skill_md_path=skill_md,
            category=category,
            version=version,
            triggers=triggers,
            interface=interface,
        )

    def count(self) -> int:
        return len(self._skills)

    def get(self, name: str) -> SkillDescriptor | None:
        return self._skills.get(name)

    def list_all(self) -> list[SkillDescriptor]:
        return sorted(self._skills.values(), key=lambda s: (s.category, s.name))

    def search(self, query: str, limit: int = 8) -> list[SkillDescriptor]:
        """Keyword + trigger relevance search across discovered skills."""
        q = query.strip().lower()
        if not q:
            return self.list_all()[:limit]
        tokens = [t for t in re.split(r"[\s,;/|]+", q) if t]
        scored: list[tuple[float, SkillDescriptor]] = []
        for s in self._skills.values():
            score = 0.0
            name_l = s.name.lower()
            desc_l = s.description.lower()
            cat_l = s.category.lower()
            trig_l = " ".join(s.triggers).lower()
            if q == name_l:
                score += 100.0
            elif q in name_l:
                score += 40.0
            for tok in tokens:
                if tok in name_l:
                    score += 15.0
                if tok in trig_l:
                    score += 12.0
                if tok in desc_l:
                    score += 6.0
                if tok in cat_l:
                    score += 3.0
            if score > 0:
                scored.append((score, s))
        scored.sort(key=lambda pair: (-pair[0], pair[1].name))
        return [s for _, s in scored[:limit]]

    def recommend(
        self,
        *,
        stage: str = "",
        files: list[str] | None = None,
        query: str = "",
        limit: int = 6,
    ) -> list[SkillDescriptor]:
        """Deterministic skill router combining DevOps stage, touched languages, and task intent."""
        ordered_names: list[str] = []
        seen: set[str] = set()

        def _push(skill_name: str) -> None:
            if skill_name in self._skills and skill_name not in seen:
                seen.add(skill_name)
                ordered_names.append(skill_name)

        # 1. Language/domain skills inferred from file extensions (highest specificity)
        ext_queries: list[str] = []
        for f in files or []:
            ext = Path(f).suffix.lower()
            for cand in EXTENSION_SKILL_MAP.get(ext, []):
                _push(cand)
            if ext in EXTENSION_DYNAMIC_KEYWORDS:
                ext_queries.append(EXTENSION_DYNAMIC_KEYWORDS[ext])

        # 2. Lifecycle stage playbook skills
        for cand in STAGE_SKILL_PLAYBOOK.get(stage.lower(), []):
            _push(cand)

        # 3. Dynamic semantic/keyword discovery across any mounted skill pack
        combined_query = " ".join(
            part
            for part in [query, " ".join(ext_queries), STAGE_DYNAMIC_KEYWORDS.get(stage.lower(), "")]
            if part
        ).strip()
        if combined_query:
            for desc in self.search(combined_query, limit=limit):
                _push(desc.name)

        return [self._skills[n] for n in ordered_names[:limit]]

    def build_context_bundle(
        self,
        skill_names: list[str],
        *,
        max_total_chars: int = 12_000,
    ) -> str:
        """Compile activated skills into a bounded, DLP-redacted prompt section (<40% context rule)."""
        if not skill_names:
            return ""
        from ..devops.privacy import redact_for_llm

        per_skill = max(1_500, max_total_chars // max(1, len(skill_names)))
        blocks = [
            "## Loaded Engineering Skills (Mandatory Guardrails — Follow Strictly)",
            f"Loaded skills: {', '.join(skill_names)}",
        ]
        used = sum(len(b) for b in blocks)
        for name in skill_names:
            desc = self.get(name)
            if desc is None:
                continue
            body = redact_for_llm(desc.read_body(max_chars=per_skill))
            section = f"\n### [Skill: {desc.name}] ({desc.category})\n{body}\n"
            if used + len(section) > max_total_chars:
                remaining = max(0, max_total_chars - used - 80)
                if remaining > 300:
                    blocks.append(section[:remaining] + "\n... [budget cap reached]")
                break
            blocks.append(section)
            used += len(section)
        return "\n".join(blocks)

    def catalog_markdown(self, max_items: int = 50) -> str:
        """Tier 1 compact catalog (<40 tokens per skill)."""
        items = self.list_all()
        if not items:
            return "(No skills discovered. Set NEOAGENT_SKILL_REPO to your skill repository.)"
        lines = [f"Discovered {len(items)} enterprise skill(s):"]
        for s in items[:max_items]:
            short = s.description.replace("\n", " ")[:90]
            lines.append(f"- `{s.name}` [{s.category}]: {short}")
        if len(items) > max_items:
            lines.append(f"... and {len(items) - max_items} more (use search_company_skills).")
        return "\n".join(lines)


_registry_singleton: SkillRegistry | None = None


def get_skill_registry(refresh: bool = False) -> SkillRegistry:
    global _registry_singleton
    if _registry_singleton is None:
        _registry_singleton = SkillRegistry()
    elif refresh:
        _registry_singleton.refresh()
    return _registry_singleton


def reset_skill_registry() -> None:
    global _registry_singleton
    _registry_singleton = None
