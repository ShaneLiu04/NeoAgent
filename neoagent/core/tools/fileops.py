"""Safe file operations within the workspace sandbox.

CyberClaw's sandbox_tools.py uses path traversal regex (bypassable).
This module enforces workspace confinement via realpath comparison,
which is immune to symlink, encoding, and environment variable tricks.

Additional improvements:
- Atomic writes (write to temp file, then rename)
- Automatic backup on overwrite (.bak)
- Configurable max file size for reads
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..config import get_config
from ..security import Capability
from .base import RiskLevel, neoagent_tool


def _resolve_safe(relative_path: str, must_exist: bool = False) -> Path:
    """Resolve a relative path and enforce workspace confinement.

    Uses realpath() to resolve symlinks before checking confinement —
    immune to the symlink-based bypasses that defeat prefix-checking.
    """
    cfg = get_config()
    base = cfg.office_dir.resolve()

    # Clean the path: strip leading slashes, normalize
    cleaned = relative_path.lstrip("/").lstrip("\\").lstrip(".")
    while cleaned.startswith("/") or cleaned.startswith("\\"):
        cleaned = cleaned[1:]

    target = (base / cleaned).resolve()

    # Confinement check using resolved (dereferenced) paths.
    # Uses Path.is_relative_to() rather than str.startswith() to prevent
    # sibling-prefix bypass (CWE-22: /workspace/office_evil vs /workspace/office).
    if not target.is_relative_to(base):
        raise PermissionError(
            f"Access denied: '{relative_path}' resolves outside workspace.\n"
            f"Target: {target}\n"
            f"Workspace: {base}"
        )

    if must_exist and not target.exists():
        raise FileNotFoundError(f"File not found: {relative_path}")

    return target


@neoagent_tool(
    name="list_files",
    description="List files and folders in the workspace. Optionally specify a subdirectory.",
    capabilities=Capability.FILESYSTEM_READ,
    risk_level=RiskLevel.LOW,
    timeout_seconds=10.0,
)
async def list_files(sub_dir: str = "") -> str:
    target = _resolve_safe(sub_dir) if sub_dir else get_config().office_dir
    if not target.exists():
        return f"Directory not found: {sub_dir or 'workspace root'}"

    items = sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    if not items:
        return f"'{sub_dir or 'workspace root'}' is empty."

    lines = []
    for item in items:
        icon = "dir" if item.is_dir() else "file"
        size = ""
        if item.is_file():
            try:
                size = f" ({item.stat().st_size:,} bytes)"
            except Exception:
                pass
        lines.append(f"  [{icon}] {item.name}{size}")
    return "\n".join(lines)


@neoagent_tool(
    name="read_file",
    description="Read the contents of a file in the workspace.",
    capabilities=Capability.FILESYSTEM_READ,
    risk_level=RiskLevel.LOW,
    timeout_seconds=10.0,
)
async def read_file(filepath: str) -> str:
    target = _resolve_safe(filepath, must_exist=True)

    # Configurable max read size
    max_size = 100_000  # 100KB
    file_size = target.stat().st_size
    if file_size > max_size:
        # Read only the first max_size bytes
        with open(target, encoding="utf-8", errors="replace") as f:
            content = f.read(max_size)
        return (
            f"Warning: file is {file_size:,} bytes. Showing first {max_size:,}.\n\n"
            f"{content}\n\n... [truncated — {file_size - max_size:,} bytes remaining]"
        )

    with open(target, encoding="utf-8", errors="replace") as f:
        content = f.read()
    return content if content else "(empty file)"


@neoagent_tool(
    name="write_file",
    description="Write content to a file in the workspace. Creates parent directories if needed.",
    capabilities=Capability.FILESYSTEM_WRITE,
    risk_level=RiskLevel.MEDIUM,
    timeout_seconds=15.0,
)
async def write_file(filepath: str, content: str, mode: str = "w") -> str:
    if mode not in ("w", "a"):
        return "Error: mode must be 'w' (overwrite) or 'a' (append)."

    target = _resolve_safe(filepath)

    # Create parent directories
    target.parent.mkdir(parents=True, exist_ok=True)

    if mode == "w":
        existed_before = target.exists()
        # Create .bak backup if file exists
        if existed_before:
            backup = target.with_suffix(target.suffix + ".bak")
            shutil.copy2(target, backup)

        # Atomic write: write to temp file, then rename
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(target)
        action = "Overwritten (backup saved to .bak)" if existed_before else "Created"
    else:
        # Append mode
        with open(target, "a", encoding="utf-8") as f:
            if not content.startswith("\n"):
                f.write("\n")
            f.write(content)
        action = "Appended"

    return f"{action}: {filepath} ({len(content):,} chars)"


@neoagent_tool(
    name="delete_file",
    description="Delete a file or empty directory in the workspace.",
    capabilities=Capability.FILESYSTEM_DELETE,
    risk_level=RiskLevel.HIGH,
    timeout_seconds=10.0,
)
async def delete_file(filepath: str) -> str:
    target = _resolve_safe(filepath, must_exist=True)
    if target.is_dir():
        target.rmdir()
        return f"Deleted directory: {filepath}"
    else:
        target.unlink()
        return f"Deleted file: {filepath}"
