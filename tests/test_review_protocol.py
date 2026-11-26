"""Governed Review Protocol: attribution, grounding, security rules, routing, attestation, records."""

from __future__ import annotations

import difflib
import json
from pathlib import Path

import pytest

from neoagent.devops.autofix import CallableCodingBackend, CodingOutcome, parse_disputes
from neoagent.devops.config import DevOpsConfig, load_devops_config
from neoagent.devops.engine import ESCALATED, GATE_FAILED, OK, DevOpsEngine
from neoagent.devops.models import (
    Finding,
    FindingCategory,
    FindingStatus,
    LoopRecord,
    Severity,
    findings_to_sarif,
)
from neoagent.devops.review import (
    CATALOG,
    ContractDriftReviewer,
    ReviewProtocol,
    ScopeDriftReviewer,
    SecurityReviewer,
    TestEvidenceReviewer,
    Verdict,
    WeakTestReviewer,
    attribute_findings,
    ground_findings,
    invert_diff,
    offending_code_unchanged,
    resolve_rule,
)
from neoagent.devops.reviewers import (
    SkillOrchestratedReviewer,
    StaticASTReviewer,
    build_review_chain,
    parse_diff,
    parse_llm_findings,
)


def mk_diff(path: str, old: str, new: str) -> str:
    body = difflib.unified_diff(
        old.splitlines(), new.splitlines(), f"a/{path}", f"b/{path}", lineterm="", n=2
    )
    return f"diff --git a/{path} b/{path}\n" + "\n".join(body) + "\n"


def _loop(**kw: object) -> LoopRecord:
    return LoopRecord(id="L-test", title="t", **kw)  # type: ignore[arg-type]


# ── catalog ──────────────────────────────────────────────────────────────────


class TestCatalog:
    def test_every_rule_has_a_citation_and_valid_triage(self):
        for spec in CATALOG.values():
            assert spec.standard, spec.rule
            assert spec.title, spec.rule
            FindingCategory(spec.triage)  # raises on an invalid routing class

    def test_alias_resolution_for_free_form_llm_rule_names(self):
        assert resolve_rule("scope-creep").id == "scope-drift"
        assert resolve_rule("breaking-change").id == "public-contract-change"
        assert resolve_rule("definitely-not-a-rule") is None

    def test_sarif_carries_rule_metadata(self):
        f = Finding(rule="sec-weak-cipher", message="m", severity=Severity.MAJOR, file="a.py", line=3)
        sarif = findings_to_sarif([f])
        rule = sarif["runs"][0]["tool"]["driver"]["rules"][0]
        assert rule["id"] == "sec-weak-cipher"
        assert "CWE" in json.dumps(rule)


# ── invert + attribution ─────────────────────────────────────────────────────


class TestAttribution:
    def test_invert_diff_swaps_added_and_removed(self):
        d = mk_diff("a.py", "x = 1\n", "x = 2\n")
        inv = parse_diff(invert_diff(d))
        assert [dl.text for dl in inv if dl.added] == ["x = 1"]

    def test_introduced_carried_over_and_fixed(self, tmp_path: Path):
        old = (
            "import hashlib\n"
            "a = hashlib.md5(password.encode())\n"
            "b = hashlib.md5(token.encode())\n"
            "c = 1\n"
        )
        new = (
            "import hashlib\n"
            "a = hashlib.md5(  password.encode()  )\n"  # re-formatted only -> carried over
            "c = 1\n"
            "d = hashlib.sha1(secret.encode())\n"  # new offence -> introduced
        )
        diff = mk_diff("svc/auth.py", old, new)
        proto = ReviewProtocol(DevOpsConfig().review, tmp_path)
        out = proto.run(_loop(), diff, [SecurityReviewer()], 1)

        by_att = {f.evidence.split("=")[0].strip(): f.attribution for f in out.findings}
        assert by_att["a"] == "carried_over"
        assert by_att["d"] == "introduced"
        assert out.stats()["fixed_by_change"] == 1  # `b` was removed by the change
        carried = next(f for f in out.findings if f.attribution == "carried_over")
        assert carried.severity == Severity.INFO  # downgraded, never blocking
        introduced = next(f for f in out.findings if f.attribution == "introduced")
        assert introduced.severity.rank >= Severity.MINOR.rank

    def test_critical_is_never_downgraded_even_when_carried_over(self, tmp_path: Path):
        old = "tok = jwt.decode(t, verify=False)\n"
        new = "tok = jwt.decode( t, verify=False )\n"
        diff = mk_diff("api/jwt_util.py", old, new)
        out = ReviewProtocol(DevOpsConfig().review, tmp_path).run(_loop(), diff, [SecurityReviewer()], 1)
        assert out.findings and out.findings[0].severity == Severity.CRITICAL

    def test_carried_over_policy_drop_and_keep(self, tmp_path: Path):
        old = "h = hashlib.md5(password.encode())\n"
        new = "h = hashlib.md5(  password.encode())\n"
        diff = mk_diff("a.py", old, new)
        cfg = DevOpsConfig().review
        cfg.carried_over_policy = "drop"
        assert ReviewProtocol(cfg, tmp_path).run(_loop(), diff, [SecurityReviewer()], 1).findings == []
        cfg.carried_over_policy = "keep"
        kept = ReviewProtocol(cfg, tmp_path).run(_loop(), diff, [SecurityReviewer()], 1).findings
        assert kept and kept[0].severity.rank >= Severity.MINOR.rank

    def test_attribute_findings_direct(self):
        diff = mk_diff("a.py", "x = md5(a)\n", "x = md5(b)\n")
        fresh = [Finding(rule="r", message="m", file="a.py", line=1, evidence="x = md5(b)")]
        old = [Finding(rule="r", message="m", file="a.py", line=1, evidence="x = md5(a)")]
        res = attribute_findings(fresh, old, diff)
        assert res.counts() == {"introduced": 0, "carried_over": 1, "fixed_by_change": 0}


# ── grounding ────────────────────────────────────────────────────────────────


class TestGrounding:
    DIFF = mk_diff("pkg/mod.py", "a = 1\n", "a = 1\nresult = compute(user_input)\nreturn result\n")

    def _llm(self, **kw: object) -> Finding:
        base = dict(rule="llm-rule", message="m", source="neoagent-skills", file="pkg/mod.py", line=2)
        base.update(kw)
        return Finding(**base)  # type: ignore[arg-type]

    def test_keeps_grounded_and_snaps_line_drift(self):
        rep = ground_findings([self._llm(line=3, evidence="result = compute(user_input)")], self.DIFF)
        assert len(rep.kept) == 1 and rep.kept[0].line == 2 and rep.snapped == 1

    def test_drops_ghost_file_and_fabricated_evidence(self):
        ghosts = [
            self._llm(file="nope.py"),
            self._llm(evidence="os.system(cmd)"),
            self._llm(line=40),
        ]
        rep = ground_findings(ghosts, self.DIFF)
        assert rep.kept == []
        reasons = rep.to_dict()["drop_reasons"]
        assert reasons["file-not-in-diff"] == 1
        assert reasons["evidence-not-in-diff"] == 1
        assert rep.by_source["neoagent-skills"]["dropped"] == 3

    def test_deterministic_findings_are_never_grounded_away(self):
        det = Finding(rule="x", message="m", source="static", file="whatever.py", line=99)
        assert ground_findings([det], self.DIFF).kept == [det]

    def test_llm_contract_parses_evidence_and_standard(self):
        raw = '[{"severity":"major","file":"a.py","line":2,"rule":"r","message":"m",' \
              '"evidence":"x = 1","standard":"CWE-89"}]'
        f = parse_llm_findings(raw, "neoagent-skills:correctness")[0]
        assert f.evidence == "x = 1" and f.standard == "CWE-89"


# ── deterministic reviewers ──────────────────────────────────────────────────


class TestSecurityReviewer:
    @pytest.mark.parametrize(
        "path,line,rule",
        [
            ("a.py", "ctx = ssl._create_unverified_context()", None),  # not a rule: stays silent
            ("a.py", "cipher = AES.new(key, AES.MODE_ECB)", "sec-weak-cipher"),
            ("a.py", "token = random.choice(alphabet)", "sec-insecure-random"),
            ("a.py", "app.run(debug=True)", "sec-debug-enabled"),
            ("a.py", "claims = jwt.decode(t, options={'verify_signature': False})", "sec-jwt-unverified"),
            ("a.py", "z.extractall(dest)", "sec-archive-extract"),
            ("a.py", "p = tempfile.mktemp()", "sec-temp-file"),
            ("w.js", "el.innerHTML = userHtml;", "sec-xss-sink"),
            ("w.js", "localStorage.setItem('auth_token', t)", "sec-storage-secret"),
            ("M.java", "new ObjectInputStream(in)", "sec-java-deserialize"),
            ("m.xml", "select * from t where id = ${id}", "sec-mybatis-dollar"),
        ],
    )
    def test_rule_triggers(self, path: str, line: str, rule: str | None):
        hits = SecurityReviewer().scan_line(path, 1, line)
        if rule is None:
            assert hits == []
        else:
            assert any(h.rule == rule for h in hits), hits
            assert all(h.standard for h in hits)

    def test_secret_context_escalates_weak_hash(self):
        plain = SecurityReviewer().scan_line("a.py", 1, "etag = hashlib.md5(body)")[0]
        secret = SecurityReviewer().scan_line("a.py", 1, "pw_hash = hashlib.md5(password)")[0]
        assert secret.severity.rank > plain.severity.rank

    def test_comments_and_tests_are_ignored(self):
        assert SecurityReviewer().scan_line("a.py", 1, "# hashlib.md5(password)") == []
        diff = mk_diff("tests/test_a.py", "", "h = hashlib.md5(password)\n")
        assert SecurityReviewer().review(diff) == []

    def test_deep_mode_files_old_issues_as_debt(self, tmp_path: Path):
        src = tmp_path / "svc"
        src.mkdir()
        (src / "m.py").write_text(
            "import tempfile\nold = tempfile.mktemp()\nnew_line = 1\n", encoding="utf-8"
        )
        diff = mk_diff("svc/m.py", "import tempfile\nold = tempfile.mktemp()\n", (src / "m.py").read_text())
        out = ReviewProtocol(DevOpsConfig().review, tmp_path).run(
            _loop(), diff, [SecurityReviewer(deep=True)], 1
        )
        assert out.findings == []  # nothing the change introduced
        assert [d.rule for d in out.debt] == ["sec-temp-file"]
        # quick mode does not even look
        out2 = ReviewProtocol(DevOpsConfig().review, tmp_path).run(_loop(), diff, [SecurityReviewer()], 1)
        assert out2.debt == []


class TestStructuralReviewers:
    def test_scope_drift_blocks_source_but_only_warns_for_config(self):
        diff = mk_diff("billing/x.py", "", "x = 1\n") + mk_diff("devops.toml", "", "k = 1\n")
        found = ScopeDriftReviewer(["svc/"]).review(diff)
        sev = {f.file: f.severity for f in found}
        assert sev["billing/x.py"] == Severity.MAJOR
        assert sev["devops.toml"] == Severity.MINOR
        assert all(f.category == FindingCategory.USER_INPUT for f in found)

    def test_scope_drift_allows_tests_and_declared_paths(self):
        diff = mk_diff("svc/a.py", "", "x = 1\n") + mk_diff("tests/test_a.py", "", "y = 1\n")
        assert ScopeDriftReviewer(["svc/"]).review(diff) == []

    def test_missing_test_evidence(self):
        body = "".join(f"value_{i} = compute({i})\n" for i in range(10))
        assert TestEvidenceReviewer().review(mk_diff("svc/a.py", "", body))[0].rule == "change-without-tests"
        both = mk_diff("svc/a.py", "", body) + mk_diff("tests/test_a.py", "", "assert_ok = run()\n")
        assert TestEvidenceReviewer().review(both) == []

    def test_contract_drift(self):
        removed = mk_diff("svc/a.py", "def keep(a, b):\n    pass\n", "x = 1\n")
        assert ContractDriftReviewer().review(removed)[0].rule == "public-api-removed"
        changed = mk_diff("svc/a.py", "def f(a, b):\n    pass\n", "def f(a, b, c):\n    pass\n")
        assert ContractDriftReviewer().review(changed)[0].rule == "public-contract-change"
        compatible = mk_diff("svc/a.py", "def f(a, b):\n    pass\n", "def f(a, b, c=1):\n    pass\n")
        assert ContractDriftReviewer().review(compatible) == []
        private = mk_diff("svc/a.py", "def _f(a):\n    pass\n", "def _f(a, b):\n    pass\n")
        assert ContractDriftReviewer().review(private) == []

    def test_weak_tests(self, tmp_path: Path):
        tests = tmp_path / "tests"
        tests.mkdir()
        src = (
            "def test_no_assert():\n    run()\n\n"
            "def test_tautology():\n    assert 1 == 1\n\n"
            "def test_none_only():\n    assert run() is not None\n\n"
            "def test_good():\n    assert run() == 3\n"
        )
        (tests / "test_w.py").write_text(src, encoding="utf-8")
        diff = mk_diff("tests/test_w.py", "", src)
        rules = {f.rule for f in WeakTestReviewer().review(diff, repo_root=tmp_path)}
        assert rules == {"test-without-assertion", "tautological-assertion", "weak-assertion-only"}

    def test_default_chain_is_governed_and_cheap(self):
        chain, _ = build_review_chain(["static"])
        names = {r.name for r in chain}
        assert {"security", "contract-drift", "test-evidence", "weak-tests"} <= names
        assert all(getattr(r, "deterministic", False) for r in chain)
        bare, _ = build_review_chain(["static"], governed=False)
        assert {r.name for r in bare} == {"static-ast", "bug-patterns"}


# ── protocol: verdicts, vanish check, attestation, disputes ──────────────────


@pytest.fixture
def engine(tmp_path: Path) -> DevOpsEngine:
    return DevOpsEngine(tmp_path, DevOpsConfig())


class TestEngineIntegration:
    @staticmethod
    def _write_mutable_default(engine: DevOpsEngine) -> str:
        """The AST reviewer reads the worktree, so the file must exist next to the diff."""
        src = "def f(x, h=[]):\n    return h\n"
        (engine.root / "svc").mkdir(exist_ok=True)
        (engine.root / "svc" / "a.py").write_text(src, encoding="utf-8")
        return mk_diff("svc/a.py", "", src)

    def _start(self, engine: DevOpsEngine, files: list[str] | None = None) -> LoopRecord:
        loop = engine.new_loop("feature", files=files or ["svc/"])
        engine.start_build(loop)
        return loop

    def test_ai_fixable_findings_return_to_build_and_write_a_record(self, engine: DevOpsEngine):
        loop = self._start(engine)
        diff = self._write_mutable_default(engine)
        res = engine.review(loop, diff, [StaticASTReviewer()])
        assert res.code == GATE_FAILED
        assert res.data["verdict"] == Verdict.NEEDS_CHANGES.value
        rec = engine.ledger.artifact_dir(loop.id) / res.data["review_record"]
        text = rec.read_text(encoding="utf-8")
        assert "Reviewer independence" in text and "Resolution" in text
        assert str(engine.root) not in text  # records never embed absolute workstation paths
        payload = json.loads((rec.parent / "review-record.json").read_text(encoding="utf-8"))
        assert payload["rounds"][0]["round"] == 1
        brief = (rec.parent / "fix-brief.md").read_text(encoding="utf-8")
        assert "REVIEW RECEPTION PROTOCOL" in brief and "DISPUTE <finding-id>" in brief

    def test_scope_drift_escalates_with_decision_brief(self, engine: DevOpsEngine):
        loop = self._start(engine, files=["svc/"])
        diff = mk_diff("billing/x.py", "", "x = 1\n")
        res = engine.review(loop, diff, [StaticASTReviewer()])
        assert res.code == ESCALATED
        assert res.data["verdict"] == Verdict.NEEDS_HUMAN.value
        brief = (engine.ledger.artifact_dir(loop.id) / res.data["decision_brief"]).read_text(encoding="utf-8")
        assert "billing/x.py" in brief and "Options" in brief

    def test_breaking_contract_is_a_redesign_verdict(self, engine: DevOpsEngine):
        loop = self._start(engine)
        diff = mk_diff("svc/a.py", "def f(a, b):\n    pass\n", "def f(a, b, c):\n    pass\n")
        res = engine.review(loop, diff, [ContractDriftReviewer()])
        assert res.code == ESCALATED
        assert res.data["verdict"] == Verdict.REDESIGN.value

    def test_clean_change_passes_and_credits_fixes(self, engine: DevOpsEngine):
        loop = self._start(engine)
        diff = mk_diff("svc/a.py", "t = tempfile.mktemp()\n", "t = tempfile.mkstemp()\n")
        res = engine.review(loop, diff, [SecurityReviewer()])
        assert res.code == OK
        assert res.data["attribution"]["fixed_by_change"] == 1
        assert res.data["attestation"]["isolated"] is True

    def test_vanished_llm_finding_stays_open_while_code_is_unchanged(self, engine: DevOpsEngine):
        loop = self._start(engine)
        diff = mk_diff("svc/a.py", "", "def run(cmd):\n    return eval(cmd)\n")
        llm = Finding(
            rule="eval-use", message="eval on input", severity=Severity.MAJOR, source="neoagent-skills:x",
            file="svc/a.py", line=2, evidence="return eval(cmd)",
        )
        loop.findings.append(llm)
        kept = engine._merge_findings_on_rereview(loop, [], 2, diff_text=diff, verify_vanished=True)
        assert kept == 1 and llm.status == FindingStatus.OPEN
        # ...and is genuinely closed once the quoted code is gone
        fixed_diff = mk_diff("svc/a.py", "", "def run(cmd):\n    return parse(cmd)\n")
        kept = engine._merge_findings_on_rereview(loop, [], 3, diff_text=fixed_diff, verify_vanished=True)
        assert kept == 0 and llm.status == FindingStatus.FIXED

    def test_density_suppressed_finding_is_not_read_as_fixed(self, engine: DevOpsEngine):
        """Hiding a finding from the report is not evidence that the code was fixed."""
        loop = self._start(engine)
        info = Finding(rule="sec-weak-hash", message="m", severity=Severity.INFO, source="security",
                       file="svc/a.py", line=3, evidence="h = md5(x)")
        loop.findings.append(info)
        # Re-review raised the finding, but the density cap hid it from the report (fresh = []).
        engine._merge_findings_on_rereview(loop, [], 2, present_fps={info.fingerprint()})
        assert info.status == FindingStatus.OPEN
        # Once the reviewers stop raising it at all, absence is real and it is closed.
        engine._merge_findings_on_rereview(loop, [], 3, present_fps=set())
        assert info.status == FindingStatus.FIXED

    def test_offending_code_unchanged_ignores_deterministic_sources(self):
        f = Finding(rule="r", message="m", source="static", file="a.py", evidence="return eval(x)")
        assert offending_code_unchanged(f, mk_diff("a.py", "", "return eval(x)\n")) is False

    def test_dispute_keeps_finding_open_and_routes_to_human(self, engine: DevOpsEngine):
        loop = self._start(engine)
        f = Finding(rule="r", message="m", severity=Severity.MAJOR, file="svc/a.py", line=1)
        loop.findings.append(f)
        engine.respond(loop, f.id, "dev", "guarded by the caller, see test_x")
        assert f.status == FindingStatus.OPEN
        assert f.category == FindingCategory.HUMAN_DECISION
        assert f.resolution.startswith("DISPUTED by dev")
        with pytest.raises(ValueError):
            engine.respond(loop, f.id, "", "no reason")
        with pytest.raises(ValueError):
            engine.respond(loop, "F-missing", "dev", "x")

    def test_fixer_disputes_are_parsed_and_applied_by_autopilot(self, engine: DevOpsEngine):
        assert parse_disputes("ok\nDISPUTE F-1a2b: caller validates\nDISPUTE F-9: x") == {
            "F-1a2b": "caller validates",
            "F-9": "x",
        }
        loop = engine.new_loop("feature", files=["svc/"])
        diff = self._write_mutable_default(engine)

        def repair(lp: LoopRecord, brief: Path, root: Path, rnd: int) -> CodingOutcome:
            fid = lp.open_findings()[0].id
            return CodingOutcome(
                True, "disputed", session_id=f"fixer_{lp.id}_r{rnd}",
                disputes={fid: "the default is intentionally shared", "F-ghost": "hallucinated id"},
            )

        res = engine.autopilot(
            loop,
            reviewers=[StaticASTReviewer()],
            coder=CallableCodingBackend(repair_fn=repair),
            diff_supplier=lambda _l: diff,
        )
        assert res.code == ESCALATED  # the dispute surfaced as a human decision, not a silent loop
        assert any(f.resolution.startswith("DISPUTED") for f in loop.findings)
        assert loop.evidence["author_sessions"][0].startswith("fixer_")

    def test_isolation_is_measured_not_assumed(self, engine: DevOpsEngine):
        assert engine.harness_status()["layers"]["L5_independent_evaluation"][
            "generator_evaluator_isolated"
        ] is None  # nothing reviewed yet -> honest "unknown"

        loop = self._start(engine)
        loop.evidence["author_sessions"] = ["review-correctness"]  # author shares a reviewer session
        sk = SkillOrchestratedReviewer()
        sk.session_ids = ["review-correctness"]
        att = ReviewProtocol.attest(loop, [sk], "diff", 1)
        assert att["isolated"] is False and att["shared_sessions"] == ["review-correctness"]
        loop.evidence["review_attestations"] = [att]
        engine.ledger.save(loop)
        l5 = engine.harness_status()["layers"]["L5_independent_evaluation"]
        assert l5["generator_evaluator_isolated"] is False and l5["status"] == "violation"

    def test_forbidden_reviewer_inputs_break_isolation(self):
        class Peeker:
            name = "peek"
            context_inputs = ("diff", "author_transcript")

        att = ReviewProtocol.attest(_loop(), [Peeker()], "d", 1)  # type: ignore[list-item]
        assert att["isolated"] is False and att["forbidden_inputs"] == ["author_transcript"]


class TestConfig:
    def test_review_block_loads_and_validates(self, tmp_path: Path):
        (tmp_path / "devops.toml").write_text(
            '[review]\ncarried_over_policy = "keep"\ndeep_audit = true\n', encoding="utf-8"
        )
        cfg = load_devops_config(tmp_path)
        assert cfg.review.carried_over_policy == "keep" and cfg.review.deep_audit is True
        (tmp_path / "devops.toml").write_text('[review]\ncarried_over_policy = "bogus"\n', encoding="utf-8")
        with pytest.raises(ValueError):
            load_devops_config(tmp_path)


class TestCliAndMetrics:
    def _cli(self, tmp: Path, *argv: str) -> int:
        from neoagent.cli.devops_cli import run_cli

        return run_cli(["--root", str(tmp), *argv])

    def test_review_respond_record_rules_and_metrics(self, tmp_path: Path, capsys):
        assert self._cli(tmp_path, "init") == 0
        capsys.readouterr()
        assert self._cli(tmp_path, "--json", "new", "Harden auth", "--files", "svc/") == 0
        loop_id = json.loads(capsys.readouterr().out)["id"]
        assert self._cli(tmp_path, "build", loop_id) == 0

        diff_file = tmp_path / "change.diff"
        diff_file.write_text(
            mk_diff("svc/a.py", "", "def make():\n    return tempfile.mktemp()\n"), encoding="utf-8"
        )
        capsys.readouterr()
        assert self._cli(tmp_path, "review", loop_id, "--diff-file", str(diff_file)) == 2
        out = capsys.readouterr().out
        assert "Verdict: NEEDS_CHANGES" in out and "review-record-r1.md" in out

        assert self._cli(tmp_path, "--json", "status", loop_id) == 0
        finding_id = json.loads(capsys.readouterr().out)["findings"][0]["id"]
        assert self._cli(tmp_path, "respond", loop_id, finding_id, "--by", "dev", "--reason", "x") == 0

        capsys.readouterr()
        assert self._cli(tmp_path, "review-record", loop_id) == 0
        assert "Reviewer independence" in capsys.readouterr().out

        assert self._cli(tmp_path, "--json", "rules") == 0
        assert json.loads(capsys.readouterr().out)["total"] == len(CATALOG)

        assert self._cli(tmp_path, "--json", "metrics") == 0
        quality = json.loads(capsys.readouterr().out)["review_quality"]
        assert quality["rounds"] == 1 and quality["isolated_rate"] == 1.0
        assert quality["verdicts"] == {"needs_changes": 1}

    def test_review_record_requires_a_review(self, tmp_path: Path, capsys):
        assert self._cli(tmp_path, "init") == 0
        capsys.readouterr()
        assert self._cli(tmp_path, "--json", "new", "x") == 0
        loop_id = json.loads(capsys.readouterr().out)["id"]
        assert self._cli(tmp_path, "review-record", loop_id) == 1  # USAGE: nothing to show yet
