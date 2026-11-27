"""Comprehensive tests for the 3-Layer Coding Standards Quality Stack and Spec-to-Code Blueprint Engine."""

from __future__ import annotations

import json
import subprocess
import urllib.request
from pathlib import Path

import pytest

from neoagent.cli.devops_cli import run_cli
from neoagent.desktop.server import DesktopControlServer
from neoagent.devops.blueprint import (
    BlueprintPhase,
    BlueprintPhaseGate,
    BlueprintWorkspace,
    ChangeType,
    DeterministicBlueprintSynthesizer,
    GranularityAnalyzer,
    ImplementerStatus,
    LiteEligibilityGate,
    ModelTierSelector,
    OpenQuestion,
    QualityScenario,
    RequirementItem,
    RequirementType,
    ScriptedTaskImplementer,
    SpecDocument,
    TaskExecutionOutcome,
    TaskGranularity,
    TaskPlan,
    TaskSpec,
    TDDEvidence,
    apply_rework_reset,
    build_compaction_snapshot,
    compile_task_context_pack,
    detect_phase,
)
from neoagent.devops.config import DevOpsConfig
from neoagent.devops.engine import ESCALATED, OK, WAITING, DevOpsEngine
from neoagent.devops.models import RiskTier
from neoagent.devops.reviewers import build_review_chain
from neoagent.devops.standards import (
    CleanCodeCheck,
    CleanCodeReviewer,
    QualityStackCompiler,
    infer_languages,
    inspect_debt_markers,
    parse_clean_code_check,
)
from neoagent.mcp.server import MCPDevOpsServer


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "dev@example.org"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Demo Dev"], cwd=root, check=True, capture_output=True)
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8")
    (root / "svc").mkdir(parents=True, exist_ok=True)
    (root / "svc" / "pricing.py").write_text(
        "def compute_total(unit_price: float, qty: int) -> float:\n    return round(unit_price * qty, 2)\n",
        encoding="utf-8",
    )
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "test_pricing.py").write_text(
        "from svc.pricing import compute_total\n\n\ndef test_compute_total():\n    assert compute_total(10.0, 2) == 20.0\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=root, check=True, capture_output=True)


# ══════════════════════════════════════════════════════════════════════════════
# 1. 3-Layer Coding Standards Quality Stack & Clean-Code AST Reviewer
# ══════════════════════════════════════════════════════════════════════════════


class TestQualityStackAndCleanCode:
    def test_quality_stack_compiler_layers_budget_and_dlp(self, tmp_path: Path):
        (tmp_path / "AGENTS.md").write_text(
            "# Repo Schema\nFollow strict typing. Workstation note: C:\\Users\\alice\\secret.txt\n",
            encoding="utf-8",
        )
        langs = infer_languages(["svc/api.py", "web/view.tsx", "native/engine.cpp", "db/init.sql"])
        assert langs == ["python", "typescript", "cpp", "sql"]

        bundle = QualityStackCompiler().compile(
            files=["svc/api.py", "web/view.tsx"],
            stage="build",
            repo_root=tmp_path,
            max_chars=3500,
        )
        assert "clean-code-5d" in bundle.loaded_layers
        assert "minimal-code-ladder" in bundle.loaded_layers
        assert "python-standards" in bundle.loaded_layers
        assert "typescript-standards" in bundle.loaded_layers
        assert "repo-agents-schema" in bundle.loaded_layers
        assert len(bundle.prompt_markdown) <= 3500
        assert "C:\\Users\\alice" not in bundle.prompt_markdown

    def test_clean_code_check_rejects_rubber_stamps_and_parses_bilingual(self):
        vague = CleanCodeCheck(
            simplicity="looks clean",
            reliability="ok",
            maintainability="good",
            testability="",
            performance="lgtm",
            scope_discipline="yes",
        )
        errs = vague.validate()
        assert len(errs) == 6

        parsed = parse_clean_code_check(
            "- 简洁: Reused stdlib dataclasses with zero extra wrapper classes.\n"
            "- Reliability: Validated inputs at entry and chained exceptions with `from err`.\n"
            "- Maintainability: Max function length is 28 lines and nesting depth is 2.\n"
            "- Testability: Added deterministic unit tests without mocking core logic.\n"
            "- 性能: O(n) single pass over diff lines; zero repeated file reads.\n"
            "- scope_discipline: Modified only `svc/pricing.py` and `tests/test_pricing.py`.\n"
        )
        assert parsed.validate() == []

    def test_debt_marker_validation(self):
        has_tag, ok = inspect_debt_markers(
            "items.sort()  # debt: O(n log n) in-memory sort <= 5k rows, revisit when dataset exceeds 50k"
        )
        assert has_tag is True and ok is True

        has_bad, bad_ok = inspect_debt_markers("x = 1  # debt: quick hack")
        assert has_bad is True and bad_ok is False

    def test_clean_code_reviewer_catches_all_python_gotchas_and_structural_smells(self, tmp_path: Path):
        bad_code_lines = [
            "from math import *",
            "",
            "class Bucket:",
            "    cache = []",
            "",
            "def configure(a, b, c, d, e, f, g, force: bool = True, recursive: bool = False):",
            "    # debt: temporary shortcut",
            "    fns = [lambda: idx for idx in range(3)]",
            "    if a:",
            "        if b:",
            "            if c:",
            "                if d:",
            "                    if e:",
            "                        return fns",
            "    return []",
            "",
            "async def fetch_remote(url: str):",
            "    import time",
            "    time.sleep(1)",
            "    try:",
            "        return int(url)",
            "    except ValueError:",
            "        raise RuntimeError('bad url')",
            "    finally:",
            "        return 0",
        ]
        # Append an >85-line function with >12 branches for length + McCabe checks
        long_fn = ["", "def mega_branch(x: int) -> int:", "    total = 0"]
        for i in range(14):
            long_fn.extend(
                [
                    f"    if x == {i}:",
                    f"        total += {i}",
                    f"    elif x == {i + 100}:",
                    f"        total -= {i}",
                ]
            )
        long_fn.extend([f"    step_{k} = total + {k}" for k in range(30)])
        long_fn.append("    return total")

        full_src = "\n".join(bad_code_lines + long_fn) + "\n"
        (tmp_path / "svc").mkdir(parents=True, exist_ok=True)
        (tmp_path / "svc" / "bad.py").write_text(full_src, encoding="utf-8")

        diff = (
            "diff --git a/svc/bad.py b/svc/bad.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/svc/bad.py\n"
            f"@@ -0,0 +1,{len(full_src.splitlines())} @@\n"
            + "\n".join(f"+{ln}" for ln in full_src.splitlines())
            + "\n"
        )
        findings = CleanCodeReviewer().review(diff, repo_root=tmp_path)
        rules = {f.rule for f in findings}
        expected = {
            "py-wildcard-import",
            "py-mutable-class-attr",
            "cc-too-many-params",
            "cc-boolean-trap",
            "cc-untracked-debt",
            "py-late-binding-closure",
            "cc-deep-nesting",
            "py-async-blocking-call",
            "py-reraise-without-context",
            "py-finally-jump",
            "cc-long-function",
            "cc-high-complexity",
        }
        assert expected.issubset(rules), f"Missing rules: {expected - rules}"

    def test_clean_code_attribution_downgrades_carried_over_smell(self, tmp_path: Path):
        _init_git_repo(tmp_path)
        legacy = (
            "class LegacyStore:\n"
            "    shared = []\n\n"
            "def add_one(x: int) -> int:\n"
            "    return x + 1\n"
        )
        (tmp_path / "svc" / "pricing.py").write_text(legacy, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "legacy"], cwd=tmp_path, check=True, capture_output=True)

        # Reformat `shared = []` -> `shared = [ ]` while adding tested clean function
        edited = (
            "class LegacyStore:\n"
            "    shared = [ ]\n\n"
            "def add_one(x: int) -> int:\n"
            "    return x + 1\n"
        )
        (tmp_path / "svc" / "pricing.py").write_text(edited, encoding="utf-8")
        (tmp_path / "tests" / "test_pricing.py").write_text(
            "from svc.pricing import add_one\n\ndef test_add_one():\n    assert add_one(2) == 3\n",
            encoding="utf-8",
        )
        diff = subprocess.run(
            ["git", "diff", "main"], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout

        eng = DevOpsEngine(tmp_path, DevOpsConfig())
        lp = eng.new_loop("Touch pricing formatting", files=["svc/pricing.py", "tests/test_pricing.py"])
        eng.start_build(lp)
        chain, _ = build_review_chain(["static"])
        res = eng.review(lp, diff, chain)
        assert res.code == OK
        assert res.data["attribution"]["carried_over"] >= 1


# ══════════════════════════════════════════════════════════════════════════════
# 2. Spec-to-Code Blueprint, Lite Red Lines, Granularity & G1-G15 / P1-P6 Gates
# ══════════════════════════════════════════════════════════════════════════════


class TestBlueprintGatesAndRouter:
    def test_lite_qualification_and_hard_upgrade_red_lines(self):
        ok_lite = LiteEligibilityGate.evaluate(
            requested_lite=True,
            title="Fix minor rounding edge case",
            files=["svc/pricing.py", "tests/test_pricing.py"],
            changed_lines=25,
            tier=RiskTier.LOW,
        )
        assert ok_lite.eligible is True

        # Escalates when touching public API / auth / high risk / cross-module
        escalated = LiteEligibilityGate.evaluate(
            requested_lite=True,
            title="Modify public API auth schema",
            files=["svc/pricing.py", "billing/ledger.py"],
            changed_lines=120,
            tier=RiskTier.HIGH,
        )
        assert escalated.eligible is False
        assert len(escalated.red_lines_triggered) >= 3

    def test_granularity_analyzer_splits_crud_bundle(self):
        bundled = RequirementItem(
            id="FR-001",
            title="Coupon CRUD management",
            statement="The service shall create, read, update, and delete coupon rules.",
            acceptance=["Given input, When invoked, Then succeeds."],
            source_ref="epic-42",
        )
        signals = GranularityAnalyzer.inspect(bundled)
        assert any(s.code == "G2-crud-bundle" for s in signals)
        split = GranularityAnalyzer.split_crud_requirement(bundled)
        assert len(split) == 4
        assert all(item.source_ref == "epic-42" for item in split)

    def test_spec_design_and_plan_phase_gates_catch_defects(self, tmp_path: Path):
        _init_git_repo(tmp_path)
        synth = DeterministicBlueprintSynthesizer()
        bundle = synth.synthesize(
            loop_id="L-001",
            title="Add tiered volume discount calculation",
            requirements_Input=["Apply 10% discount when quantity >= 10 and reject negative quantities"],
            files=["svc/pricing.py", "tests/test_pricing.py"],
            tier=RiskTier.MEDIUM,
            repo_root=tmp_path,
        )
        assert BlueprintPhaseGate.verify_spec(bundle.spec).passed is True
        assert bundle.design is not None
        assert BlueprintPhaseGate.verify_design(bundle.spec, bundle.design).passed is True
        assert BlueprintPhaseGate.verify_plan(bundle.spec, bundle.design, bundle.plan).passed is True

        # Break Spec: vague acceptance + modify without baseline + blocking OpenQuestion
        bad_spec = SpecDocument(
            loop_id="L-bad",
            title="Broken spec",
            background="bg",
            in_scope=["svc/pricing.py"],
            out_of_scope=["other"],
            requirements=[
                RequirementItem(
                    id="FR-001",
                    title="Fast response",
                    statement="System responds fast",  # missing shall/must
                    acceptance=["Given user, When clicked, Then good performance"],  # vague without numbers
                    change_type=ChangeType.MODIFY,
                    existing_baseline="N/A",  # forbidden on MODIFY
                ),
                RequirementItem(
                    id="NFR-001",
                    title="Latency",
                    statement="The system shall respond fast.",
                    req_type=RequirementType.NFR,
                    qas=QualityScenario("u", "req", "prod", "ok", "fast"),  # unquantified QAS
                ),
            ],
            open_questions=[OpenQuestion(id="OQ-1", question="Which currency?", blocking=True)],
        )
        spec_gate = BlueprintPhaseGate.verify_spec(bad_spec)
        assert spec_gate.passed is False
        failed_ids = {i.id for i in spec_gate.failed_items()}
        assert {"G3", "G5", "G8", "G9"}.issubset(failed_ids)

        # Break Plan: introduce placeholder + cyclic dependency + extra ungrounded TC
        bad_plan = TaskPlan(
            loop_id="L-001",
            goal="Goal",
            tasks=[
                TaskSpec(
                    id="T001",
                    title="First",
                    depends_on=["T002"],
                    covers_reqs=["FR-001"],
                    covers_cases=["TC-999"],
                    files_modify=["svc/pricing.py"],
                    files_test=["tests/test_pricing.py"],
                    steps=["TODO: similar to task 2"],
                    definition_of_done="TBD",
                ),
                TaskSpec(
                    id="T002",
                    title="Second",
                    depends_on=["T001"],
                    covers_reqs=["NFR-001"],
                    covers_cases=["TC-001"],
                    files_modify=["svc/pricing.py"],
                    files_test=["tests/test_pricing.py"],
                    steps=["Run pytest"],
                    definition_of_done="Passes",
                ),
            ],
        )
        plan_gate = BlueprintPhaseGate.verify_plan(bundle.spec, bundle.design, bad_plan)
        assert plan_gate.passed is False
        p_failed = {i.id for i in plan_gate.failed_items()}
        assert {"P1", "P2", "P5"}.issubset(p_failed)

    def test_phase_router_hand_edit_sync_rework_reset_and_compaction_snapshot(self, tmp_path: Path):
        _init_git_repo(tmp_path)
        ws = BlueprintWorkspace(tmp_path / ".neoagent-devops" / "artifacts" / "L-01" / "blueprint")
        assert detect_phase(ws).phase == BlueprintPhase.SPEC

        bundle = DeterministicBlueprintSynthesizer().synthesize(
            loop_id="L-01",
            title="Volume discount",
            requirements_Input=["Compute volume discount"],
            files=["svc/pricing.py", "tests/test_pricing.py"],
            tier=RiskTier.MEDIUM,
            repo_root=tmp_path,
        )
        ws.save(spec=bundle.spec, design=bundle.design, plan=bundle.plan, trace=bundle.traceability)
        assert detect_phase(ws).phase == BlueprintPhase.SPEC_GATE

        # Pass Spec, Design, Plan gates -> phase advances to DEVELOP
        plan = ws.load_plan()
        assert plan is not None and bundle.design is not None
        plan.gate_records.append(BlueprintPhaseGate.verify_spec(bundle.spec))
        plan.gate_records.append(BlueprintPhaseGate.verify_design(bundle.spec, bundle.design))
        plan.gate_records.append(BlueprintPhaseGate.verify_plan(bundle.spec, bundle.design, plan))
        ws.save(plan=plan)
        snap = detect_phase(ws)
        assert snap.phase == BlueprintPhase.DEVELOP
        assert snap.next_task_id == "T001"

        # Simulate manual status edit directly inside `tasks.md`
        md = ws.tasks_path.read_text(encoding="utf-8").replace("| pending |", "| passing |")
        ws.tasks_path.write_text(md, encoding="utf-8")
        assert detect_phase(ws).phase == BlueprintPhase.SDC_REVIEW

        # Compaction snapshot stays concise (<2400 chars) and captures active state
        compact = build_compaction_snapshot(ws)
        assert "sdc_review" in compact
        assert len(compact) <= 2400

        # Upstream Design rework resets tasks back to pending and clears design/plan/sdc gates
        after_reset = apply_rework_reset(ws, "design")
        assert after_reset.phase == BlueprintPhase.DESIGN_GATE


# ══════════════════════════════════════════════════════════════════════════════
# 3. Isolated Subagent TDD Execution, S/D/C Review, Acceptance & Surfaces
# ══════════════════════════════════════════════════════════════════════════════


class TestIsolatedSubagentTDDAndEndToEnd:
    def test_red_green_session_separation_model_tiers_and_clean_check_enforcement(self, tmp_path: Path):
        _init_git_repo(tmp_path)
        eng = DevOpsEngine(tmp_path, DevOpsConfig())
        lp = eng.new_loop(
            "Add tiered discount and cap validation",
            files=["svc/pricing.py", "tests/test_pricing.py"],
            tier=RiskTier.MEDIUM,
        )
        bp_res = eng.blueprint(
            lp,
            requirements=[
                "Apply 10% discount when quantity >= 10",
                "Reject discount percentages outside 0..50% with ValueError",
            ],
        )
        assert bp_res.code == OK
        ws = eng.blueprint_workspace(lp.id)
        plan = ws.load_plan()
        assert plan is not None and len(plan.tasks) == 2

        # Verify context pack contains Quality Stack and zero conversation history
        pack_md, layers = compile_task_context_pack(
            task=plan.tasks[0],
            spec=ws.load_spec(),  # type: ignore[arg-type]
            design=ws.load_design(),
            repo_root=tmp_path,
        )
        assert "TC-001" in pack_md
        assert "clean-code-5d" in layers

        # 1) Reject rubber-stamped CleanCodeCheck on T001 first
        bad_impl = ScriptedTaskImplementer(
            custom_outcomes={
                "T001": TaskExecutionOutcome(
                    task_id="T001",
                    status=ImplementerStatus.DONE,
                    evidence=TDDEvidence(refactor_note="none"),
                    clean_check=CleanCodeCheck(simplicity="ok"),  # vague!
                )
            }
        )
        fail_res = eng.execute_blueprint_tasks(lp, implementer=bad_impl, max_tasks=1)
        assert fail_res.code == ESCALATED
        assert "Clean-Code" in fail_res.message

        # 2) Run valid ScriptedTaskImplementer across all tasks -> verifies Red/Green distinct sessions!
        good_impl = ScriptedTaskImplementer()
        ok_res = eng.execute_blueprint_tasks(lp, implementer=good_impl)
        assert ok_res.code == OK
        assert ok_res.data["status"]["phase"] == "sdc_review"
        assert len(good_impl.calls) == 2
        # Standard tasks must use distinct RED vs GREEN session IDs
        assert good_impl.calls[0]["red_session_id"] != good_impl.calls[0]["green_session_id"]
        assert good_impl.calls[0]["red_session_id"] in lp.evidence["author_sessions"]
        assert good_impl.calls[0]["green_session_id"] in lp.evidence["author_sessions"]

        # 3) Run S/D/C 3D Compliance Gate -> PASS
        sdc_res = eng.run_phase_gate(lp, "sdc")
        assert sdc_res.code == OK
        assert sdc_res.data["status"]["phase"] == "acceptance"

        # 4) Run Black-Box Acceptance (`AT-*`) & Clean Archive -> COMPLETE
        acc_res = eng.run_acceptance(lp)
        assert acc_res.code == OK
        assert acc_res.data["status"]["phase"] == "complete"
        archived_rel = acc_res.data["acceptance"]["archived_to"]
        archive_dir = tmp_path / archived_rel
        assert (archive_dir / "spec.md").is_file()
        assert (archive_dir / "design.md").is_file()
        assert (archive_dir / "traceability.md").is_file()
        assert (archive_dir / "acceptance-cases.md").is_file()
        # Scratch `tasks.md` must NOT pollute the durable spec archive
        assert not (archive_dir / "tasks.md").exists()

    def test_sdc_contextual_reviewer_catches_undeclared_file_touch_in_review(self, tmp_path: Path):
        _init_git_repo(tmp_path)
        eng = DevOpsEngine(tmp_path, DevOpsConfig())
        lp = eng.new_loop("Scoped pricing update", files=["svc/pricing.py", "tests/test_pricing.py"], tier=RiskTier.MEDIUM)
        eng.blueprint(lp, requirements=["Clamp negative quantities to zero"])
        eng.execute_blueprint_tasks(lp, implementer=ScriptedTaskImplementer())

        # Touch an undeclared production file `unplanned/rogue.py`
        diff = (
            "diff --git a/unplanned/rogue.py b/unplanned/rogue.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/unplanned/rogue.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+def rogue() -> int:\n"
            "+    return 42\n"
        )
        chain, _ = build_review_chain(["static"])
        rev_res = eng.review(lp, diff, chain)
        # Undeclared file touch routes to REDESIGN / ESCALATED via Blueprint S/D/C + ScopeDrift!
        assert rev_res.code == ESCALATED
        open_rules = {f["rule"] for f in rev_res.data["open_findings"]}
        assert "sdc-undeclared-file-touch" in open_rules

    def test_strict_clarify_pauses_until_open_question_answered(self, tmp_path: Path):
        _init_git_repo(tmp_path)
        eng = DevOpsEngine(tmp_path, DevOpsConfig())
        lp = eng.new_loop("High concurrency fast pricing cache", files=["svc/pricing.py"])
        wait_res = eng.blueprint(lp, strict_clarify=True)
        assert wait_res.code == WAITING
        assert wait_res.data["open_questions"][0]["id"] == "OQ-001"

        ok_res = eng.blueprint(
            lp,
            strict_clarify=True,
            clarifications={"OQ-001": "p99 <= 50ms at 200 RPS"},
        )
        assert ok_res.code == OK
        # the human's answer is folded into the vague requirement, so the Spec Gate can verify it
        assert all(g["passed"] for g in ok_res.data["gates"].values())
        spec_md = (eng.blueprint_workspace(lp.id).dir / "spec.md").read_text(encoding="utf-8")
        assert "p99 <= 50ms at 200 RPS" in spec_md.split("## 4. Open Questions")[0]

    def test_model_tier_selector_scales_with_complexity(self):
        micro = TaskSpec(id="T1", title="Micro", granularity=TaskGranularity.MICRO, files_modify=["a.py"], covers_cases=["TC-1"])
        std = TaskSpec(id="T2", title="Std", granularity=TaskGranularity.STANDARD, files_modify=["a.py", "b.py"], covers_cases=["TC-1", "TC-2"])
        heavy = TaskSpec(id="T3", title="Heavy", granularity=TaskGranularity.STANDARD, files_modify=["a.py", "b.py", "c.py"], covers_reqs=["FR-1", "FR-2", "FR-3"])
        assert ModelTierSelector.select_tier(micro) == "cheap"
        assert ModelTierSelector.select_tier(std) == "standard"
        assert ModelTierSelector.select_tier(heavy) == "reasoner"

    def test_cli_mcp_and_studio_blueprint_and_standards_surfaces(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("HTTP_PROXY", raising=False)
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        _init_git_repo(tmp_path)

        # CLI flow: init -> standards -> new -> blueprint -> next-task -> gate sdc -> acceptance
        assert run_cli(["--root", str(tmp_path), "init"]) == 0
        assert run_cli(["--root", str(tmp_path), "--json", "standards", "--files", "svc/pricing.py"]) == 0
        capsys.readouterr()

        assert run_cli(["--root", str(tmp_path), "--json", "new", "Add loyalty tier discount", "--files", "svc/pricing.py", "tests/test_pricing.py"]) == 0
        created = json.loads(capsys.readouterr().out)
        lid = created["id"]

        assert run_cli(["--root", str(tmp_path), "--json", "blueprint", lid, "--req", "Apply 5% loyalty discount"]) == 0
        bp_out = json.loads(capsys.readouterr().out)
        assert bp_out["status"]["phase"] == "develop"

        assert run_cli(["--root", str(tmp_path), "--json", "next-task", lid, "--scripted"]) == 0
        capsys.readouterr()
        assert run_cli(["--root", str(tmp_path), "--json", "gate", lid, "--phase", "sdc"]) == 0
        capsys.readouterr()
        assert run_cli(["--root", str(tmp_path), "--json", "acceptance", lid]) == 0
        acc_out = json.loads(capsys.readouterr().out)
        assert acc_out["status"]["phase"] == "complete"

        # MCP tools: standards_compile, devops_blueprint, devops_phase_gate
        mcp = MCPDevOpsServer(workspace_root=tmp_path)
        tools = {t["name"] for t in mcp.tool_definitions()}
        assert {"devops_blueprint", "devops_phase_gate", "standards_compile"}.issubset(tools)
        std_resp = mcp.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "standards_compile", "arguments": {"files": ["svc/pricing.py"]}}}
        )
        assert std_resp is not None and std_resp["result"]["isError"] is False

        # Studio HTTP endpoints: /api/standards & /api/loops/<id>/blueprint
        srv = DesktopControlServer(workspace_root=tmp_path, host="127.0.0.1", port=0)
        base_url = srv.start_background()
        try:
            with urllib.request.urlopen(f"{base_url}/api/standards?files=svc/pricing.py", timeout=5) as r:
                std_json = json.loads(r.read().decode("utf-8"))
                assert "python" in std_json["languages"]
            with urllib.request.urlopen(f"{base_url}/api/loops/{lid}/blueprint", timeout=5) as r:
                bp_json = json.loads(r.read().decode("utf-8"))
                assert bp_json["phase"] == "complete"
        finally:
            srv.shutdown()

    def test_zero_internal_identifiers_and_clean_repo_privacy(self):
        from neoagent.devops.privacy import PrivacyPolicyConfig, scan_repository_privacy

        repo_root = Path(__file__).resolve().parent.parent
        # 1) Full repository DLP scan (workstation paths, internal URLs, proxy credentials)
        violations = scan_repository_privacy(repo_root, PrivacyPolicyConfig())
        assert violations == [], f"DLP violations detected: {[v.to_dict() for v in violations]}"

        # 2) Ensure skill/rule catalogs use only vendor-neutral public names
        legacy_prefixes = ("devflow" + "-", "sdd" + "-router", "sdd" + "-task", "g.fnm" + ".", "g.err" + ".", "g.res" + ".")
        scan_dirs = [repo_root / "neoagent", repo_root / "scripts" / "showcase", repo_root / "docs"]
        leaks: list[str] = []
        for d in scan_dirs:
            for p in d.rglob("*"):
                if not p.is_file() or p.suffix not in {".py", ".js", ".html", ".css", ".md", ".toml"}:
                    continue
                txt = p.read_text(encoding="utf-8", errors="replace").lower()
                for prefix in legacy_prefixes:
                    if prefix in txt:
                        leaks.append(f"{p.relative_to(repo_root)} -> {prefix}")
        assert leaks == [], f"Non-neutral identifiers detected: {leaks}"
