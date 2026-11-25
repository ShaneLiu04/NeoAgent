"""Git VCS introspection, review-package builder, and Git Worktree isolation.

Supports ``using-git-worktrees`` and ``subagent-driven-development`` workflows:
- Isolated worktrees keep AI coding experiments off the engineer's main branch.
- ``build_review_package`` writes commit log + diffstat + unified diff to an
  artifact file so reviewer sub-agents read one bounded file instead of bloating
  orchestrator context.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class VcsError(Exception):
    """Raised when a git command fails."""


def _run_git(root: Path, *args: str, timeout: int = 60) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VcsError(f"git execution failed: {exc}") from exc
    if proc.returncode != 0:
        raise VcsError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def is_git_repo(root: Path) -> bool:
    try:
        out = _run_git(root, "rev-parse", "--is-inside-work-tree")
        return out.strip() == "true"
    except VcsError:
        return False


def diff_against(root: Path, base: str = "main", context_lines: int = 3) -> str:
    """Return unified diff combining base...HEAD commits and uncommitted worktree edits."""
    ctx_flag = f"-U{max(0, context_lines)}"
    try:
        committed = _run_git(root, "diff", "--no-color", ctx_flag, f"{base}...HEAD")
    except VcsError:
        committed = ""
    try:
        worktree = _run_git(root, "diff", "--no-color", ctx_flag, "HEAD")
    except VcsError:
        worktree = _run_git(root, "diff", "--no-color", ctx_flag)
    return committed + ("\n" if committed and worktree else "") + worktree


def changed_files(root: Path, base: str = "main") -> list[str]:
    names: list[str] = []
    try:
        names.extend(_run_git(root, "diff", "--name-only", f"{base}...HEAD").splitlines())
    except VcsError:
        pass
    try:
        names.extend(_run_git(root, "diff", "--name-only", "HEAD").splitlines())
    except VcsError:
        pass
    return sorted({n.strip().replace("\\", "/") for n in names if n.strip()})


def count_changed_lines(diff_text: str) -> int:
    count = 0
    for line in diff_text.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")):
            count += 1
    return count


def build_review_package(root: Path, base: str, output_file: Path) -> Path:
    """Create a self-contained review package file (commit log + stat + diff)."""
    output_file.parent.mkdir(parents=True, exist_ok=True)
    sections: list[str] = [f"# Review Package (base={base})"]
    if is_git_repo(root):
        try:
            log_out = _run_git(root, "log", "--oneline", f"{base}..HEAD")
            sections.append("## Commits\n```\n" + (log_out.strip() or "(uncommitted working tree)") + "\n```")
        except VcsError:
            pass
        diff_body = diff_against(root, base, context_lines=5)
    else:
        diff_body = ""
    sections.append("## Unified Diff\n```diff\n" + diff_body + "\n```")
    output_file.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    return output_file


def create_isolated_worktree(root: Path, loop_id: str, base: str = "HEAD") -> Path:
    """Create an isolated git worktree under ``.neoagent-devops/worktrees/<loop_id>``."""
    wt_dir = (root / ".neoagent-devops" / "worktrees" / loop_id).resolve()
    branch_name = f"neoagent/{loop_id.lower()}"
    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    _run_git(root, "worktree", "add", "-B", branch_name, str(wt_dir), base)
    return wt_dir


def remove_worktree(root: Path, worktree_path: Path) -> None:
    """Cleanly remove an isolated git worktree."""
    if worktree_path.exists():
        _run_git(root, "worktree", "remove", "--force", str(worktree_path))
