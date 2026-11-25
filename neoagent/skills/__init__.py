"""Progressive-Disclosure Skill Ecosystem — Loader, Validator & Stage Router.

Supports the universal ``SKILL.md`` specification (YAML frontmatter + optional
``interface.json`` + ``references/`` + ``scripts/`` + ``evolution/``) and mounts
local or shared skill libraries via ``NEOAGENT_SKILL_REPO`` with 40% context
budget protection and outbound DLP redaction.
"""

from .registry import (
    SkillDescriptor,
    SkillRegistry,
    SkillValidationReport,
    get_skill_registry,
    parse_frontmatter,
    reset_skill_registry,
)

__all__ = [
    "SkillDescriptor",
    "SkillRegistry",
    "SkillValidationReport",
    "get_skill_registry",
    "parse_frontmatter",
    "reset_skill_registry",
]
