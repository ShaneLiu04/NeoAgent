"""True sandbox, capability-based permissions, and safe execution.

Fixes CyberClaw's critical security flaws:
1. Docker-based isolation (NOT regex path blocking — trivial to bypass)
2. Capability system: tools declare required permissions, agent must request
3. AST-based safe math evaluator (NOT Python eval())
4. SecretManager with encryption-at-rest support

CyberClaw's sandbox_tools.py uses regex patterns to block "../" —
bypassable via symlinks, encoding tricks, env vars, indirect execution.
This module provides real OS-level isolation.
"""

from __future__ import annotations

import ast
import asyncio
import enum
import math as _math
import operator as _op
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── Capability System ──────────────────────────────


class Capability(enum.Flag):
    """Granular permissions that tools must declare.

    Each tool declares which capabilities it needs. The agent starts with
    a minimal set and must request escalation for restricted operations.
    This is capability-based security — not CyberClaw's regex blacklist.
    """

    NONE = 0
    FILESYSTEM_READ = 1 << 0      # Read files within workspace
    FILESYSTEM_WRITE = 1 << 1     # Write/create files within workspace
    FILESYSTEM_DELETE = 1 << 2    # Delete files
    NETWORK_OUTBOUND = 1 << 3     # Make HTTP requests
    PROCESS_EXECUTE = 1 << 4      # Run subprocesses
    PROCESS_SANDBOX = 1 << 5      # Run in Docker sandbox (elevated)
    USER_DATA_READ = 1 << 6       # Access user profile/memory
    USER_DATA_WRITE = 1 << 7      # Modify user profile/memory
    SYSTEM_SCHEDULE = 1 << 8      # Create scheduled tasks
    SYSTEM_MONITOR = 1 << 9       # Access system info

    # Convenience groups
    READ_ONLY = FILESYSTEM_READ | NETWORK_OUTBOUND | USER_DATA_READ
    FULL_SANDBOX = READ_ONLY | FILESYSTEM_WRITE | PROCESS_SANDBOX
    ALL = (
        FILESYSTEM_READ | FILESYSTEM_WRITE | FILESYSTEM_DELETE
        | NETWORK_OUTBOUND | PROCESS_EXECUTE | PROCESS_SANDBOX
        | USER_DATA_READ | USER_DATA_WRITE
        | SYSTEM_SCHEDULE | SYSTEM_MONITOR
    )


@dataclass
class CapabilityToken:
    """Holds the currently granted capabilities for a session."""
    granted: Capability = Capability.READ_ONLY
    escalation_callback: Callable[[Capability], bool] | None = None

    def has(self, required: Capability) -> bool:
        return (self.granted & required) == required

    def require(self, required: Capability) -> None:
        if not self.has(required):
            missing = required & ~self.granted
            raise PermissionError(
                f"Insufficient capabilities. Missing: {missing!r}. "
                f"Granted: {self.granted!r}"
            )

    async def request_escalation(self, needed: Capability, reason: str) -> bool:
        """Request additional capabilities — calls the escalation callback if set."""
        if self.escalation_callback:
            return self.escalation_callback(needed)
        # Auto-deny if no callback configured (safe default)
        return False


# ── Safe Math Evaluator ────────────────────────────


class SafeEvaluator:
    """AST-based safe math expression evaluator.

    CyberClaw uses eval() in its calculator tool — a code injection vector.
    This evaluator only allows literal values, arithmetic operators, and
    a small whitelist of math functions. Everything else raises an error.
    """

    SAFE_NODES: set[type[ast.AST]] = {
        ast.Expression, ast.Constant, ast.BinOp, ast.UnaryOp,
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
        ast.USub, ast.UAdd, ast.Call, ast.Name, ast.Load,
        ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
        ast.BoolOp, ast.And, ast.Or,
        ast.Subscript, ast.Slice, ast.List, ast.Tuple, ast.Dict,
    }

    SAFE_FUNCTIONS: dict[str, Callable] = {
        "abs": abs, "round": round, "min": min, "max": max,
        "int": int, "float": float, "str": str, "bool": bool, "len": len,
    }

    SAFE_MATH: dict[str, Callable] = {
        "sqrt": _math.sqrt, "pow": _math.pow, "log": _math.log,
        "log10": _math.log10, "log2": _math.log2,
        "sin": _math.sin, "cos": _math.cos, "tan": _math.tan,
        "pi": _math.pi, "e": _math.e,
        "ceil": _math.ceil, "floor": _math.floor,
        "degrees": _math.degrees, "radians": _math.radians,
    }

    def __init__(self) -> None:
        self._allowed_names: dict[str, Any] = {}
        self._allowed_names.update(self.SAFE_FUNCTIONS)
        self._allowed_names.update(self.SAFE_MATH)

    def evaluate(self, expression: str) -> Any:
        """Safely evaluate a math expression. Raises on any unsafe construct."""
        try:
            tree = ast.parse(expression.strip(), mode="eval")
        except SyntaxError as e:
            raise ValueError(f"Invalid expression syntax: {e}") from e

        self._validate_node(tree)
        return self._exec_node(tree.body)

    def _validate_node(self, node: ast.AST) -> None:
        """Recursively validate that the AST only contains safe nodes."""
        if type(node) not in self.SAFE_NODES:
            raise ValueError(f"Forbidden construct: {type(node).__name__}")

        for child in ast.iter_child_nodes(node):
            self._validate_node(child)

    def _exec_node(self, node: ast.AST) -> Any:
        match node:
            case ast.Constant(value=value):
                return value
            case ast.BinOp(left=left, op=op, right=right):
                left_val = self._exec_node(left)
                right_val = self._exec_node(right)
                op_map = {
                    ast.Add: _op.add, ast.Sub: _op.sub, ast.Mult: _op.mul,
                    ast.Div: _op.truediv, ast.FloorDiv: _op.floordiv,
                    ast.Mod: _op.mod, ast.Pow: _op.pow,
                }
                return op_map[type(op)](left_val, right_val)
            case ast.UnaryOp(op=op, operand=operand):
                val = self._exec_node(operand)
                op_map = {ast.USub: _op.neg, ast.UAdd: _op.pos}
                return op_map[type(op)](val)
            case ast.Call(func=func, args=args):
                if isinstance(func, ast.Name) and func.id in self._allowed_names:
                    fn = self._allowed_names[func.id]
                    arg_vals = [self._exec_node(a) for a in args]
                    return fn(*arg_vals)
                raise ValueError(f"Function '{getattr(func, 'id', '?')}' not allowed")
            case ast.Name(id=name):
                if name in self._allowed_names:
                    return self._allowed_names[name]
                raise ValueError(f"Variable '{name}' not defined")
            case ast.Compare(left=left, ops=ops, comparators=comparators):
                left_val = self._exec_node(left)
                cmp_vals = [self._exec_node(c) for c in comparators]
                cmp_ops = {
                    ast.Eq: _op.eq, ast.NotEq: _op.ne,
                    ast.Lt: _op.lt, ast.LtE: _op.le,
                    ast.Gt: _op.gt, ast.GtE: _op.ge,
                }
                result = True
                for op, right_val in zip(ops, cmp_vals, strict=False):
                    result = result and cmp_ops[type(op)](left_val, right_val)
                    left_val = right_val
                return result
            case ast.BoolOp(op=op, values=values):
                vals = [self._exec_node(v) for v in values]
                if isinstance(op, ast.And):
                    return all(vals)
                if isinstance(op, ast.Or):
                    return any(vals)
            case ast.List(elts=elts):
                return [self._exec_node(e) for e in elts]
            case ast.Tuple(elts=elts):
                return tuple(self._exec_node(e) for e in elts)
            case ast.Dict(keys=keys, values=values):
                return {
                    self._exec_node(k): self._exec_node(v)
                    for k, v in zip(keys, values, strict=False)
                }
            case ast.Subscript(value=value, slice=slice_node):
                container = self._exec_node(value)
                if isinstance(slice_node, ast.Slice):
                    lower = self._exec_node(slice_node.lower) if slice_node.lower else None
                    upper = self._exec_node(slice_node.upper) if slice_node.upper else None
                    step = self._exec_node(slice_node.step) if slice_node.step else None
                    return container[slice(lower, upper, step)]
                key = self._exec_node(slice_node)
                return container[key]
            case _:
                raise ValueError(f"Unexpected AST node: {type(node).__name__}")


# ── Docker Sandbox ──────────────────────────────────


@dataclass
class SandboxResult:
    """Result from a sandboxed execution."""
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    truncated: bool = False


class DockerSandbox:
    """Execute code in ephemeral Docker containers with seccomp profiles.

    Unlike CyberClaw's directory-restriction "sandbox" (bypassable via
    symlinks, env vars, etc.), this provides real OS-level isolation:

    - Ephemeral container: destroyed after execution, no state leakage
    - Memory limit: prevents fork bombs and memory exhaustion
    - CPU limit: prevents mining or infinite loops
    - Network isolation: no outbound access by default
    - Read-only rootfs: container filesystem cannot be modified
    - Timeout: hard kill after timeout_seconds
    - Volume: only workspace subdirectory is mounted (writeable area)

    Requires Docker to be installed and running.
    """

    DOCKER_IMAGE = "python:3.11-slim"  # Lightweight, reproducible

    def __init__(
        self,
        timeout_seconds: int = 30,
        max_memory_mb: int = 256,
        network_enabled: bool = False,
        workspace_mount: Path | None = None,
    ) -> None:
        self.timeout = timeout_seconds
        self.max_memory = max_memory_mb
        self.network_enabled = network_enabled
        self.workspace_mount = workspace_mount

    async def run_python(self, code: str, stdin: str = "") -> SandboxResult:
        """Execute Python code in a Docker sandbox with all restrictions."""
        # Write code to temp file for volume mounting
        work_dir = tempfile.mkdtemp(prefix="neoagent_sandbox_")
        code_file = Path(work_dir) / "script.py"

        try:
            code_file.write_text(code, encoding="utf-8")

            cmd = [
                "docker", "run", "--rm",
                "--memory", f"{self.max_memory}m",
                "--memory-swap", f"{self.max_memory}m",
                "--cpus", "1",
                "--network", "none" if not self.network_enabled else "bridge",
                "--read-only",
                "--tmpfs", "/tmp:exec,size=64m",
                "--security-opt", "no-new-privileges:true",
                "--cap-drop", "ALL",
                "--pids-limit", "50",
                "-v", f"{work_dir}:/code:ro",
                "-w", "/code",
                self.DOCKER_IMAGE,
                "timeout", str(self.timeout),
                "python", "-I", "script.py",
            ]

            start = asyncio.get_event_loop().time()
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self.timeout + 5,  # slightly longer than container timeout
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return SandboxResult(
                    exit_code=-1,
                    stdout="",
                    stderr="Execution timed out — sandbox killed",
                    duration_ms=(asyncio.get_event_loop().time() - start) * 1000,
                    truncated=True,
                )

            elapsed = (asyncio.get_event_loop().time() - start) * 1000
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            # Truncate large outputs
            truncated = False
            if len(stdout) > 50_000:
                stdout = stdout[:50_000] + "\n... [truncated]"
                truncated = True
            if len(stderr) > 20_000:
                stderr = stderr[:20_000] + "\n... [truncated]"
                truncated = True

            return SandboxResult(
                exit_code=proc.returncode or 0,
                stdout=stdout,
                stderr=stderr,
                duration_ms=elapsed,
                truncated=truncated,
            )

        finally:
            # Cleanup temp directory
            import shutil
            shutil.rmtree(work_dir, ignore_errors=True)

    async def run_shell(self, command: str) -> SandboxResult:
        """Execute a shell command in a Docker sandbox."""
        work_dir = tempfile.mkdtemp(prefix="neoagent_sandbox_")
        try:
            cmd = [
                "docker", "run", "--rm",
                "--memory", f"{self.max_memory}m",
                "--memory-swap", f"{self.max_memory}m",
                "--cpus", "1",
                "--network", "none" if not self.network_enabled else "bridge",
                "--read-only",
                "--tmpfs", "/tmp:exec,size=64m",
                "--security-opt", "no-new-privileges:true",
                "--cap-drop", "ALL",
                "--pids-limit", "50",
                "-v", f"{work_dir}:/work:rw",
                "-w", "/work",
                self.DOCKER_IMAGE,
                "timeout", str(self.timeout),
                "sh", "-c", command,
            ]

            start = asyncio.get_event_loop().time()
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self.timeout + 5,
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return SandboxResult(
                    exit_code=-1, stdout="", stderr="Timeout — sandbox killed",
                    duration_ms=(asyncio.get_event_loop().time() - start) * 1000,
                    truncated=True,
                )

            elapsed = (asyncio.get_event_loop().time() - start) * 1000
            truncated = False
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            if len(stdout) > 50_000:
                stdout = stdout[:50_000] + "\n... [truncated]"
                truncated = True

            return SandboxResult(
                exit_code=proc.returncode or 0,
                stdout=stdout, stderr=stderr,
                duration_ms=elapsed, truncated=truncated,
            )
        finally:
            import shutil
            shutil.rmtree(work_dir, ignore_errors=True)

    @staticmethod
    def is_available() -> bool:
        """Check if Docker is installed and the daemon is running."""
        try:
            import subprocess
            result = subprocess.run(
                ["docker", "info"], capture_output=True, timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False


# ── Secret Manager ──────────────────────────────────


class SecretManager:
    """API key management with optional OS keyring integration.

    CyberClaw stores keys in plaintext .env — readable by any process
    with filesystem access. This provides a path to encrypted storage.
    """

    def __init__(self, use_keyring: bool = False) -> None:
        self.use_keyring = use_keyring

    def get(self, service: str, username: str = "neoagent") -> str | None:
        """Retrieve a secret. Tries keyring first, then env."""
        if self.use_keyring:
            try:
                import keyring
                value = keyring.get_password(service, username)
                if value:
                    return value
            except Exception:
                pass
        return os.getenv(f"{service.upper()}_API_KEY")

    def set(self, service: str, username: str, value: str) -> None:
        """Store a secret securely."""
        if self.use_keyring:
            try:
                import keyring
                keyring.set_password(service, username, value)
                return
            except Exception:
                pass
        # Fallback: warn, don't write to .env (that would be less secure)
        import warnings
        warnings.warn(
            f"Keyring unavailable — {service} key NOT persisted. Set env var manually.",
            stacklevel=2,
        )


# ── Singleton accessors ────────────────────────────

_safe_eval: SafeEvaluator | None = None


def get_safe_evaluator() -> SafeEvaluator:
    global _safe_eval
    if _safe_eval is None:
        _safe_eval = SafeEvaluator()
    return _safe_eval
