"""End-to-end tests for DeepSeek Harness, 7-Layer Harness (L1-L7), DLP Privacy, and the Studio desktop app."""

from __future__ import annotations

import json
import subprocess
import urllib.request
from pathlib import Path

import pytest

from neoagent.cli.devops_cli import run_cli
from neoagent.core._compat import AIMessage, HumanMessage, SystemMessage, ToolMessage
from neoagent.core.deepseek_harness import (
    HarnessRoleRouter,
    ThinkStreamParser,
    fold_completed_turn_reasoning,
)
from neoagent.desktop.server import DesktopControlServer
from neoagent.devops.config import load_devops_config
from neoagent.devops.harness import (
    STAGE_BOUNDARIES,
    ContextBudgetMonitor,
    ContextZone,
    EvolutionConstraintError,
    EvolutionManager,
    EvolutionScope,
    RecoveryAction,
    RecoveryPolicy,
    RepoScaffold,
    SpecPhaseGate,
    SprintTracker,
    SubagentDispatchGate,
    ToolOutputDistiller,
    enforce_subagent_return_boundary,
    route_workflow_weight,
    write_context_reset_handoff,
)
from neoagent.devops.models import FailureKind, RiskTier, Severity, Stage
from neoagent.devops.privacy import (
    PrivacyPolicyConfig,
    redact_for_llm,
    scan_repository_privacy,
)
from neoagent.devops.reviewers import StaticASTReviewer
from neoagent.mcp.server import MCPDevOpsServer
from neoagent.skills.registry import SkillRegistry


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "ci@example.org"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "CI Bot"], cwd=path, check=True, capture_output=True
    )
    (path / "app.py").write_text("def health() -> str:\n    return 'ok'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


# ---------------------------------------------------------------------------
# 1. DeepSeek Reasoning-Action Harness
# ---------------------------------------------------------------------------


class TestDeepSeekHarness:
    def test_think_stream_parser_handles_split_tags_and_native_reasoning(self):
        parser = ThinkStreamParser()
        chunks = [
            ("<thi", "native-r1 "),
            ("nk>plan ", ""),
            ("step 2</th", ""),
            ("ink>Final ", ""),
            ("answer.", ""),
        ]
        reasoning_out: list[str] = []
        visible_out: list[str] = []
        for content_delta, reasoning_delta in chunks:
            f = parser.feed(content_delta, reasoning_delta)
            if f.reasoning:
                reasoning_out.append(f.reasoning)
            if f.answer:
                visible_out.append(f.answer)
        tail = parser.flush()
        if tail.reasoning:
            reasoning_out.append(tail.reasoning)
        if tail.answer:
            visible_out.append(tail.answer)

        assert "".join(reasoning_out) == "native-r1 plan step 2"
        assert "".join(visible_out) == "Final answer."

    def test_fold_completed_turn_reasoning_preserves_active_turn_and_folds_history(self):
        messages = [
            SystemMessage(content="System prompt"),
            HumanMessage(content="Turn 1 question"),
            AIMessage(
                content="<think>very long prior reasoning</think>Turn 1 answer",
                additional_kwargs={"reasoning_content": "native prior reasoning"},
            ),
            HumanMessage(content="Turn 2 active multi-step task"),
            AIMessage(
                content="<think>active tool step thought</think>",
                additional_kwargs={"reasoning_content": "keep active step reasoning"},
                tool_calls=[{"id": "call_1", "name": "read_file", "args": {}}],
            ),
            ToolMessage(content="file body", tool_call_id="call_1"),
        ]
        folded = fold_completed_turn_reasoning(messages)
        # Turn 1 reasoning was stripped:
        assert "<think>" not in folded[2].content
        assert "reasoning_content" not in folded[2].additional_kwargs
        assert folded[2].content == "Turn 1 answer"
        # Active Turn 2 reasoning is preserved for ReAct continuity:
        assert "<think>active tool step thought</think>" in folded[4].content
        assert folded[4].additional_kwargs["reasoning_content"] == "keep active step reasoning"

    def test_role_router_maps_reasoning_vs_execution_roles(self):
        for role in ("planner", "architect", "reviewer", "debugger", "security_auditor"):
            assert (
                HarnessRoleRouter.select_model(role, "deepseek-reasoner", "deepseek-chat")
                == "deepseek-reasoner"
            )
        for role in ("coder", "distiller", "executor"):
            assert (
                HarnessRoleRouter.select_model(role, "deepseek-reasoner", "deepseek-chat")
                == "deepseek-chat"
            )


# ---------------------------------------------------------------------------
# 2. 7-Layer Harness Engineering (L1 - L7)
# ---------------------------------------------------------------------------


class TestSevenLayerHarness:
    def test_l1_stage_boundaries_workflow_router_and_scaffold(self, tmp_path: Path):
        for stage in (Stage.INTAKE, Stage.BUILD, Stage.REVIEW, Stage.PIPELINE, Stage.RELEASE):
            assert stage.value in STAGE_BOUNDARIES
            assert STAGE_BOUNDARIES[stage.value].must_know
            assert STAGE_BOUNDARIES[stage.value].forbidden_know

        assert (
            route_workflow_weight(
                files=["app.py"],
                changed_lines=10,
                tier=RiskTier.LOW,
                title="Fix typo in error log",
            )["track"]
            == "lite"
        )
        assert (
            route_workflow_weight(
                files=[f"mod_{i}.py" for i in range(6)],
                changed_lines=250,
                tier=RiskTier.HIGH,
                title="Design distributed payment saga architecture",
            )["track"]
            == "full"
        )

        # Phase 1 diagnosis on bare directory -> scaffold -> apply -> aligned
        diag0 = RepoScaffold.diagnose(tmp_path)
        assert diag0.mode == "scaffold"
        created = RepoScaffold.apply(tmp_path, diag0)
        assert "AGENTS.md" in created
        assert "docs/AI-Map.md" in created
        diag1 = RepoScaffold.diagnose(tmp_path)
        assert diag1.mode == "aligned"
        assert diag1.anti_patterns == []

        # Anti-pattern detection when AGENTS.md exceeds 120 lines
        (tmp_path / "AGENTS.md").write_text("\n".join(["line"] * 135), encoding="utf-8")
        diag2 = RepoScaffold.diagnose(tmp_path)
        assert diag2.mode == "refactor"
        assert any("AGENTS.md" in ap for ap in diag2.anti_patterns)

    def test_l2_tool_output_distiller_and_grep_log(self, tmp_path: Path):
        noisy = "\n".join(
            [f"collecting test item number {i} with padding text" for i in range(80)]
            + ["FAILED tests/test_api.py::test_auth - AssertionError: expected 401 got 200"]
            + ["1 failed, 79 passed in 1.12s"]
        )
        distilled = ToolOutputDistiller.distill_command_output(noisy, max_lines=15, max_chars=900)
        assert "AssertionError" in distilled
        assert len(distilled.splitlines()) <= 16

        log_file = tmp_path / "logs" / "harness.log"
        ToolOutputDistiller.append_grep_log(
            log_file, "ERROR", "pipeline", "step failed\non second line"
        )
        lines = log_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert lines[0].startswith("ERROR:")
        assert "[pipeline]" in lines[0]

    def test_l3_subagent_dispatch_gate(self):
        # Mainline-reserved category is always rejected even if booleans are True
        denied_mainline = SubagentDispatchGate.evaluate(
            "decision_reasoning",
            self_contained_input=True,
            structured_output=True,
            high_independence=True,
            estimated_Read_lines=120,
        )
        assert not denied_mainline.dispatch

        # Missing one of the 3 conditions is rejected
        denied_coupled = SubagentDispatchGate.evaluate(
            "independent_verification",
            self_contained_input=True,
            structured_output=False,
            high_independence=True,
            estimated_Read_lines=120,
        )
        assert not denied_coupled.dispatch

        # Clean 3-condition verification task is approved
        approved = SubagentDispatchGate.evaluate(
            "independent_verification",
            self_contained_input=True,
            structured_output=True,
            high_independence=True,
            estimated_Read_lines=120,
        )
        assert approved.dispatch

    def test_l4_context_budget_40pct_ceiling_and_handoff(self, tmp_path: Path):
        mon = ContextBudgetMonitor(window_tokens=100_000)
        smart = mon.check(15_000)  # 15%
        assert smart.zone == ContextZone.SMART

        warn = mon.check(35_000)  # 35%
        assert warn.zone == ContextZone.WARNING

        dumb = mon.check(45_000)  # 45% > 40%
        assert dumb.zone == ContextZone.DUMB
        assert dumb.recommended_action == "trigger_context_reset_handoff"

        handoff = write_context_reset_handoff(
            tmp_path / "artifacts",
            goal="Implement gateway limiter",
            current_stage="build",
            completed_summaries=["Created schema"],
            next_action="Run pytest suite",
        )
        assert handoff.is_file()
        assert "Implement gateway limiter" in handoff.read_text(encoding="utf-8")

        bounded = enforce_subagent_return_boundary(
            "sec-reviewer",
            "\n".join([f"full trace line {i}" for i in range(40)]),
            "PASS: 0 critical findings",
            tmp_path / "artifacts",
        )
        assert len(bounded.splitlines()) <= 4
        assert (tmp_path / "artifacts" / "subagent-sec-reviewer.out.md").is_file()

    def test_l5_spec_phase_gate_and_sprint_tracker(self, tmp_path: Path):
        req = tmp_path / "req.md"
        req.write_text("# Draft\ntoo short\n", encoding="utf-8")
        bad = SpecPhaseGate.verify_spec_file(req, phase="req")
        assert not bad.passed

        req.write_text(
            "# Feature Scope & Requirement\nDeliver token bucket limiter.\n"
            "## Acceptance Criteria\nGiven burst > 100 rps, return HTTP 429.\n",
            encoding="utf-8",
        )
        assert SpecPhaseGate.verify_spec_file(req, phase="req").passed

        design = tmp_path / "design.md"
        design.write_text(
            "# Architecture & Module Design\nGateway middleware component.\n"
            "## Interface Contract\n`check_limit(key: str) -> bool`\n"
            "## Verification & Test Plan\nUnit + concurrency test cases.\n",
            encoding="utf-8",
        )
        assert SpecPhaseGate.verify_spec_file(design, phase="design").passed

        slices = SprintTracker.partition(["S1", "S2", "S3", "S4", "S5"], sprint_size=2)
        assert len(slices) == 3
        assert slices[0].tasks == ["S1", "S2"]

    def test_l6_recovery_ladder_and_dlp_privacy_gate(self, tmp_path: Path):
        assert (
            RecoveryPolicy.decide(attempt=1, max_attempts=3, transient=True)
            == RecoveryAction.RETRY_TRANSIENT
        )
        assert (
            RecoveryPolicy.decide(attempt=1, max_attempts=3)
            == RecoveryAction.REPAIR_AND_RETRY
        )
        assert (
            RecoveryPolicy.decide(canary_breached=True)
            == RecoveryAction.ROLLBACK_CHECKPOINT
        )
        assert (
            RecoveryPolicy.decide(optional_backend_missing=True)
            == RecoveryAction.DEGRADE_GRACEFULLY
        )
        assert (
            RecoveryPolicy.decide(failure_kind=FailureKind.ENV_BLOCKER, attempt=3, max_attempts=3)
            == RecoveryAction.ESCALATE_HUMAN
        )

        policy = PrivacyPolicyConfig(
            enabled=True,
            blocked_terms=["SYNTHETIC_CONFIDENTIAL_MARK"],
        )
        synth_win_path = "C:\\" + "Users\\alice_dev\\secret_proj\\app.py"
        synth_proxy_url = "http://" + "user:pw123@" + "proxy.corp" + ".internal:8080"
        synth_internal_api = "https://api.staging" + ".internal/v1"
        raw = (
            f"Loaded from {synth_win_path} via "
            f"{synth_proxy_url} with SYNTHETIC_CONFIDENTIAL_MARK"
        )
        scrubbed = redact_for_llm(raw, policy)
        assert "alice_dev" not in scrubbed
        assert "pw123" not in scrubbed
        assert "proxy.corp" not in scrubbed
        assert "SYNTHETIC_CONFIDENTIAL_MARK" not in scrubbed

        diff_with_leak = (
            "diff --git a/service.py b/service.py\n"
            "+++ b/service.py\n"
            "@@ -1,1 +1,2 @@\n"
            f'+ENDPOINT = "{synth_internal_api}"\n'
            '+TAG = "SYNTHETIC_CONFIDENTIAL_MARK"\n'
        )
        reviewer = StaticASTReviewer(privacy_policy=policy)
        findings = reviewer.review(diff_with_leak, tmp_path)
        dlp_findings = [f for f in findings if f.rule.startswith("privacy-")]
        assert len(dlp_findings) >= 2
        assert all(f.severity == Severity.CRITICAL for f in dlp_findings)

    def test_l7_safe_evolution_and_hard_constraints(self, tmp_path: Path):
        evo = EvolutionManager(tmp_path)

        # DATA scope auto-applies
        p_data = evo.formalize(
            scope=EvolutionScope.DATA,
            trigger_signal="unmapped_ext",
            target_artifact="AGENTS.md",
            addition_summary="Map .proto files to rpc-interface skill",
            rollback_hint="Remove .proto entry in AGENTS.md",
        )
        assert p_data.approved_by == "auto-l7-data"
        assert len(evo.list_proposals()["applied"]) == 1

        # RULE scope stays pending until approved
        p_rule = evo.record_hashimoto_guardrail(
            loop_id="loop-01",
            reason="Missing idempotency check on retryable POST",
            trigger="three_strike_error",
        )
        assert p_rule.approved_by == ""
        assert len(evo.list_proposals()["pending"]) == 1
        applied_rule = evo.apply_proposal(p_rule, approved_by="lead-engineer")
        assert applied_rule.approved_by == "lead-engineer"
        assert len(evo.list_proposals()["pending"]) == 0

        # STRUCTURAL rejects auto approval
        p_struct = evo.formalize(
            scope=EvolutionScope.STRUCTURAL,
            trigger_signal="stage_topology_change",
            target_artifact="AGENTS.md",
            addition_summary="Add pre-release chaos gate",
            rollback_hint="Revert stage config",
        )
        with pytest.raises(EvolutionConstraintError):
            evo.apply_proposal(p_struct, approved_by="auto-bot")

        # Hard Constraints: cannot weaken L6 or delete existing rules
        with pytest.raises(EvolutionConstraintError):
            evo.formalize(
                scope=EvolutionScope.RULE,
                trigger_signal="shortcut",
                target_artifact="devops.toml",
                addition_summary="Disable anti-cheat",
                rollback_hint="git revert",
                weakens_l6_constraint=True,
            )
        with pytest.raises(EvolutionConstraintError):
            evo.formalize(
                scope=EvolutionScope.DATA,
                trigger_signal="cleanup",
                target_artifact=".bug-patterns/checklist.md",
                addition_summary="Delete legacy checklist",
                rollback_hint="git revert",
                deletes_existing=True,
            )


# ---------------------------------------------------------------------------
# 3. Studio Desktop Control-Plane Server + CLI + MCP Integration
# ---------------------------------------------------------------------------


class TestStudioDesktopAndCLI:
    def test_desktop_control_server_http_and_sse_end_to_end(self, tmp_path: Path, monkeypatch):
        # Force deterministic local Harness advisor stream (no external network call)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("NEOAGENT_API_KEY", raising=False)
        from neoagent.core.config import reset_config

        reset_config()

        _init_git_repo(tmp_path)
        skill_dir = tmp_path / "skills" / "demo-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: Use when testing the Studio skill index.\n---\n"
            "# Demo Skill\nKeep diffs small and run pytest.\n",
            encoding="utf-8",
        )
        reg = SkillRegistry(roots=[tmp_path / "skills"])

        srv = DesktopControlServer(
            workspace_root=tmp_path,
            host="127.0.0.1",
            port=0,
            skill_registry=reg,
        )
        base_url = srv.start_background()
        try:
            # 1. Static Studio HTML shell
            with urllib.request.urlopen(f"{base_url}/", timeout=5) as resp:
                html = resp.read().decode("utf-8")
                assert resp.status == 200
                assert "NeoAgent Studio" in html

            # 2. Initialize workspace + scaffold
            init_req = urllib.request.Request(
                f"{base_url}/api/init",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(init_req, timeout=5) as resp:
                init_payload = json.loads(resp.read().decode("utf-8"))
                assert init_payload["initialized"] is True

            scaf_req = urllib.request.Request(
                f"{base_url}/api/harness/scaffold",
                data=json.dumps({"apply": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(scaf_req, timeout=5) as resp:
                scaf_payload = json.loads(resp.read().decode("utf-8"))
                assert scaf_payload["diagnosis"]["mode"] == "aligned"

            # 3. Create closed-loop & advance BUILD -> REVIEW
            create_req = urllib.request.Request(
                f"{base_url}/api/loops",
                data=json.dumps(
                    {
                        "title": "Add healthz readiness probe",
                        "files": ["app.py"],
                        "changed_lines": 12,
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(create_req, timeout=5) as resp:
                loop_data = json.loads(resp.read().decode("utf-8"))["loop"]
                loop_id = loop_data["id"]

            for act in ("build", "review"):
                act_req = urllib.request.Request(
                    f"{base_url}/api/loops/{loop_id}/action",
                    data=json.dumps({"action": act}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(act_req, timeout=5) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                    assert "loop" in res or "result" in res

            # 4. Skills F1-F4 validation & Harness L1-L7 status endpoints
            val_req = urllib.request.Request(
                f"{base_url}/api/skills/validate",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(val_req, timeout=5) as resp:
                sk_payload = json.loads(resp.read().decode("utf-8"))
                assert sk_payload["total"] == 1
                assert sk_payload["passed"] == 1

            with urllib.request.urlopen(f"{base_url}/api/harness", timeout=5) as resp:
                h_payload = json.loads(resp.read().decode("utf-8"))
                assert h_payload["layers"]["L1_information_boundary"]["repo_scaffold"]["mode"] == "aligned"
                assert h_payload["layers"]["L6_mechanical_constraints_and_dlp"]["privacy_violations_count"] == 0

            # 5. SSE /api/chat/stream (local Harness advisor streaming reasoning + token + context_status)
            sse_req = urllib.request.Request(
                f"{base_url}/api/chat/stream",
                data=json.dumps(
                    {"message": "Show workspace harness status", "session_id": "test-sse"}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(sse_req, timeout=5) as resp:
                sse_body = resp.read().decode("utf-8")
                assert "event: context_status" in sse_body
                assert "event: reasoning" in sse_body
                assert "event: token" in sse_body
                assert "event: done" in sse_body
        finally:
            srv.shutdown()
            reset_config()

    def test_cli_and_mcp_harness_commands(self, tmp_path: Path):
        _init_git_repo(tmp_path)
        assert run_cli(["--root", str(tmp_path), "init"]) == 0
        assert run_cli(["--root", str(tmp_path), "scaffold", "--apply"]) == 0
        assert run_cli(["--root", str(tmp_path), "privacy-scan"]) == 0
        assert run_cli(["--root", str(tmp_path), "harness-audit"]) == 0

        mcp = MCPDevOpsServer(workspace_root=tmp_path)
        resp = mcp.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "tools/call",
                "params": {"name": "devops_harness_audit", "arguments": {}},
            }
        )
        audit = json.loads(resp["result"]["content"][0]["text"])
        assert audit["layers"]["L1_information_boundary"]["repo_scaffold"]["mode"] == "aligned"
        assert audit["layers"]["L6_mechanical_constraints_and_dlp"]["audit_chain_valid"] is True


# ---------------------------------------------------------------------------
# 4. Repository-Wide Zero Confidential Leakage & Self-Alignment Gate
# ---------------------------------------------------------------------------


class TestRepositorySelfCompliance:
    def test_neoagent_repository_has_zero_privacy_leaks_and_aligned_l1_scaffold(self):
        repo_root = Path(__file__).resolve().parents[1]
        cfg = load_devops_config(repo_root)
        violations = scan_repository_privacy(repo_root, cfg.privacy)
        assert violations == [], f"Privacy violations found in repository: {[v.to_dict() for v in violations]}"

        scaffold = RepoScaffold.diagnose(repo_root)
        assert scaffold.mode == "aligned", f"Expected L1 aligned scaffold, got: {scaffold.to_dict()}"
        assert scaffold.anti_patterns == []

    def test_skill_harness_f1_f4_validator(self, tmp_path: Path):
        bad_dir = tmp_path / "mismatched-dir"
        bad_dir.mkdir()
        (bad_dir / "SKILL.md").write_text(
            "---\nname: wrong-name\n---\n# Oversized\n" + ("line\n" * 520),
            encoding="utf-8",
        )
        reg = SkillRegistry(roots=[tmp_path])
        skills = reg.list_all()
        assert len(skills) == 1
        rep = skills[0].validate_harness()
        assert not rep.passed
        assert not rep.name_matches_dir
        assert not rep.has_description
        assert rep.line_count > 500
