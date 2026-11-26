"""Run real `neo-devops` CLI sessions against synthetic demo workspaces and capture their output.

* :func:`run_story`        — closed-loop tour (init → scaffold → new → review → harness → privacy → ledger)
* :func:`run_review_story` — governed review protocol over two rounds: attribution, a repair,
  and escalation to an owner with a decision brief when the repair drifts out of scope.

Narration lines (prefixed with ``# ``) describe steps performed by the offline scripted coder;
they are rendered as comments, never as tool output.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from demo_workspace import (  # noqa: E402
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


if __name__ == "__main__":
    for cmd, text in run_story():
        print(f"$ {cmd}\n{text}\n")
