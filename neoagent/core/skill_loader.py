"""Dynamic skill-to-tool bridge loaded by ``neoagent.core.tools.get_all_tools()``.

Previously ``neoagent/core/tools/__init__.py`` attempted ``from ..skill_loader import load_skills``
which silently failed because ``skill_loader.py`` was missing. This module implements
the progressive-disclosure skill tools so any ``NeoAgent`` instance can search,
activate, and read references from the company Skill repository on demand without
bloating the base system prompt.
"""

from __future__ import annotations

import json

from ._compat import BaseTool
from .security import Capability
from .tools.base import RiskLevel, neoagent_tool


@neoagent_tool(
    name="search_company_skills",
    description=(
        "Search or recommend engineering skills (SDD, TDD, Clean Code, OWASP/CWE security review, "
        "API test debugging, progressive canary release gates) from the mounted Skill library. "
        "Provide query keywords, optional DevOps stage (intake|build|review|pipeline|release|observe|learn), "
        "and optional comma-separated file paths."
    ),
    capabilities=Capability.FILESYSTEM_READ,
    risk_level=RiskLevel.LOW,
    timeout_seconds=10.0,
    tags=["skills", "devops", "standards"],
)
async def search_company_skills(query: str = "", stage: str = "", files_csv: str = "") -> str:
    from ..skills.registry import get_skill_registry

    reg = get_skill_registry()
    files = [f.strip() for f in files_csv.split(",") if f.strip()]
    if stage or files:
        matched = reg.recommend(stage=stage, files=files, query=query, limit=8)
    elif query:
        matched = reg.search(query, limit=8)
    else:
        return reg.catalog_markdown(max_items=30)

    if not matched:
        return f"No matching skills found for query='{query}', stage='{stage}'."
    return json.dumps([m.to_summary_dict() for m in matched], ensure_ascii=False, indent=2)


@neoagent_tool(
    name="load_skill_guide",
    description=(
        "Activate a specific enterprise skill by name (Tier 2 progressive disclosure) to read its "
        "SKILL.md workflow, hard constraints, and available reference files. Optionally pass "
        "reference_path to load a specific Tier 3 rubric or coding standard under references/."
    ),
    capabilities=Capability.FILESYSTEM_READ,
    risk_level=RiskLevel.LOW,
    timeout_seconds=10.0,
    tags=["skills", "progressive-disclosure"],
)
async def load_skill_guide(skill_name: str, reference_path: str = "") -> str:
    from ..skills.registry import get_skill_registry

    reg = get_skill_registry()
    desc = reg.get(skill_name.strip())
    if desc is None:
        suggestions = [s.name for s in reg.search(skill_name, limit=5)]
        return f"Skill '{skill_name}' not found. Closest matches: {suggestions}"

    if reference_path.strip():
        try:
            return desc.read_reference(reference_path.strip())
        except (FileNotFoundError, PermissionError) as exc:
            return f"Error reading reference '{reference_path}': {exc}\nAvailable: {desc.list_references()}"

    refs = desc.list_references()
    header = (
        f"# Skill: {desc.name} (v{desc.version}, category={desc.category})\n"
        f"Available references ({len(refs)}): {', '.join(refs[:15]) or 'none'}\n\n"
    )
    return header + desc.read_body(max_chars=8_000)


def load_skills() -> list[BaseTool]:
    """Return dynamic skill-discovery tools for NeoAgent."""
    return [search_company_skills, load_skill_guide]
