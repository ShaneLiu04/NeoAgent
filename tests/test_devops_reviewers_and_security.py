"""
Tests for Hybrid Reviewers, AST Rules, Anti-Test-Weakening, SARIF/MR Export, and CWE-22 Security Fixes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from neoagent.devops.config import ConvergenceBudgetConfig
from neoagent.devops.gates import review_gate
from neoagent.devops.models import (
    Finding,
    LoopRecord,
    Mode,
    RiskTier,
    Severity,
    findings_to_mr_comments,
    findings_to_sarif,
)
from neoagent.devops.reviewers import (
    BugPatternReviewer,
    StaticASTReviewer,
    apply_density_cap,
)


class TestFileOpsCWE22Fix:
    @pytest.mark.asyncio
    async def test_sibling_directory_prefix_traversal_blocked_and_created_flag_accurate(
        self, tmp_path: Path, monkeypatch
    ):
        ws_root = tmp_path / "ws"
        sibling_evil = ws_root / "office_evil"
        sibling_evil.mkdir(parents=True)
        secret_file = sibling_evil / "secret.txt"
        secret_file.write_text("TOP_SECRET", encoding="utf-8")

        monkeypatch.setenv("NEOAGENT_WORKSPACE", str(ws_root))
        from neoagent.core.config import reset_config
        from neoagent.core.tools.fileops import _resolve_safe, write_file

        reset_config()
        try:
            # Previously `str(target).startswith(str(base))` allowed `sub/../../office_evil/secret.txt`
            # because `.../ws/office_evil` starts with string prefix `.../ws/office`!
            with pytest.raises(PermissionError):
                _resolve_safe("sub/../../office_evil/secret.txt")

            # First write must say "Created", second write must say "Overwritten"
            res1 = await write_file.ainvoke({"filepath": "hello.txt", "content": "v1"})
            assert "Created: hello.txt" in res1
            res2 = await write_file.ainvoke({"filepath": "hello.txt", "content": "v2"})
            assert "Overwritten" in res2 and "hello.txt" in res2
        finally:
            reset_config()


class TestStaticASTAndAntiCheatReviewer:
    def test_detects_secrets_sqli_ast_defects_and_test_weakening(self, tmp_path: Path):
        bad_src = tmp_path / "service.py"
        bad_src.write_text(
            "def append_item(item, bucket=[]):\n"
            "    try:\n"
            "        return bucket + [item]\n"
            "    except Exception:\n"
            "        pass\n",
            encoding="utf-8",
        )

        diff = (
            "diff --git a/service.py b/service.py\n"
            "--- a/service.py\n"
            "+++ b/service.py\n"
            "@@ -0,0 +1,8 @@\n"
            '+API_SECRET = "sk-1234567890abcdefghijklmnop"\n'
            '+query = f"SELECT * FROM users WHERE name = {user_input}"\n'
            "+def append_item(item, bucket=[]):\n"
            "+    try:\n"
            "+        return bucket + [item]\n"
            "+    except Exception:\n"
            "+        pass\n"
            "diff --git a/tests/test_service.py b/tests/test_service.py\n"
            "--- a/tests/test_service.py\n"
            "+++ b/tests/test_service.py\n"
            "@@ -1,4 +1,2 @@\n"
            "+@pytest.mark.skip(reason='temporarily bypass failing check')\n"
            " def test_auth():\n"
            "-    assert verify_token('bad') is False\n"
            "-    assert verify_token('good') is True\n"
        )

        findings = StaticASTReviewer().review(diff, repo_root=tmp_path)
        rules = {f.rule for f in findings}

        assert any("secret" in r for r in rules)
        assert any("sql" in r for r in rules)
        assert any("mutable-default" in r for r in rules)
        assert any("swallow" in r or "except" in r for r in rules)
        assert any("test-weakening" in r for r in rules)

        loop = LoopRecord(
            id="L20261125-001",
            title="Test gate",
            tier=RiskTier.LOW,
            mode=Mode.UNATTENDED,
            findings=findings,
        )
        gate = review_gate(loop, ConvergenceBudgetConfig())
        assert gate.passed is False
        assert len(gate.reasons) >= 3

        # SARIF 2.1.0 & MR Inline Comments export validation
        sarif = findings_to_sarif(findings)
        assert sarif["version"] == "2.1.0"
        assert len(sarif["runs"][0]["results"]) == len(findings)

        mr_comments = findings_to_mr_comments(findings)
        assert any(c["severity"] == "critical" for c in mr_comments)
        assert all("body" in c and "file" in c for c in mr_comments)

    def test_bug_pattern_checklist_and_l7_auto_evolution(self, tmp_path: Path):
        checklist_rel = ".bug-patterns/checklist.md"
        checklist_file = tmp_path / checklist_rel
        checklist_file.parent.mkdir(parents=True)
        checklist_file.write_text(
            "### [P1] Blocking sleep in async path\n"
            "- Severity: major\n"
            "- Regex: `time\\.sleep\\(`\n"
            "- Message: Avoid blocking time.sleep in async service handlers\n",
            encoding="utf-8",
        )

        diff = (
            "diff --git a/worker.py b/worker.py\n"
            "+++ b/worker.py\n"
            "@@ -1,1 +1,2 @@\n"
            "+time.sleep(5)\n"
        )
        bp = BugPatternReviewer(checklist_rel)
        matched = bp.review(diff, repo_root=tmp_path)
        assert len(matched) == 1
        assert matched[0].rule == "bug-pattern:P1"

        # L7 Flywheel: recurring review defect evolves into checklist
        recurring = [
            Finding(
                rule="missing-timeout-httpx",
                message="httpx call missing explicit timeout",
                severity=Severity.MAJOR,
                file="a.py",
                line=10,
            ),
        ]
        added = bp.evolve_checklist(tmp_path, recurring)
        assert added == ["P2"]
        assert "RuleOrigin: missing-timeout-httpx" in checklist_file.read_text(encoding="utf-8")

    def test_clean_code_minor_density_cap(self):
        findings = [
            Finding(rule="bug", message="m1", severity=Severity.MAJOR, file="a.py", line=1),
            Finding(rule="style-1", message="s1", severity=Severity.MINOR, file="a.py", line=2),
            Finding(rule="style-2", message="s2", severity=Severity.MINOR, file="a.py", line=3),
            Finding(rule="nit-1", message="s3", severity=Severity.INFO, file="a.py", line=4),
        ]
        # For 150 changed lines with lines_per_minor=200 -> ceil(150/200) = 1 Minor/Info allowed, Major untouched
        capped, suppressed = apply_density_cap(findings, changed_lines=150, lines_per_minor=200)
        assert suppressed == 2
        assert sum(1 for f in capped if f.severity == Severity.MAJOR) == 1
        assert sum(1 for f in capped if f.severity in (Severity.MINOR, Severity.INFO)) == 1
