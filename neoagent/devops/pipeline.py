"""Platform-Agnostic CI Pipeline Runner & 3-Way Failure Root-Cause Triager.

Integrates ``api-test-debug`` discipline:
- Distinguishes ``CODE_DEFECT`` (auto-fixable) vs ``ENV_BLOCKER`` (missing binary,
  connection refused, timeout — stop & escalate!) vs ``SPEC_ASSERTION_MISMATCH``
  (business expectation mismatch — fix implementation, NEVER weaken assertions!).
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import PipelinePolicyConfig, PipelineStepConfig
from .models import FailureKind

TAIL_CHARS = 4_000

_SUBCLASS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("timeout", re.compile(r"(?i)timed out|timeout expired")),
    (
        "env_unavailable",
        re.compile(
            r"(?i)connection refused|could not connect|name or service not known|"
            r"cannot connect to the docker daemon|permission denied.*socket|no space left on device"
        ),
    ),
    (
        "compile_or_syntax",
        re.compile(
            r"(?i)SyntaxError|IndentationError|compilation (failed|error)|cannot find symbol|"
            r"error TS\d+|undefined reference"
        ),
    ),
    (
        "dependency_or_import",
        re.compile(
            r"(?i)ModuleNotFoundError|ImportError|No matching distribution|Could not resolve|npm ERR!"
        ),
    ),
    ("lint_or_type", re.compile(r"(?i)\bruff\b|flake8|eslint|mypy|error:\s+Incompatible types")),
    ("security_scan", re.compile(r"(?i)vulnerab|CVE-\d{4}-\d+|secret detected|bandit")),
    ("assertion_failure", re.compile(r"(?i)AssertionError|assert .+ == .+")),
    ("test_runtime_error", re.compile(r"(?i)\bFAILED\b|TypeError:|AttributeError:|KeyError:|NameError:|ValueError:")),
]


def classify_step_failure(
    output: str, exit_code: int = 1, timed_out: bool = False
) -> tuple[str, FailureKind]:
    """Classify step failure into ``(failure_subclass, FailureKind)``."""
    if timed_out:
        return "timeout", FailureKind.ENV_BLOCKER
    if exit_code == 127:
        return "missing_binary", FailureKind.ENV_BLOCKER

    for subclass, pat in _SUBCLASS_PATTERNS:
        if pat.search(output):
            if subclass in ("timeout", "env_unavailable"):
                return subclass, FailureKind.ENV_BLOCKER
            if subclass == "assertion_failure":
                return subclass, FailureKind.SPEC_ASSERTION_MISMATCH
            return subclass, FailureKind.CODE_DEFECT
    return "unknown", FailureKind.CODE_DEFECT


StepRunner = Callable[[PipelineStepConfig, Path], tuple[int, str, bool]]


def default_step_runner(step: PipelineStepConfig, cwd: Path) -> tuple[int, str, bool]:
    """Execute a step safely. List commands bypass the shell entirely."""
    use_shell = isinstance(step.cmd, str)
    try:
        res = subprocess.run(
            step.cmd,
            cwd=cwd,
            shell=use_shell,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=step.timeout,
        )
        return res.returncode, (res.stdout or "") + (res.stderr or ""), False
    except subprocess.TimeoutExpired as exc:
        raw_out = exc.stdout or b""
        text_out = raw_out.decode("utf-8", "replace") if isinstance(raw_out, bytes) else raw_out
        return 124, text_out or "", True
    except OSError as exc:
        return 127, f"Command execution failed (OSError): {exc}", False


def execute_pipeline(
    cfg: PipelinePolicyConfig,
    cwd: Path,
    log_dir: Path | None = None,
    runner: StepRunner = default_step_runner,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    steps_out: list[dict[str, Any]] = []
    coverage: float | None = None
    dominant_failure_kind: str | None = None

    for step in cfg.steps:
        max_attempts = 1 + max(0, step.retries)
        flaky = False
        code, out, timed_out, dur = 1, "", False, 0.0
        attempt = 0

        for attempt in range(1, max_attempts + 1):
            t0 = clock()
            code, out, timed_out = runner(step, cwd)
            dur = clock() - t0
            if log_dir is not None:
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / f"{step.name}.attempt{attempt}.log").write_text(out, encoding="utf-8")
            if code == 0:
                flaky = attempt > 1
                break

        status = "passed" if code == 0 else "failed"
        entry: dict[str, Any] = {
            "name": step.name,
            "required": step.required,
            "status": status,
            "exit_code": code,
            "duration_s": round(dur, 3),
            "attempts": attempt,
            "flaky": flaky,
        }
        if code != 0:
            subclass, f_kind = classify_step_failure(out, code, timed_out)
            entry["failure_class"] = subclass
            entry["failure_kind"] = f_kind.value
            entry["tail"] = out[-TAIL_CHARS:]
            if step.required and dominant_failure_kind is None:
                dominant_failure_kind = f_kind.value

        if step.coverage_regex:
            m = re.search(step.coverage_regex, out)
            if m:
                try:
                    coverage = float(m.group(1))
                    entry["coverage"] = coverage
                except (IndexError, ValueError):
                    pass

        steps_out.append(entry)
        if code != 0 and step.required:
            break

    skipped = [s.name for s in cfg.steps[len(steps_out) :]]
    return {
        "steps": steps_out,
        "skipped": skipped,
        "coverage": coverage,
        "failure_kind": dominant_failure_kind,
    }


def render_failure_digest(report: dict[str, Any]) -> str:
    """Format actionable failure brief for the Auto-Fixer sub-agent."""
    lines: list[str] = []
    for s in report.get("steps", []):
        if s.get("status") != "passed":
            lines.append(
                f"## Failed Step: {s['name']} "
                f"[class={s.get('failure_class', 'unknown')}, kind={s.get('failure_kind', 'code_defect')}] "
                f"(exit={s.get('exit_code')})"
            )
            lines.append("```text\n" + s.get("tail", "").strip() + "\n```")
    return "\n\n".join(lines)
