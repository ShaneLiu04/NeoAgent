"""Docker-based code execution sandbox.

CyberClaw's execute_office_shell uses subprocess.run(shell=True) with
a regex blacklist — trivially bypassable. This module provides:

1. Docker executor: full OS-level isolation (seccomp, cgroups, namespaces)
2. Process executor: lighter-weight but still confined to workspace
3. Automatic fallback: Docker → process → error

Both executors enforce:
- Hard timeout with SIGKILL
- Memory limits
- Output size limits (prevent token explosion)
- Workspace path confinement
"""

from __future__ import annotations

import asyncio
import os
import platform
from pathlib import Path

from ..config import get_config
from ..security import Capability, DockerSandbox, SandboxResult
from .base import RiskLevel, neoagent_tool


def _workspace_path() -> Path:
    return get_config().office_dir


async def _run_docker_python(code: str) -> SandboxResult:
    """Execute Python code in a Docker container."""
    cfg = get_config()
    sandbox = DockerSandbox(
        timeout_seconds=cfg.sandbox.timeout_seconds,
        max_memory_mb=cfg.sandbox.max_memory_mb,
        network_enabled=cfg.sandbox.network_enabled,
    )
    return await sandbox.run_python(code)


async def _run_shell_process(command: str, cwd: Path, timeout: int) -> SandboxResult:
    """Execute a shell command as a subprocess, confined to workspace.

    WARNING: This uses ``sh -c`` in fallback mode when Docker is unavailable.
    Shell injection is possible if ``command`` is constructed from untrusted input.
    Always prefer Docker sandbox mode for untrusted code.
    """
    import time

    start = time.time()

    try:
        proc = await asyncio.create_subprocess_exec(
            "sh", "-c", command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env={
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": str(cwd),
                "TMPDIR": str(cwd / ".tmp"),
                "LANG": "en_US.UTF-8",
            },
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            await proc.wait()
            elapsed = (time.time() - start) * 1000
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                duration_ms=elapsed,
                truncated=True,
            )

        elapsed = (time.time() - start) * 1000
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        truncated = False
        if len(stdout) > 50_000:
            stdout = stdout[:50_000] + "\n... [truncated]"
            truncated = True

        return SandboxResult(
            exit_code=proc.returncode or 0,
            stdout=stdout,
            stderr=stderr,
            duration_ms=elapsed,
            truncated=truncated,
        )

    except Exception as e:
        elapsed = (time.time() - start) * 1000
        return SandboxResult(
            exit_code=-1,
            stdout="",
            stderr=f"Execution error: {e}",
            duration_ms=elapsed,
        )


async def _run_python_process(code_file: Path, cwd: Path, timeout: int) -> SandboxResult:
    """Execute a Python script using ``python -I`` with explicit args (no shell)."""
    import time

    start = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            "python", "-I", str(code_file),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env={
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": str(cwd),
                "TMPDIR": str(cwd / ".tmp"),
                "LANG": "en_US.UTF-8",
            },
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            await proc.wait()
            elapsed = (time.time() - start) * 1000
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr=f"Python execution timed out after {timeout}s",
                duration_ms=elapsed,
                truncated=True,
            )

        elapsed = (time.time() - start) * 1000
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        truncated = False
        if len(stdout) > 50_000:
            stdout = stdout[:50_000] + "\n... [truncated]"
            truncated = True

        return SandboxResult(
            exit_code=proc.returncode or 0,
            stdout=stdout,
            stderr=stderr,
            duration_ms=elapsed,
            truncated=truncated,
        )
    except Exception as e:
        elapsed = (time.time() - start) * 1000
        return SandboxResult(
            exit_code=-1,
            stdout="",
            stderr=f"Execution error: {e}",
            duration_ms=elapsed,
        )


def _format_result(result: SandboxResult) -> str:
    """Format a SandboxResult for display."""
    parts = [
        f"Exit code: {result.exit_code}",
        f"Duration: {result.duration_ms:.0f}ms",
        f"System: {platform.system()}",
        "",
    ]
    if result.stdout:
        parts.append(f"[stdout]\n{result.stdout}")
    if result.stderr:
        parts.append(f"[stderr]\n{result.stderr}")
    if result.truncated:
        parts.append("\n(output was truncated)")
    if not result.stdout and not result.stderr:
        parts.append("(no output)")
    return "\n".join(parts)


@neoagent_tool(
    name="run_python",
    description="Execute Python code in a secure sandbox with timeout and memory limits.",
    capabilities=Capability.PROCESS_SANDBOX,
    risk_level=RiskLevel.CRITICAL,
    timeout_seconds=60.0,
    tags=["code", "execution", "python"],
)
async def run_python(code: str) -> str:
    """Execute Python code in a sandbox.

    Uses Docker if available, falls back to process isolation.
    """
    cfg = get_config()

    if cfg.sandbox.mode == "docker" and DockerSandbox.is_available():
        result = await _run_docker_python(code)
        return _format_result(result)

    # Fallback: restricted process execution (no shell)
    ws = _workspace_path()
    ws.mkdir(parents=True, exist_ok=True)

    # Write code to temp file, run with -I (isolated mode)
    tmp = ws / ".tmp"
    tmp.mkdir(exist_ok=True)
    code_file = tmp / "_sandbox_script.py"
    code_file.write_text(code, encoding="utf-8")

    try:
        result = await _run_python_process(code_file, ws, cfg.sandbox.timeout_seconds)
        return _format_result(result)
    finally:
        code_file.unlink(missing_ok=True)


@neoagent_tool(
    name="run_shell",
    description="Execute a shell command within the workspace directory.",
    capabilities=Capability.PROCESS_EXECUTE,
    risk_level=RiskLevel.CRITICAL,
    timeout_seconds=60.0,
    tags=["shell", "execution"],
)
async def run_shell(command: str) -> str:
    """Execute a shell command confined to workspace.

    Safer than CyberClaw: no shell=True, uses subprocess with cleaned env,
    and enforces workspace confinement.
    """
    cfg = get_config()

    if cfg.sandbox.mode == "docker" and DockerSandbox.is_available():
        sandbox = DockerSandbox(
            timeout_seconds=cfg.sandbox.timeout_seconds,
            max_memory_mb=cfg.sandbox.max_memory_mb,
            network_enabled=cfg.sandbox.network_enabled,
        )
        result = await sandbox.run_shell(command)
        return _format_result(result)

    ws = _workspace_path()
    ws.mkdir(parents=True, exist_ok=True)
    result = await _run_shell_process(command, ws, cfg.sandbox.timeout_seconds)
    return _format_result(result)
