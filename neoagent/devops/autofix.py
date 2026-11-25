"""Closed-Loop AI Coder & Auto-Fixer with Generator-Evaluator Isolation.

Enforces ``subagent-driven-development``, ``devflow-tdd``, and ``api-test-debug``:
1. Fresh context per coding/fixing round — never shares memory with Reviewer.
2. File handoff protocol: reads bounded ``task-brief.md`` / ``fix-brief.md``
   instead of stuffing 50k chars of conversation history into prompts.
3. Environment blockers (``FailureKind.ENV_BLOCKER``) immediately abort auto-fixing
   and escalate to humans instead of thrashing source files.
4. Oscillation detection: if any finding is reopened >= 2 times, halts and escalates.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .models import LoopRecord


@dataclass
class CodingOutcome:
    success: bool
    summary: str
    modified_files: list[str] = field(default_factory=list)
    diff_override: str | None = None  # Optional in-memory diff for dry-run / testing
    cost_usd: float = 0.0
    error: str = ""


class CodingBackend(Protocol):
    """Pluggable AI coding / bug-repair execution backend."""

    def implement_feature(
        self,
        loop: LoopRecord,
        task_brief_path: Path,
        workspace_root: Path,
        skill_context: str = "",
    ) -> CodingOutcome: ...

    def repair_from_brief(
        self,
        loop: LoopRecord,
        fix_brief_path: Path,
        workspace_root: Path,
        round_no: int,
        skill_context: str = "",
    ) -> CodingOutcome: ...


class CallableCodingBackend:
    """Deterministic / custom-hook backend for CI automation and closed-loop testing."""

    def __init__(
        self,
        implement_fn: Callable[[LoopRecord, Path, Path], CodingOutcome] | None = None,
        repair_fn: Callable[[LoopRecord, Path, Path, int], CodingOutcome] | None = None,
    ) -> None:
        self._implement_fn = implement_fn
        self._repair_fn = repair_fn

    def implement_feature(
        self,
        loop: LoopRecord,
        task_brief_path: Path,
        workspace_root: Path,
        skill_context: str = "",
    ) -> CodingOutcome:
        if self._implement_fn is None:
            return CodingOutcome(True, "No-op implement hook")
        return self._implement_fn(loop, task_brief_path, workspace_root)

    def repair_from_brief(
        self,
        loop: LoopRecord,
        fix_brief_path: Path,
        workspace_root: Path,
        round_no: int,
        skill_context: str = "",
    ) -> CodingOutcome:
        if self._repair_fn is None:
            return CodingOutcome(False, "No repair hook configured", error="missing_repair_fn")
        return self._repair_fn(loop, fix_brief_path, workspace_root, round_no)


class NeoAgentCodingBackend:
    """Production AI Coder/Fixer powered by isolated NeoAgent sessions + Skill bundles."""

    def __init__(self, provider: str | None = None, model: str | None = None) -> None:
        self.provider = provider
        self.model = model

    def _run_isolated_agent(self, session_id: str, system_prompt: str, user_prompt: str) -> CodingOutcome:
        from ..core.agent import NeoAgent
        from ..core.config import get_config

        cfg = get_config()
        prov = self.provider or cfg.llm.default_provider
        if not cfg.api_key_for(prov):
            return CodingOutcome(
                success=False,
                summary="",
                error=f"Missing API key for provider '{prov}'",
            )

        agent = NeoAgent(
            provider=prov,
            model=self.model or cfg.llm.powerful_model,
            system_prompt=system_prompt,
            session_id=session_id,
            router_enabled=False,
        )

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        try:
            if running and running.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    out = pool.submit(lambda: asyncio.run(agent.invoke(user_prompt))).result()
            else:
                out = asyncio.run(agent.invoke(user_prompt))
        except Exception as exc:
            return CodingOutcome(False, "", error=str(exc))

        cost = agent._budget.total_cost if agent._budget else 0.0
        return CodingOutcome(success=not out.startswith("Error:"), summary=out[:1000], cost_usd=cost)

    def implement_feature(
        self,
        loop: LoopRecord,
        task_brief_path: Path,
        workspace_root: Path,
        skill_context: str = "",
    ) -> CodingOutcome:
        brief = task_brief_path.read_text(encoding="utf-8")
        sys_prompt = (
            "You are NeoAgent TDD Implementer operating in an isolated session.\n"
            "STRICT RULES:\n"
            "1. Follow RED -> GREEN -> REFACTOR: write failing tests first, then minimal clean code.\n"
            "2. Never hardcode secrets, never use eval/exec/shell=True, never swallow exceptions.\n"
            f"3. Workspace root: {workspace_root}\n\n"
            f"{skill_context}"
        )
        return self._run_isolated_agent(
            session_id=f"coder_{loop.id}",
            system_prompt=sys_prompt,
            user_prompt=f"Implement the following task brief:\n\n{brief}",
        )

    def repair_from_brief(
        self,
        loop: LoopRecord,
        fix_brief_path: Path,
        workspace_root: Path,
        round_no: int,
        skill_context: str = "",
    ) -> CodingOutcome:
        brief = fix_brief_path.read_text(encoding="utf-8")
        sys_prompt = (
            "You are NeoAgent Surgical Auto-Fixer operating in an isolated session.\n"
            "STRICT ANTI-CHEAT RED LINES:\n"
            "1. NEVER delete assertions, NEVER add @pytest.mark.skip/xfail/# noqa, NEVER lower coverage thresholds.\n"
            "2. Fix ONLY the root causes listed in the fix brief; do not refactor unrelated files.\n"
            "3. For every bug fixed, ensure a regression test covers it.\n"
            f"4. Workspace root: {workspace_root}\n\n"
            f"{skill_context}"
        )
        return self._run_isolated_agent(
            session_id=f"fixer_{loop.id}_r{round_no}",
            system_prompt=sys_prompt,
            user_prompt=f"Repair all issues in this fix brief (Round {round_no}):\n\n{brief}",
        )


def detect_oscillation(loop: LoopRecord, max_reopens: int = 2) -> list[str]:
    """Return IDs of findings oscillating (marked fixed then reopened repeatedly)."""
    return [f.id for f in loop.open_findings() if f.reopened_count >= max_reopens]
