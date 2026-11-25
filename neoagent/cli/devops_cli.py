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

    waive_p = sub.add_parser("waive", help="Waive a specific finding with mandatory human sign-off & reason")
    waive_p.add_argument("id")
    waive_p.add_argument("finding")
    waive_p.add_argument("--by", required=True)
    waive_p.add_argument("--reason", required=True)

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
        _emit(args, status, human)
        return 0 if l6["privacy_violations_count"] == 0 else 2

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
            rev_names, skill_context=bundle, checklist_rel_path=cfg.skills.checklist_path
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
            rev_names, skill_context=bundle, checklist_rel_path=cfg.skills.checklist_path
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
    elif args.cmd == "approve":
        res = eng.approve(loop, args.point, args.by, args.note)
    elif args.cmd == "resume":
        res = eng.resume(loop, args.by, Stage(args.to) if args.to else None, args.note)
    else:  # pragma: no cover
        raise ValueError(f"Unknown command: {args.cmd}")

    _emit(args, res)
    if not args.json and res.code in (2, 4) and res.data.get("fix_brief"):
        print(f"  Fix brief written to: {res.data['fix_brief']}")
    return res.code


def entry_point() -> None:  # pragma: no cover
    raise SystemExit(run_cli())


if __name__ == "__main__":  # pragma: no cover
    entry_point()
