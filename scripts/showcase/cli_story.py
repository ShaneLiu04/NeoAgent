"""Run real `neo-devops` CLI sessions against synthetic demo workspaces and capture their output.

* :func:`run_story`        — closed-loop tour (init → scaffold → new → review → harness → privacy → ledger)
* :func:`run_review_story` — governed review protocol over two rounds: attribution, a repair,
  and escalation to an owner with a decision brief when the repair drifts out of scope.
* :func:`run_blueprint_story`  — Spec-to-Code Blueprint: synthesize, gate, isolated TDD, S/D/C review, acceptance.
* :func:`run_guardrail_story`  — strict-clarify Open Question pause and the Lite red-line upgrade.
* :func:`run_standards_story`  — 3-layer quality stack and the deterministic clean-code reviewer.

Narration lines (prefixed with ``# ``) describe steps performed by the offline scripted coder;
they are rendered as comments, never as tool output.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from demo_workspace import (  # noqa: E402
    apply_blueprint_ai_change,
    build_blueprint_demo,
    build_demo_workspace,
    build_review_demo,
    build_skill_pack,
    review_round2_edit,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

_LOOP_ID = re.compile(r"\b(L\d{8}-\d{3})\b")


def _run(args: list[str], cwd: Path, env: dict[str, str]) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "neoagent.cli.devops_cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return (proc.stdout + proc.stderr).rstrip()


def _neutralise(text: str, base: Path) -> str:
    """Replace throw-away temp paths so rendered output never shows workstation paths."""
    for variant in {str(base), str(base).replace("\\", "/"), str(base.resolve())}:
        text = text.replace(variant, "~/demo")
    text = re.sub(r"[A-Za-z]:\\[^\s]*?neoagent-showcase-[^\\/\s]+", "~/demo", text)
    return re.sub(r"~/demo[^\s'\"]*", lambda m: m.group(0).replace("\\", "/"), text)


def _env(base: Path, skills: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONIOENCODING": "utf-8",
        "NEOAGENT_SKILL_REPO": str(skills),
        "NEOAGENT_WORKSPACE": str(base / "agent-home"),
        "OPENAI_API_KEY": "",
        "DEEPSEEK_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "COLUMNS": "110",
    }


def run_story() -> list[tuple[str, str]]:
    base = Path(tempfile.mkdtemp(prefix="neoagent-showcase-"))
    repo, skills = build_demo_workspace(base)
    env = _env(base, skills)
    steps: list[tuple[str, list[str]]] = [
        ("neo-devops init", ["init"]),
        ("neo-devops scaffold --apply", ["scaffold", "--apply"]),
        ('neo-devops new "Add apply_discount pricing" --from-git --mode unattended', ["new", "Add apply_discount pricing", "--from-git", "--mode", "unattended"]),
    ]
    out: list[tuple[str, str]] = []
    for shown, args in steps:
        out.append((shown, _neutralise(_run(args, repo, env), base)))

    m = _LOOP_ID.search(out[-1][1])
    loop_id = m.group(1) if m else "L00000000-001"
    for shown, args in [
        (f"neo-devops build {loop_id}", ["build", loop_id]),
        (f"neo-devops review {loop_id}", ["review", loop_id]),
        (f"neo-devops status {loop_id}", ["status", loop_id]),
        ("neo-devops skills --stage review --files app/calculator.py", ["skills", "--stage", "review", "--files", "app/calculator.py"]),
        ("neo-devops skills --validate", ["skills", "--validate"]),
        ("neo-devops harness-audit", ["harness-audit"]),
        ("neo-devops privacy-scan", ["privacy-scan"]),
        ("neo-devops verify-ledger", ["verify-ledger"]),
    ]:
        out.append((shown, _neutralise(_run(args, repo, env), base)))
        if args[0] == "review":
            brief = repo / ".neoagent-devops" / "artifacts" / loop_id / "fix-brief.md"
            if brief.is_file():
                rel = f".neoagent-devops/artifacts/{loop_id}/fix-brief.md"
                out.append((f"cat {rel}", _neutralise(brief.read_text(encoding="utf-8").rstrip(), base)))
    return out


def run_review_story() -> list[tuple[str, str]]:
    base = Path(tempfile.mkdtemp(prefix="neoagent-showcase-"))
    repo = build_review_demo(base)
    env = _env(base, build_skill_pack(base / "skills"))
    out: list[tuple[str, str]] = []

    def step(shown: str, args: list[str]) -> str:
        text = _neutralise(_run(args, repo, env), base)
        out.append((shown, text))
        return text

    step("neo-devops init", ["init"])
    loop_id = _LOOP_ID.search(
        step('neo-devops new "Harden reset codes" --files svc/auth.py', ["new", "Harden reset codes", "--files", "svc/auth.py"])
    )
    loop_id = loop_id.group(1) if loop_id else "L00000000-001"
    step(f"neo-devops build {loop_id}", ["build", loop_id])
    step(f"neo-devops review {loop_id}", ["review", loop_id])

    review_round2_edit(repo)
    stat = subprocess.run(["git", "diff", "--stat", "main"], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")
    narration = "# AI fixer repairs the findings in fix-brief.md (scripted stand-in); its patch also edits billing/"
    out.append((narration, _neutralise(stat.stdout.rstrip(), base)))

    step(f"neo-devops review {loop_id}", ["review", loop_id])
    step(f"neo-devops status {loop_id}", ["status", loop_id])
    step(f"neo-devops review-record {loop_id} --round 2", ["review-record", loop_id, "--round", "2"])
    brief = repo / ".neoagent-devops" / "artifacts" / loop_id / "decision-brief.md"
    if brief.is_file():
        rel = f".neoagent-devops/artifacts/{loop_id}/decision-brief.md"
        out.append((f"cat {rel}", _neutralise(brief.read_text(encoding="utf-8").rstrip(), base)))
    return out


def _clip(text: str, width: int = 108, max_lines: int | None = None) -> str:
    """Trim long lines (and optionally the line count) so a terminal render stays readable."""
    lines = [ln if len(ln) <= width else ln[: width - 1] + "…" for ln in text.splitlines()]
    if max_lines is not None and len(lines) > max_lines:
        hidden = len(lines) - max_lines
        lines = [*lines[:max_lines], f"… ({hidden} more lines)"]
    return "\n".join(lines)


def _md_table(text: str, keep: list[str]) -> str:
    """Re-render a Markdown table as aligned plain text, keeping only the named columns."""
    rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in text.splitlines() if ln.startswith("|")]
    if len(rows) < 3:
        return text
    head, body = rows[0], rows[2:]
    idx = [head.index(k) for k in keep if k in head]
    cells = [[r[i].strip("`") for i in idx] for r in [head, *body]]
    widths = [max(len(r[i]) for r in cells) for i in range(len(idx))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in cells)


def _record_digest(record: str) -> str:
    """The attribution table and findings of a review record, without the boilerplate."""
    out: list[str] = []
    lines = record.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("## What this change is responsible for"):
            tbl = [x for x in lines[i + 1 : i + 8] if x.startswith("|")]
            out.append(_md_table("\n".join(tbl), ["Introduced", "Carried over (pre-existing, touched)", "Fixed by this change"]))
        if ln.startswith("## Findings and their resolution"):
            tbl = [x for x in lines[i + 1 :] if x.startswith("|")]
            out.append("")
            out.append(_md_table("\n".join(tbl), ["Severity", "Rule", "Where", "Attribution"]))
    return "\n".join(out)


def run_blueprint_story() -> list[tuple[str, str]]:
    """Spec-to-Code Blueprint, end to end: synthesize, gate, isolated TDD, S/D/C review, acceptance, archive."""
    base = Path(tempfile.mkdtemp(prefix="neoagent-showcase-"))
    repo, skills = build_blueprint_demo(base)
    env = _env(base, skills)
    out: list[tuple[str, str]] = []

    def step(shown: str, args: list[str], clip: int | None = None) -> str:
        text = _neutralise(_run(args, repo, env), base)
        out.append((shown, _clip(text, 108, clip) if clip else _clip(text)))
        return text

    step("neo-devops init", ["init"])
    created = step(
        'neo-devops new "Add tiered volume discount" --files app/pricing.py tests/test_pricing.py',
        ["new", "Add tiered volume discount", "--files", "app/pricing.py", "tests/test_pricing.py"],
    )
    m = _LOOP_ID.search(created)
    lid = m.group(1) if m else "L00000000-001"
    step(
        f'neo-devops blueprint {lid} --req "100+ units get 10% off" --req "total is never negative"',
        ["blueprint", lid, "--req", "100+ units get 10% off", "--req", "total is never negative"],
    )
    bp_dir = repo / ".neoagent-devops" / "artifacts" / lid / "blueprint"
    spec_md = (bp_dir / "spec.md").read_text(encoding="utf-8")
    out.append(("# spec.md: EARS statement, grounded baseline (read from the AST) and Given/When/Then criteria", _clip(_excerpt(spec_md, "### FR-001", 6))))
    listing = "\n".join(f"{p.stat().st_size:>6}  {p.name}" for p in sorted(bp_dir.iterdir()) if p.is_file() and p.suffix == ".md")
    out.append((f"ls -l .neoagent-devops/artifacts/{lid}/blueprint/*.md", listing))
    tasks_md = (bp_dir / "tasks.md").read_text(encoding="utf-8")
    out.append(("# every task is a micro-sized TDD unit with exact Consumes / Produces signatures", _clip(_excerpt(tasks_md, "### T001", 11))))
    step(f"neo-devops next-task {lid} --scripted", ["next-task", lid, "--scripted"])
    step(f"neo-devops gate {lid} --phase sdc", ["gate", lid, "--phase", "sdc"])
    step(f"neo-devops acceptance {lid}", ["acceptance", lid])
    archive = sorted((repo / "docs" / "specs" / "archive").glob("*/*"))
    out.append((
        "ls docs/specs/archive/*/   # tasks.md is process debris and never ships",
        "\n".join(p.name for p in archive),
    ))
    return out


def run_guardrail_story() -> list[tuple[str, str]]:
    """Blueprint guardrails: strict-clarify pause, answered Open Question, and a Lite red-line upgrade."""
    base = Path(tempfile.mkdtemp(prefix="neoagent-showcase-"))
    repo, skills = build_blueprint_demo(base)
    env = _env(base, skills)
    out: list[tuple[str, str]] = []

    def step(shown: str, args: list[str]) -> str:
        text = _neutralise(_run(args, repo, env), base)
        out.append((shown, _clip(text)))
        return text

    step("neo-devops init", ["init"])
    lid1 = _LOOP_ID.search(step('neo-devops new "Make checkout fast" --files app/pricing.py', ["new", "Make checkout fast", "--files", "app/pricing.py"]))
    lid1 = lid1.group(1) if lid1 else "L00000000-001"
    out.append(("# the request is unquantified: the engine asks instead of letting the LLM guess", ""))
    step(f"neo-devops blueprint {lid1} --strict-clarify", ["blueprint", lid1, "--strict-clarify"])
    step(
        f'neo-devops blueprint {lid1} --strict-clarify --answer "OQ-001=p99 <= 200ms at 100 rps"',
        ["blueprint", lid1, "--strict-clarify", "--answer", "OQ-001=p99 <= 200ms at 100 rps"],
    )
    lid2 = _LOOP_ID.search(
        step('neo-devops new "Rotate the reset-code secret" --files svc/auth.py --tier high', ["new", "Rotate the reset-code secret", "--files", "svc/auth.py", "--tier", "high"])
    )
    lid2 = lid2.group(1) if lid2 else "L00000000-002"
    out.append(("# Lite is a privilege: high-risk work cannot opt out of the full Spec/Design gates", ""))
    step(f"neo-devops blueprint {lid2} --lite", ["blueprint", lid2, "--lite"])
    return out


def run_standards_story() -> list[tuple[str, str]]:
    """3-Layer quality stack compiled into the coder prompt, then enforced by the deterministic clean-code reviewer."""
    base = Path(tempfile.mkdtemp(prefix="neoagent-showcase-"))
    repo, skills = build_blueprint_demo(base)
    apply_blueprint_ai_change(repo)
    env = _env(base, skills)
    out: list[tuple[str, str]] = []

    def step(shown: str, args: list[str]) -> str:
        text = _neutralise(_run(args, repo, env), base)
        out.append((shown, _clip(text, 112)))
        return text

    step("neo-devops init", ["init"])
    prompt = _neutralise(_run(["standards", "--files", "app/pricing.py", "--stage", "build"], repo, env), base)
    heads = [
        ln
        for ln in prompt.splitlines()
        if ln.startswith(("## NeoAgent", "### Layer", "#### Language", "- **Rung", "**Never-Cut"))
    ]
    out.append(('neo-devops standards --files app/pricing.py | grep -E "^(#|- \\*\\*Rung)"', _clip("\n".join(heads), 104)))
    rules = json.loads(_run(["--json", "rules"], repo, env)).get("rules", [])
    picked = [r for r in rules if r["rule"].startswith(("cc-", "py-"))]
    table = "\n".join(f"{r['rule']:<27}{r['dimension']:<16}{r['standard']}" for r in picked)
    out.append(('neo-devops rules | grep -E "^(cc|py)-"', _clip(table, 108, 8)))
    lid = _LOOP_ID.search(step('neo-devops new "Harden volume discount" --from-git', ["new", "Harden volume discount", "--from-git"]))
    lid = lid.group(1) if lid else "L00000000-001"
    step(f"neo-devops build {lid}", ["build", lid])
    out.append(("# legacy_tax() only gained type hints: its 7-parameter signature is carried over, not blamed on the author", ""))
    step(f"neo-devops review {lid}", ["review", lid])
    rec = _neutralise(_run(["review-record", lid], repo, env), base)
    out.append((f"neo-devops review-record {lid}   # attribution digest", _record_digest(rec)))
    return out


def _excerpt(text: str, start: str, count: int) -> str:
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith(start):
            return "\n".join(lines[i : i + count])
    return "\n".join(lines[:count])


if __name__ == "__main__":
    for cmd, text in run_story():
        print(f"$ {cmd}\n{text}\n")
