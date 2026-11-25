"""
Tests for Enterprise Skill Ecosystem (`neoagent/skills/registry.py` & `neoagent/core/skill_loader.py`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from neoagent.core.skill_loader import load_skills
from neoagent.skills.registry import SkillRegistry


@pytest.fixture
def synthetic_skill_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "skill-repo"
    # Valid skill 1
    s1 = repo / "02-代码检视与安全" / "code-review"
    (s1 / "references").mkdir(parents=True)
    (s1 / "SKILL.md").write_text(
        "---\n"
        "name: code-review\n"
        "description: Enterprise multi-language code review checklist and severity gate.\n"
        "---\n\n"
        "# Code Review Skill\nCheck security, correctness, and tests.\n",
        encoding="utf-8",
    )
    (s1 / "references" / "python-rules.md").write_text(
        "# Python Rules\nNever use mutable default arguments.\n",
        encoding="utf-8",
    )

    # Valid skill 2
    s2 = repo / "03-测试设计与自动化" / "api-test-debug"
    s2.mkdir(parents=True)
    (s2 / "SKILL.md").write_text(
        "---\n"
        "name: api-test-debug\n"
        "description: Diagnose API test failures and separate code bugs from environment blockers.\n"
        "---\n\n"
        "# API Test Debug\n",
        encoding="utf-8",
    )

    # Backup variant dir should be skipped automatically
    backup = repo / "01-研发工程范式" / "variants" / "original" / "old-skill"
    backup.mkdir(parents=True)
    (backup / "SKILL.md").write_text(
        "---\nname: old-skill\ndescription: obsolete\n---\n",
        encoding="utf-8",
    )

    # Invalid skill for Harness F1-F4 validator check
    bad = repo / "99-bad" / "wrong-folder-name"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text(
        "---\n"
        "name: mismatched-name\n"
        "description: ''\n"
        "---\n"
        "Hardcoded path C:\\Users\\someone\\secret.txt\n",
        encoding="utf-8",
    )
    return repo


class TestSkillRegistry:
    def test_synthetic_discovery_progressive_disclosure_and_confinement(
        self, synthetic_skill_repo: Path
    ):
        reg = SkillRegistry(roots=[synthetic_skill_repo])
        names = [m.name for m in reg.list_all()]
        assert "code-review" in names
        assert "api-test-debug" in names
        # Backup variant must be ignored
        assert "old-skill" not in names

        # Tier 1: Compact catalog
        catalog = reg.catalog_markdown()
        assert "code-review" in catalog
        assert "Python Rules" not in catalog  # Tier 3 content not leaked into Tier 1!

        # Tier 2: Load SKILL.md body + reference listing
        desc = reg.get("code-review")
        assert desc is not None
        body = desc.read_body()
        assert "Code Review Skill" in body
        assert "python-rules.md" in desc.list_references()

        # Tier 3: Read confined reference file
        ref = desc.read_reference("python-rules.md")
        assert "Never use mutable default arguments" in ref

        # Security: CWE-22 path traversal outside skill references dir must fail
        with pytest.raises(PermissionError):
            desc.read_reference("../../../99-bad/wrong-folder-name/SKILL.md")

    def test_harness_f1_f4_mechanical_validation(self, synthetic_skill_repo: Path):
        reg = SkillRegistry(roots=[synthetic_skill_repo])
        ok_rep = reg.get("code-review").validate_harness()  # type: ignore[union-attr]
        assert ok_rep.passed is True

        bad_desc = reg.get("mismatched-name")
        assert bad_desc is not None
        bad_rep = bad_desc.validate_harness()
        assert bad_rep.passed is False
        assert bad_rep.name_matches_dir is False  # F3
        assert bad_rep.has_description is False  # F4
        assert len(bad_rep.hardcoded_paths) >= 1

    def test_enterprise_skill_repo_autodiscovery_and_stage_routing(self):
        real_skill_repo = Path(__file__).resolve().parents[2] / "skill"
        if not real_skill_repo.is_dir():
            pytest.skip("Sibling ../skill repository not present in this environment")

        reg = SkillRegistry(roots=[real_skill_repo])
        all_skills = reg.list_all()
        assert len(all_skills) >= 75

        # Stage + extension routing for Python review
        rec_review = reg.recommend(
            stage="review",
            files=["neoagent/core/agent.py"],
            query="security and clean code review",
            limit=6,
        )
        rec_names = [m.name for m in rec_review]
        assert "code-review" in rec_names
        assert any(
            n in rec_names
            for n in ("python-coding-standards", "secure-coding", "bug-pattern-checker")
        )

        # Stage routing for CI pipeline debugging
        rec_pipe = reg.recommend(stage="pipeline", query="pytest api failure", limit=5)
        assert "api-test-debug" in [m.name for m in rec_pipe]

    @pytest.mark.asyncio
    async def test_dynamic_skill_loader_tools(self, synthetic_skill_repo: Path, monkeypatch):
        monkeypatch.setenv("NEOAGENT_SKILL_REPO", str(synthetic_skill_repo))
        from neoagent.core.config import reset_config
        from neoagent.skills.registry import reset_skill_registry

        reset_config()
        reset_skill_registry()
        try:
            tools = {t.name: t for t in load_skills()}
            assert "search_company_skills" in tools
            assert "load_skill_guide" in tools

            search_out = await tools["search_company_skills"].ainvoke(
                {"query": "review", "stage": "review", "files_csv": "app.py"}
            )
            assert "code-review" in search_out

            load_out = await tools["load_skill_guide"].ainvoke(
                {"skill_name": "code-review", "reference_path": "python-rules.md"}
            )
            assert "Never use mutable default arguments" in load_out
        finally:
            reset_skill_registry()
            reset_config()
