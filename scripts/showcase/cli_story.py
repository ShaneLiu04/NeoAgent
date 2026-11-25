"""Run a real `neo-devops` CLI session against the synthetic demo workspace and capture output."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from demo_workspace import build_demo_workspace  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def run_story() -> list[tuple[str, str]]:
    base = Path(tempfile.mkdtemp(prefix="neoagent-showcase-"))
    repo, skills = build_demo_workspace(base)
    env = {
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
    steps: list[tuple[str, list[str]]] = [
        ("neo-devops init", ["init"]),
        ("neo-devops scaffold --apply", ["scaffold", "--apply"]),
        ('neo-devops new "Add apply_discount pricing" --from-git --mode unattended', ["new", "Add apply_discount pricing", "--from-git", "--mode", "unattended"]),
    ]
    out: list[tuple[str, str]] = []
    for shown, args in steps:
        out.append((shown, _neutralise(_run(args, repo, env), base)))

    m = re.search(r"\b(L\d{8}-\d{3})\b", out[-1][1])
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


if __name__ == "__main__":
    for cmd, text in run_story():
        print(f"$ {cmd}\n{text}\n")
