"""``neo-devops`` Command-Line Interface — NeoAgent closed-loop delivery CLI.

Designed for both human engineers (Rich readable output) and CI/CD / AI agents
(``--json`` structured output + deterministic exit codes ``0/1/2/3/4``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..devops.config import DEFAULT_DEVOPS_TOML, DEVOPS_CONFIG_NAME, load_devops_config
from ..devops.engine import USAGE, DevOpsEngine, StepResult
from ..devops.feedback import load_feedback_file
from ..devops.harness import RepoScaffold
from ..devops.ledger import AuditChainTamperedError, DevOpsLedger, LedgerError
from ..devops.metrics import render_devops_metrics_markdown
from ..devops.models import Mode, RiskTier, Stage
from ..devops.privacy import scan_repository_privacy
from ..devops.release import ReleaseError
from ..devops.reviewers import ReviewerBackendUnavailable, build_review_chain
from ..devops.vcs import (
    VcsError,
    changed_files,
    count_changed_lines,
    create_isolated_worktree,
    diff_against,
)
from ..skills.registry import get_skill_registry


def _emit(args: argparse.Namespace, payload: StepResult | dict[str, Any], human: str | None = None) -> None:
    obj = payload.to_dict() if isinstance(payload, StepResult) else payload
    if args.json:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    elif human is not None:
        print(human)
    elif isinstance(payload, StepResult):
        print(f"[{payload.stage}] {payload.message}")


_GATE_MARK = {"YES": "PASS", "NO": "FAIL", "WARN": "WARN"}


def _blueprint_detail_lines(cmd: str, res: StepResult) -> list[str]:
    """Human-readable detail block for the blueprint family of commands (empty for other commands)."""
    data = res.data or {}
    status = data.get("status") or {}
    lines: list[str] = []
    if cmd == "blueprint":
        bp = data.get("blueprint") or {}
        qual = bp.get("qualification") or {}
        lines.append(f"  Track: {str(bp.get('track', '')).upper()}  |  {qual.get('reason', '')}".rstrip(" |"))
        for rl in qual.get("red_lines_triggered", []):
            lines.append(f"  Lite red line -> upgraded to FULL: {rl}")
        for q in data.get("open_questions", []):
            lines.append(f"  OPEN {q.get('id', '')} [blocking] {q.get('question', '')}")
        gates = data.get("gates") or {}
        if gates:
            chips = "  ".join(f"{ph.upper()} {'PASS' if (g or {}).get('passed') else 'FAIL'}" for ph, g in gates.items())
            lines.append(f"  Gates: {chips}")
        for t in status.get("tasks", []):
            deps = ",".join(t.get("depends_on") or []) or "root"
            lines.append(f"  {t['id']} [{t['granularity']}/{t.get('model_tier', 'standard')}] after:{deps}  {t['title']}")
        if status.get("artifacts_dir"):
            names = ", ".join(sorted(Path(p).name for p in (bp.get("artifacts") or {}).values()))
            lines.append(f"  Artifacts: {names}")
    elif cmd == "gate":
        gate = data.get("gate") or {}
        for it in gate.get("items", []):
            mark = _GATE_MARK.get(it.get("verdict", ""), it.get("verdict", ""))
            tail = f" - {it['evidence']}" if it.get("verdict") != "YES" and it.get("evidence") else ""
            lines.append(f"  {mark} {it['id']:<4} {it['title']}{tail}"[:150])
        for f in data.get("sdc_findings", []):
            lines.append(f"  SDC {f.get('rule', '')} [{f.get('severity', '')}] {f.get('message', '')}"[:150])
    elif cmd == "next-task":
        run = data.get("tdd_run") or {}
        by_id = {t["id"]: t for t in status.get("tasks", [])}
        for tid in run.get("executed_tasks", []):
            t = by_id.get(tid, {})
            lines.append(f"  {tid} {t.get('status', '')}  [{t.get('granularity', '')}] tier={t.get('model_tier', '')}  {t.get('title', '')}"[:150])
            red, green = t.get("red_session", ""), t.get("green_session", "")
            if red and green and red != green:
                lines.append(f"      RED session {red}  ->  GREEN session {green}  (separate contexts)")
            elif green:
                lines.append(f"      single fresh session {green}")
        for pack in run.get("context_packs_written", []):
            lines.append(f"  Zero-history context pack: {Path(pack).name}")
        if status.get("total_tasks"):
            lines.append(f"  Progress: {status.get('passing_tasks', 0)}/{status['total_tasks']} tasks passing")
    elif cmd == "acceptance":
        acc = data.get("acceptance") or {}
        for c in acc.get("cases", []):
            lines.append(f"  {c.get('status', '')} {c.get('id', '')} ({c.get('req_id', '')}, {c.get('category', '')})  {c.get('scenario_gwt', '')}"[:150])
        if acc:
            lines.append(f"  Verdict: {acc.get('verdict', '')}  |  requirement coverage {float(acc.get('requirement_coverage', 0)):.0%}")
    return lines


def build_cli_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="neo-devops",
        description="NeoAgent AI DevOps Companion: Intake -> TDD Build -> Auto-Review/Fix -> CI -> Canary -> Feedback Loop",
    )
    p.add_argument("--root", default=".", help="Repository root directory (default: current dir)")
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Initialize .neoagent-devops/ ledger and devops.toml")

    new_p = sub.add_parser("new", help="Create a new closed-loop delivery item with risk & skill routing")
    new_p.add_argument("title")
    new_p.add_argument("--files", nargs="*", default=[], help="Impacted file paths")
    new_p.add_argument("--lines", type=int, default=0, help="Estimated changed lines")
    new_p.add_argument("--from-git", action="store_true", help="Infer files & lines from git diff")
    new_p.add_argument("--mode", choices=[m.value for m in Mode], default=Mode.ATTENDED.value)
    new_p.add_argument("--tier", choices=[t.value for t in RiskTier], help="Raise risk tier (cannot lower)")
    new_p.add_argument("--spec", default="", help="Requirement spec path or ticket URL")
    new_p.add_argument("--base", default=None, help="Base git branch")
    new_p.add_argument("--worktree", action="store_true", help="Create isolated git worktree for this loop")

    sub.add_parser("build", help="Advance INTAKE -> BUILD (TDD coding)").add_argument("id")

    rev_p = sub.add_parser("review", help="Run 3-layer review (Static AST + BugPatterns + optional NeoAgent LLM)")
    rev_p.add_argument("id")
    rev_p.add_argument("--reviewers", default="static", help="Comma-separated: static,neoagent")
    rev_p.add_argument("--diff-file", help="Read diff from file instead of git diff")
    rev_p.add_argument(
        "--deep",
        action="store_true",
        help="Deep audit: also scan whole touched files; old issues are filed as debt, not blockers",
    )

    sub.add_parser("pipeline", help="Execute CI build/lint/test/coverage pipeline & gate").add_argument("id")

    rel_p = sub.add_parser("release", help="Run progressive canary rollout with SLO health gate")
    rel_p.add_argument("id")
    rel_p.add_argument("--version", required=True, dest="rel_version")
    rel_p.add_argument("--dry-run", action="store_true", help="Evaluate flow without live deploy commands")

    fb_p = sub.add_parser("feedback", help="Triage post-release feedback & spawn follow-up loops")
    fb_p.add_argument("id")
    fb_p.add_argument("--input", required=True, help="Feedback file (.jsonl/.json/.csv/.txt)")

    auto_p = sub.add_parser("autopilot", help="Autonomously drive Loop through coding, review/fix, CI/fix, release, and feedback")
    auto_p.add_argument("id")
    auto_p.add_argument("--reviewers", default="static", help="Comma-separated: static,neoagent")
    auto_p.add_argument("--version", default="v0.1.0", dest="rel_version")
    auto_p.add_argument("--dry-run", action="store_true", default=False)
    auto_p.add_argument("--feedback", help="Optional post-release feedback file")
    auto_p.add_argument("--deep", action="store_true", help="Deep security audit of touched files")

    waive_p = sub.add_parser("waive", help="Waive a specific finding with mandatory human sign-off & reason")
    waive_p.add_argument("id")
    waive_p.add_argument("finding")
    waive_p.add_argument("--by", required=True)
    waive_p.add_argument("--reason", required=True)

    resp_p = sub.add_parser(
        "respond", help="Dispute a review finding with evidence (it stays open and goes to a human)"
    )
    resp_p.add_argument("id")
    resp_p.add_argument("finding")
    resp_p.add_argument("--by", required=True)
    resp_p.add_argument("--reason", required=True, help="Concrete evidence that refutes the finding")

    rr_p = sub.add_parser("review-record", help="Show the audit record of a loop's latest review round")
    rr_p.add_argument("id")
    rr_p.add_argument("--round", type=int, default=0, help="Round number (default: latest)")

    sub.add_parser("rules", help="List the review rule catalog (rule -> dimension -> standard)")

    bp_p = sub.add_parser(
        "blueprint",
        help="Synthesize Spec-to-Code Blueprint (`spec.md`, `design.md`, `tasks.md`, `traceability.md`) & run gates",
    )
    bp_p.add_argument("id")
    bp_p.add_argument("--req", action="append", default=[], help="Explicit requirement item (repeatable)")
    bp_p.add_argument("--lite", action="store_true", help="Request lightweight track (subject to 6 upgrade red lines)")
    bp_p.add_argument("--option", default="A", help="Preferred architectural option (A or B)")
    bp_p.add_argument("--strict-clarify", action="store_true", help="Pause on unquantified ambiguity with OpenQuestions")
    bp_p.add_argument("--answer", action="append", default=[], help="Answer an OpenQuestion, e.g. OQ-001=p99<=100ms")

    gate_p = sub.add_parser("gate", help="Run independent read-only Blueprint Phase Gate (spec|design|plan|sdc)")
    gate_p.add_argument("id")
    gate_p.add_argument("--phase", required=True, choices=["spec", "req", "design", "plan", "sdc"])
    gate_p.add_argument("--force-by", default="", help="Human override name when adjudicating a gate")

    nt_p = sub.add_parser("next-task", help="Execute ready Blueprint TDD task(s) in isolated Context Packs")
    nt_p.add_argument("id")
    nt_p.add_argument("--max-tasks", type=int, default=None, help="Max tasks to execute (default: all ready)")
    nt_p.add_argument("--scripted", action="store_true", help="Use deterministic scripted TDD verifier")
    nt_p.add_argument("--test-cmd", default="", help="Optional shell test command for scripted verifier")

    acc_p = sub.add_parser("acceptance", help="Run black-box Acceptance (`AT-*`) from `spec.md` and archive clean specs")
    acc_p.add_argument("id")
    acc_p.add_argument("--cmd", default="", dest="acceptance_cmd", help="Optional acceptance test command")

    std_p = sub.add_parser("standards", help="Compile 3-Layer Coding Standards Quality Stack for files & stage")
    std_p.add_argument("--files", nargs="*", default=[], help="Target source files for language inference")
    std_p.add_argument("--stage", default="build", help="Lifecycle stage (intake|build|review|pipeline|release)")

    app_p = sub.add_parser("approve", help="Record human approval at review / release / full_rollout")
    app_p.add_argument("id")
    app_p.add_argument("--point", required=True, choices=["review", "release", "full_rollout"])
    app_p.add_argument("--by", required=True)
    app_p.add_argument("--note", default="")

    res_p = sub.add_parser("resume", help="Resume an ESCALATED loop after human adjudication")
    res_p.add_argument("id")
    res_p.add_argument("--by", required=True)
    res_p.add_argument(
        "--to",
        choices=[s.value for s in (Stage.INTAKE, Stage.BUILD, Stage.REVIEW, Stage.PIPELINE, Stage.RELEASE)],
    )
    res_p.add_argument("--note", default="")

    st_p = sub.add_parser("status", help="Show status of one Loop or list all Loops")
    st_p.add_argument("id", nargs="?")

    sub.add_parser("metrics", help="Display DORA + AI Autonomy metrics")
    sub.add_parser("verify-ledger", help="Cryptographically verify SHA-256 hash-chained audit events")

    sk_p = sub.add_parser("skills", help="Discover, recommend, or validate SKILL.md skills")
    sk_p.add_argument("--stage", default="", help="Lifecycle stage (intake|build|review|pipeline|release|observe|learn)")
    sk_p.add_argument("--files", nargs="*", default=[], help="Touched files for language skill matching")
    sk_p.add_argument("--query", default="", help="Search query")
    sk_p.add_argument("--validate", action="store_true", help="Run Harness F1-F4 compliance audit across skills")

    scaf_p = sub.add_parser("scaffold", help="Diagnose or materialize 4-layer AI-native repo layout (L1)")
    scaf_p.add_argument("--apply", action="store_true", help="Create missing non-destructive skeleton files")

    sub.add_parser("harness-audit", help="Run full 7-Layer Harness L1-L7 & DLP privacy audit")
    sub.add_parser("privacy-scan", help="Scan repository for confidential terms, internal URLs, or workstation paths")

    desk_p = sub.add_parser("desktop", help="Launch the NeoAgent Studio desktop app")
    desk_p.add_argument("--port", type=int, default=0)
    desk_p.add_argument("--headless", action="store_true")
    return p


def run_cli(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = build_cli_parser()
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    try:
        return _dispatch_command(args, root)
    except (
        LedgerError,
        AuditChainTamperedError,
        ReleaseError,
        VcsError,
        ValueError,
        FileNotFoundError,
        ReviewerBackendUnavailable,
    ) as exc:
        if args.json:
            print(json.dumps({"code": USAGE, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return USAGE


def _dispatch_command(args: argparse.Namespace, root: Path) -> int:
    if args.cmd == "init":
        DevOpsLedger(root).init()
        cfg_file = root / DEVOPS_CONFIG_NAME
        if not cfg_file.exists():
            cfg_file.write_text(DEFAULT_DEVOPS_TOML, encoding="utf-8")
            note = f"Created {DEVOPS_CONFIG_NAME}"
        else:
            note = f"{DEVOPS_CONFIG_NAME} already exists (preserved)"
        _emit(args, {"code": 0, "message": note}, f"Initialized .neoagent-devops/ ; {note}")
        return 0

    if args.cmd == "skills":
        reg = get_skill_registry()
        if args.validate:
            reports = {s.name: s.validate_harness().to_dict() for s in reg.list_all()}
            passed_cnt = sum(1 for r in reports.values() if r["passed"])
            payload = {"total": len(reports), "passed": passed_cnt, "reports": reports}
            _emit(
                args,
                payload,
                f"Harness F1-F4 Skill Validation: {passed_cnt}/{len(reports)} passed across {len(reg.roots)} root(s)",
            )
            return 0
        if args.stage or args.files or args.query:
            matched = reg.recommend(
                stage=args.stage, files=list(args.files), query=args.query, limit=10
            )
            human = "\n".join(
                f"- {m.name} [{m.category}]: {m.description[:100]}" for m in matched
            ) or "(no matching skills)"
            _emit(args, {"skills": [m.to_summary_dict() for m in matched]}, human)
            return 0
        _emit(
            args,
            {"total": reg.count(), "skills": [s.to_summary_dict() for s in reg.list_all()]},
            reg.catalog_markdown(max_items=40),
        )
        return 0

    cfg = load_devops_config(root)
    eng = DevOpsEngine(root, cfg)

    if args.cmd == "verify-ledger":
        ok, count, msg = eng.ledger.verify_audit_chain()
        _emit(args, {"code": 0, "verified": ok, "events": count, "message": msg}, msg)
        return 0

    if args.cmd == "metrics":
        snap = eng.metrics_snapshot()
        _emit(args, snap, render_devops_metrics_markdown(snap))
        return 0

    if args.cmd == "scaffold":
        diag = RepoScaffold.diagnose(root)
        created = RepoScaffold.apply(root, diag) if args.apply else []
        if args.apply:
            diag = RepoScaffold.diagnose(root)
        payload = {"diagnosis": diag.to_dict(), "created": created}
        human = f"L1 Repo Scaffold mode={diag.mode} | missing={len(diag.missing_paths)} | created={created}"
        _emit(args, payload, human)
        return 0

    if args.cmd == "harness-audit":
        status = eng.harness_status()
        l6 = status["layers"]["L6_mechanical_constraints_and_dlp"]
        human = (
            f"Harness L1-L7 Audit: L1={status['layers']['L1_information_boundary']['status']} | "
            f"L6={l6['status']} (privacy_leaks={l6['privacy_violations_count']}, skills={l6['skills_f1_f4_compliant']})"
        )
        l5 = status["layers"]["L5_independent_evaluation"]
        human += f" | L5 isolated={l5['generator_evaluator_isolated']} (rounds attested={l5['attested_review_rounds']})"
        _emit(args, status, human)
        clean = l6["privacy_violations_count"] == 0 and l5["generator_evaluator_isolated"] is not False
        return 0 if clean else 2

    if args.cmd == "rules":
        from ..devops.review import CATALOG

        rows = [
            {"rule": s.id, "dimension": s.dimension, "standard": s.standard, "title": s.title,
             "triage": s.triage, "rework": s.rework}
            for s in CATALOG.values()
        ]
        human = "\n".join(
            f"{r['rule']:<26} {r['dimension']:<15} {r['standard']:<28} {r['title']}" for r in rows
        )
        _emit(args, {"total": len(rows), "rules": rows}, human)
        return 0

    if args.cmd == "privacy-scan":
        hits = scan_repository_privacy(root, cfg.privacy)
        payload = {
            "clean": len(hits) == 0,
            "count": len(hits),
            "violations": [h.to_dict() for h in hits],
        }
        human = "Privacy DLP Scan: CLEAN (0 violations)" if not hits else f"Privacy DLP Scan: {len(hits)} violation(s)!"
        _emit(args, payload, human)
        return 0 if not hits else 2

    if args.cmd == "standards":
        qstack = eng.compile_quality_stack(files=list(args.files), stage=args.stage)
        _emit(args, qstack.to_dict(), qstack.prompt_markdown)
        return 0

    if args.cmd == "desktop":
        from ..desktop.launcher import launch_desktop

        return launch_desktop(workspace_root=root, port=args.port, headless=args.headless)

    if args.cmd == "new":
        files, lines, diff_txt = list(args.files), args.lines, ""
        if args.from_git:
            base = args.base or cfg.base_branch
            files = changed_files(root, base)
            diff_txt = diff_against(root, base)
            lines = count_changed_lines(diff_txt)
        lp = eng.new_loop(
            args.title,
            files=files,
            changed_lines=lines,
            diff_text=diff_txt,
            mode=Mode(args.mode),
            spec_ref=args.spec,
            base=args.base,
            tier=RiskTier(args.tier) if args.tier else None,
        )
        if args.worktree:
            wt = create_isolated_worktree(root, lp.id, lp.base)
            lp.worktree_path = str(wt)
            eng.ledger.save(lp)
        reasons = lp.evidence.get("risk_reasons", [])
        human = (
            f"{lp.id} created | tier={lp.tier.value} | mode={lp.mode.value} | "
            f"skills={', '.join(lp.activated_skills) or 'none'}"
            + "".join(f"\n  - {r}" for r in reasons)
        )
        _emit(args, lp.to_dict(), human)
        return 0

    if args.cmd == "status" and not args.id:
        loops = eng.ledger.list_all()
        human = (
            "\n".join(
                f"{lp.id}  {lp.stage.value:<12} {lp.tier.value:<7} {lp.title}" for lp in loops
            )
            or "(no active loops)"
        )
        _emit(
            args,
            {"loops": [lp.to_dict() for lp in loops]},
            human,
        )
        return 0

    loop = eng.ledger.load(args.id)
    if args.cmd == "status":
        human = (
            f"{loop.id}  {loop.title}\n"
            f"stage={loop.stage.value}  tier={loop.tier.value}  mode={loop.mode.value}\n"
            f"review_rounds={loop.review_rounds}  pipeline_fixes={loop.pipeline_fixes}  "
            f"open_findings={len(loop.open_findings())}  approvals={[a['point'] for a in loop.approvals]}\n"
            f"skills={loop.activated_skills}"
        )
        if loop.stage == Stage.ESCALATED:
            human += f"\nESCALATED: {loop.escalation_reason}"
        _emit(args, loop.to_dict(), human)
        return 0

    if args.cmd == "build":
        res = eng.start_build(loop)
    elif args.cmd == "review":
        diff = (
            Path(args.diff_file).read_text(encoding="utf-8")
            if args.diff_file
            else diff_against(root, loop.base)
        )
        bundle = eng.skills.build_context_bundle(loop.activated_skills)
        rev_names = [n.strip() for n in args.reviewers.split(",") if n.strip()]
        chain, warns = build_review_chain(
            rev_names,
            skill_context=bundle,
            checklist_rel_path=cfg.skills.checklist_path,
            deep=args.deep or cfg.review.deep_audit,
        )
        for w in warns:
            print(f"Warning: {w}", file=sys.stderr)
        try:
            res = eng.review(loop, diff, chain)
        except ReviewerBackendUnavailable as exc:
            print(f"Warning: {exc}; falling back to deterministic AST + BugPattern review", file=sys.stderr)
            res = eng.review(loop, diff, [r for r in chain if r.name != "neoagent-skills"])
    elif args.cmd == "pipeline":
        res = eng.run_pipeline(loop)
    elif args.cmd == "release":
        res = eng.release(loop, args.rel_version, dry_run=args.dry_run)
    elif args.cmd == "feedback":
        res = eng.ingest_feedback(loop, load_feedback_file(Path(args.input)))
    elif args.cmd == "autopilot":
        bundle = eng.skills.build_context_bundle(loop.activated_skills)
        rev_names = [n.strip() for n in args.reviewers.split(",") if n.strip()]
        chain, _ = build_review_chain(
            rev_names,
            skill_context=bundle,
            checklist_rel_path=cfg.skills.checklist_path,
            deep=args.deep or cfg.review.deep_audit,
        )
        fb_items = load_feedback_file(Path(args.feedback)) if args.feedback else None
        res = eng.autopilot(
            loop,
            reviewers=chain,
            version=args.rel_version,
            dry_run_release=args.dry_run,
            feedback_items=fb_items,
        )
    elif args.cmd == "waive":
        eng.waive(loop, args.finding, args.by, args.reason)
        res = StepResult(0, loop.stage.value, f"Waived {args.finding} (by {args.by})")
    elif args.cmd == "respond":
        eng.respond(loop, args.finding, args.by, args.reason)
        res = StepResult(0, loop.stage.value, f"Disputed {args.finding} (by {args.by}); routed to a human")
    elif args.cmd == "review-record":
        art = eng.ledger.artifact_dir(loop.id)
        rounds = sorted(art.glob("review-record-r*.md"), key=lambda p: int(p.stem.rsplit("-r", 1)[1]))
        if args.round:
            rounds = [p for p in rounds if p.name == f"review-record-r{args.round}.md"]
        if not rounds:
            raise FileNotFoundError(f"No review record for {loop.id} yet — run `review` first")
        text = rounds[-1].read_text(encoding="utf-8")
        _emit(args, {"code": 0, "record": rounds[-1].name, "markdown": text}, text)
        return 0
    elif args.cmd == "approve":
        res = eng.approve(loop, args.point, args.by, args.note)
    elif args.cmd == "resume":
        res = eng.resume(loop, args.by, Stage(args.to) if args.to else None, args.note)
    elif args.cmd == "blueprint":
        answers: dict[str, str] = {}
        for item in args.answer or []:
            if "=" in item:
                k, v = item.split("=", 1)
                answers[k.strip()] = v.strip()
        res = eng.blueprint(
            loop,
            requirements=list(args.req) if args.req else None,
            requested_lite=bool(args.lite),
            selected_option=str(args.option or "A"),
            clarifications=answers or None,
            strict_clarify=bool(args.strict_clarify),
        )
    elif args.cmd == "gate":
        res = eng.run_phase_gate(loop, args.phase, force_by=args.force_by)
    elif args.cmd == "next-task":
        from ..devops.blueprint import ScriptedTaskImplementer

        impl = ScriptedTaskImplementer(test_command=args.test_cmd) if args.scripted else None
        res = eng.execute_blueprint_tasks(loop, implementer=impl, max_tasks=args.max_tasks)
    elif args.cmd == "acceptance":
        res = eng.run_acceptance(loop, acceptance_cmd=args.acceptance_cmd)
    else:  # pragma: no cover
        raise ValueError(f"Unknown command: {args.cmd}")

    _emit(args, res)
    if not args.json:
        for line in _blueprint_detail_lines(args.cmd, res):
            print(line)
    if not args.json and args.cmd == "review" and res.data.get("verdict"):
        att = res.data.get("attribution", {})
        print(
            f"  Verdict: {res.data['verdict'].upper()}  |  introduced={att.get('introduced', 0)} "
            f"carried_over={att.get('carried_over', 0)} fixed_by_change={att.get('fixed_by_change', 0)} "
            f"debt={att.get('debt', 0)}  |  isolated={res.data['attestation'].get('isolated')}"
        )
        if res.data.get("review_record"):
            print(f"  Review record: {res.data['review_record']}")
    if not args.json and res.code in (2, 4) and res.data.get("fix_brief"):
        print(f"  Fix brief written to: {res.data['fix_brief']}")
    if not args.json and res.data.get("decision_brief"):
        print(f"  Decision brief (needs an owner): {res.data['decision_brief']}")
    return res.code


def entry_point() -> None:  # pragma: no cover
    raise SystemExit(run_cli())


if __name__ == "__main__":  # pragma: no cover
    entry_point()
