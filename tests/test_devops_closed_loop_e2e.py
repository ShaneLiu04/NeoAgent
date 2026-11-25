"""
End-to-End Closed-Loop Tests for `neoagent/devops/` Engine, Canary, Self-Healing, Feedback & CLI.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from neoagent.cli.devops_cli import run_cli
from neoagent.devops.autofix import CallableCodingBackend, CodingOutcome
from neoagent.devops.config import load_devops_config
from neoagent.devops.engine import ESCALATED, GATE_FAILED, OK, WAITING, DevOpsEngine
from neoagent.devops.feedback import load_feedback_file
from neoagent.devops.ledger import AuditChainTamperedError
from neoagent.devops.models import LoopRecord, Mode, RiskTier, Stage
from neoagent.devops.release import build_command_release_hooks
from neoagent.devops.reviewers import BugPatternReviewer, StaticASTReviewer


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def demo_git_project(tmp_path: Path) -> Path:
    """Create a realistic miniature Python git repository with tests and canary scripts."""
    repo = tmp_path / "company_service"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "dev@company.local")
    _git(repo, "config", "user.name", "Company Engineer")

    (repo / "app").mkdir()
    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "calculator.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(
        "from app.calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )

    # Canary deploy / rollback / metrics helper script (fast zero-sleep hooks used in tests)
    (repo / "ops_sim.py").write_text(
        "import json, pathlib, sys\n"
        "mode = sys.argv[1]\n"
        "state = pathlib.Path('.canary_sim.json')\n"
        "if mode == 'deploy':\n"
        "    state.write_text(json.dumps({'version': sys.argv[2], 'pct': int(sys.argv[3])}), encoding='utf-8')\n"
        "elif mode == 'rollback':\n"
        "    state.write_text(json.dumps({'rolled_back': True, 'version': sys.argv[2]}), encoding='utf-8')\n"
        "elif mode == 'metrics_healthy':\n"
        "    print(json.dumps({'requests': 1200, 'error_rate': 0.001, 'p95_ms': 42.0}))\n"
        "elif mode == 'metrics_breach':\n"
        "    pct = int(sys.argv[3])\n"
        "    err = 0.08 if pct >= 25 else 0.001\n"
        "    print(json.dumps({'requests': 1200, 'error_rate': err, 'p95_ms': 42.0}))\n",
        encoding="utf-8",
    )

    py = sys.executable.replace("\\", "/")
    (repo / "devops.toml").write_text(
        f'[project]\nbase_branch = "main"\n\n'
        f'[budget]\nmax_review_rounds = 3\nmax_pipeline_fixes = 3\n\n'
        f'[[pipeline.steps]]\nname = "pytest"\ncmd = ["{py}", "-m", "pytest", "tests", "-q"]\n\n'
        f'[release]\nstages = [[5, 1], [25, 1], [100, 1]]\n'
        f'deploy_cmd = "{py} ops_sim.py deploy {{version}} {{percent}}"\n'
        f'rollback_cmd = "{py} ops_sim.py rollback {{version}}"\n'
        f'metrics_cmd = "{py} ops_sim.py metrics_healthy {{version}} {{percent}}"\n\n'
        f'[feedback]\nhotfix_min_reach = 2\n',
        encoding="utf-8",
    )

    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial baseline")
    return repo


class TestClosedLoopEndToEnd:
    def test_full_self_healing_autopilot_from_coding_to_user_feedback(
        self, demo_git_project: Path
    ):
        """
        Proves the complete closed loop:
          1. AI Coder writes feature with TWO defects:
             - Review defect: mutable default arg `history=[]` (caught by StaticASTReviewer!)
             - Functional bug: `discount` subtracts wrong formula (caught by pytest in PIPELINE!)
          2. Review Round 1 REJECTS -> Auto-Fixer removes mutable default -> Review Round 2 PASSES!
          3. Pipeline Run 1 FAILS (`CODE_DEFECT`) -> Auto-Fixer fixes math -> Re-Review + Pipeline Run 2 PASSES!
          4. Progressive Canary rolls out 5% -> 25% -> 100% -> transitions to OBSERVE!
          5. Online User Feedback ingested -> clusters regression & feature requests ->
             automatically spawns `[hotfix]` and `NEXT_ITERATION` child Loops with `srs-draft.md` -> DONE!
          6. DORA + Autonomy metrics and SHA-256 audit chain verified!
        """

        def implement_fn(loop: LoopRecord, brief_path: Path, root: Path) -> CodingOutcome:
            (root / "app" / "calculator.py").write_text(
                "def add(a: int, b: int) -> int:\n"
                "    return a + b\n\n\n"
                "def apply_discount(price: float, pct: float, history=[]) -> float:\n"
                "    return price - pct\n",
                encoding="utf-8",
            )
            (root / "tests" / "test_calc.py").write_text(
                "from app.calculator import add, apply_discount\n\n\n"
                "def test_add():\n"
                "    assert add(2, 3) == 5\n\n\n"
                "def test_apply_discount():\n"
                "    assert apply_discount(100.0, 0.2) == 80.0\n",
                encoding="utf-8",
            )
            return CodingOutcome(
                True,
                "Implemented initial apply_discount feature",
                modified_files=["app/calculator.py", "tests/test_calc.py"],
                cost_usd=0.012,
            )

        def repair_fn(loop: LoopRecord, fix_brief_path: Path, root: Path, round_no: int) -> CodingOutcome:
            brief = fix_brief_path.read_text(encoding="utf-8")
            calc = root / "app" / "calculator.py"
            if "mutable-default" in brief:
                # Repair 1: remove mutable default argument, math bug remains for CI to catch
                calc.write_text(
                    "def add(a: int, b: int) -> int:\n"
                    "    return a + b\n\n\n"
                    "def apply_discount(price: float, pct: float) -> float:\n"
                    "    return price - pct\n",
                    encoding="utf-8",
                )
                return CodingOutcome(True, "Removed mutable default arg", ["app/calculator.py"], cost_usd=0.008)

            # Repair 2: fix CI test failure (`price * (1.0 - pct)`)
            calc.write_text(
                "def add(a: int, b: int) -> int:\n"
                "    return a + b\n\n\n"
                "def apply_discount(price: float, pct: float) -> float:\n"
                "    return round(price * (1.0 - pct), 2)\n",
                encoding="utf-8",
            )
            return CodingOutcome(True, "Fixed discount formula", ["app/calculator.py"], cost_usd=0.009)

        # Prepare online user feedback JSONL (post-release regression + feature request)
        feedback_file = demo_git_project / "online_feedback.jsonl"
        feedback_rows = [
            {
                "id": "fb-1",
                "text": "优惠券折扣后负数金额报错 crash 500 error when coupon > 100%",
                "severity": "critical",
                "version": "v1.2.0",
                "reach": 5,
            },
            {
                "id": "fb-2",
                "text": "优惠券折扣负数金额异常 crash 500 error when coupon > 100%",
                "severity": "high",
                "version": "v1.2.0",
                "reach": 4,
            },
            {
                "id": "fb-3",
                "text": "希望能支持批量导出账单 CSV export feature request",
                "severity": "medium",
                "reach": 6,
            },
        ]
        feedback_file.write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in feedback_rows),
            encoding="utf-8",
        )
        fb_items = load_feedback_file(feedback_file)

        cfg = load_devops_config(demo_git_project)
        engine = DevOpsEngine(demo_git_project, cfg)
        loop = engine.new_loop(
            "Add apply_discount pricing calculation",
            files=["app/calculator.py", "tests/test_calc.py"],
            mode=Mode.UNATTENDED,
            tier=RiskTier.LOW,
        )

        fast_hooks = build_command_release_hooks(
            cfg.release, demo_git_project, dry_run=False, sleep=lambda _: None
        )
        coder = CallableCodingBackend(implement_fn=implement_fn, repair_fn=repair_fn)
        reviewers = [StaticASTReviewer(), BugPatternReviewer(cfg.skills.checklist_path)]

        res = engine.autopilot(
            loop,
            reviewers=reviewers,
            coder=coder,
            version="v1.2.0",
            dry_run_release=False,
            release_hooks=fast_hooks,
            feedback_items=fb_items,
        )

        assert res.code == OK, res.message
        assert loop.stage == Stage.DONE
        assert loop.review_rounds == 1  # Rejected once in review, then passed twice!
        assert loop.pipeline_fixes == 1  # Repaired once after pytest failure!
        assert loop.evidence["release"]["status"] == "promoted"
        assert loop.evidence["release"]["completed_percent"] == 100

        spawned_ids = loop.evidence["feedback"]["spawned_loops"]
        assert len(spawned_ids) >= 1
        children = [engine.ledger.load(cid) for cid in spawned_ids]
        assert any("[hotfix]" in c.title for c in children)
        for child in children:
            srs = engine.ledger.artifact_dir(child.id) / "srs-draft.md"
            assert srs.is_file()

        # Verify DORA + AI Autonomy metrics & cryptographic ledger integrity
        metrics = engine.metrics_snapshot()
        assert metrics["audit_chain_verified"] is True
        assert metrics["autonomy"]["self_heal_rate"] == 1.0
        assert metrics["autonomy"]["auto_converge_rate"] == 1.0
        assert metrics["autonomy"]["total_ai_cost_usd"] > 0
        valid, count, _ = engine.ledger.verify_audit_chain()
        assert valid is True and count >= 8

        # Tampering with a single field in events.jsonl must raise AuditChainTamperedError
        events_path = engine.ledger.events_path
        raw = events_path.read_text(encoding="utf-8")
        events_path.write_text(raw.replace("v1.2.0", "v9.9.9", 1), encoding="utf-8")
        with pytest.raises(AuditChainTamperedError):
            engine.ledger.verify_audit_chain()

    def test_canary_slo_breach_triggers_instant_rollback_and_hotfix(
        self, demo_git_project: Path
    ):
        cfg_path = demo_git_project / "devops.toml"
        cfg_path.write_text(
            cfg_path.read_text(encoding="utf-8").replace("metrics_healthy", "metrics_breach"),
            encoding="utf-8",
        )
        (demo_git_project / "app" / "calculator.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a + b\n\n\n"
            "def mul(a: int, b: int) -> int:\n    return a * b\n",
            encoding="utf-8",
        )

        cfg = load_devops_config(demo_git_project)
        engine = DevOpsEngine(demo_git_project, cfg)
        loop = engine.new_loop(
            "Add multiply", files=["app/calculator.py"], mode=Mode.UNATTENDED, tier=RiskTier.LOW
        )

        assert engine.start_build(loop).code == OK
        diff = subprocess.run(
            ["git", "diff", "main"], cwd=demo_git_project, capture_output=True, text=True, check=True
        ).stdout
        assert engine.review(loop, diff, [StaticASTReviewer()]).code == OK
        assert engine.run_pipeline(loop).code == OK

        fast_hooks = build_command_release_hooks(
            cfg.release, demo_git_project, dry_run=False, sleep=lambda _: None
        )
        rel_out = engine.release(loop, "v1.3.0", dry_run=False, hooks=fast_hooks)
        assert rel_out.code == GATE_FAILED
        assert loop.stage == Stage.ROLLED_BACK
        # 5% passed, 25% breached -> rolled back before 100%!
        assert loop.evidence["release"]["completed_percent"] == 5
        sim_state = json.loads((demo_git_project / ".canary_sim.json").read_text(encoding="utf-8"))
        assert sim_state["rolled_back"] is True

        # Ingesting empty feedback on ROLLED_BACK automatically synthesizes rollback hotfix loop!
        fb_out = engine.ingest_feedback(loop, [])
        assert fb_out.code == OK
        assert len(fb_out.data["spawned_loops"]) == 1

    def test_high_risk_monotonic_floor_and_human_approval_gates(
        self, demo_git_project: Path
    ):
        auth_dir = demo_git_project / "auth"
        auth_dir.mkdir()
        (auth_dir / "login.py").write_text(
            "def verify_jwt(tok: str) -> bool:\n    return bool(tok)\n", encoding="utf-8"
        )

        cfg = load_devops_config(demo_git_project)
        engine = DevOpsEngine(demo_git_project, cfg)

        # Engineer attempts to force `LOW` risk + `UNATTENDED` while touching `auth/login.py`
        loop = engine.new_loop(
            "Modify auth token check",
            files=["auth/login.py"],
            mode=Mode.UNATTENDED,
            tier=RiskTier.LOW,
        )
        assert loop.tier == RiskTier.HIGH  # Monotonic floor prevented downgrade!
        assert loop.mode == Mode.ATTENDED  # HIGH risk forced ATTENDED mode!

        engine.start_build(loop)

        # Review passes technical checks, yet returns WAITING (3) awaiting human sign-off
        rev = engine.review(loop, "", [StaticASTReviewer()])
        assert rev.code == WAITING
        assert engine.approve(loop, "review", by="sec-lead").code == OK
        assert loop.stage == Stage.PIPELINE

        # Pipeline runs after review approval
        assert engine.run_pipeline(loop).code == OK

        fast_hooks = build_command_release_hooks(
            cfg.release, demo_git_project, dry_run=False, sleep=lambda _: None
        )
        # Release requires `release` approval first, then pauses before 100% awaiting `full_rollout`
        rel1 = engine.release(loop, "v2.0.0", dry_run=False, hooks=fast_hooks)
        assert rel1.code == WAITING
        engine.approve(loop, "release", by="sre-lead")

        rel2 = engine.release(loop, "v2.0.0", dry_run=False, hooks=fast_hooks)
        assert rel2.code == WAITING  # Paused after 5% & 25%, before 100% full_rollout!
        assert loop.evidence["release"]["completed_percent"] == 25
        engine.approve(loop, "full_rollout", by="sre-lead")

        rel3 = engine.release(loop, "v2.0.0", dry_run=False, hooks=fast_hooks)
        assert rel3.code == OK
        assert loop.stage == Stage.OBSERVE

    def test_env_blocker_immediately_escalates_without_corrupting_code(
        self, demo_git_project: Path
    ):
        cfg_path = demo_git_project / "devops.toml"
        cfg_path.write_text(
            '[project]\nbase_branch = "main"\n\n'
            '[[pipeline.steps]]\nname = "missing-tool"\ncmd = ["nonexistent_binary_xyz_999"]\n',
            encoding="utf-8",
        )
        repair_called = {"count": 0}

        def should_not_repair(
            loop: LoopRecord, brief: Path, root: Path, round_no: int
        ) -> CodingOutcome:
            repair_called["count"] += 1
            return CodingOutcome(True, "should never run")

        cfg = load_devops_config(demo_git_project)
        engine = DevOpsEngine(demo_git_project, cfg)
        loop = engine.new_loop("Test env blocker", mode=Mode.UNATTENDED, tier=RiskTier.LOW)
        out = engine.autopilot(
            loop,
            reviewers=[StaticASTReviewer()],
            coder=CallableCodingBackend(repair_fn=should_not_repair),
        )
        assert out.code == ESCALATED
        assert repair_called["count"] == 0
        assert loop.stage == Stage.ESCALATED

    def test_cli_json_mode(self, demo_git_project: Path, capsys):
        code = run_cli(["--root", str(demo_git_project), "--json", "init"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert "ledger" in payload or "config" in payload or payload

        code_m = run_cli(["--root", str(demo_git_project), "--json", "metrics"])
        assert code_m == 0
        m_payload = json.loads(capsys.readouterr().out)
        assert m_payload["audit_chain_verified"] is True
