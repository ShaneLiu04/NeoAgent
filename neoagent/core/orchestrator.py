"""Multi-agent orchestration for parallel task execution.

CyberClaw is strictly single-agent — one task at a time. This module
enables:
1. Sub-agent spawning for parallel independent tasks
2. Task delegation with result aggregation
3. Agent-to-agent message passing
4. Hierarchical agent teams (manager → workers pattern)

Use cases:
- "Research X, Y, and Z simultaneously" → 3 parallel research sub-agents
- "Refactor these 5 files" → 5 parallel code-editing sub-agents
- "Compare approaches A vs B" → 2 analysis sub-agents + 1 summarizer
"""

from __future__ import annotations

import asyncio
import dataclasses
import sys
from dataclasses import dataclass
from enum import Enum

from .agent import NeoAgent
from .config import get_config


class SubAgentRole(Enum):
    RESEARCHER = "researcher"       # Web search, information gathering
    CODER = "coder"                 # Code generation and editing
    ANALYZER = "analyzer"           # Data analysis, reasoning
    SUMMARIZER = "summarizer"       # Text summarization
    REVIEWER = "reviewer"           # Code review, quality check
    CUSTOM = "custom"               # User-defined role


ROLE_PROMPTS: dict[SubAgentRole, str] = {
    SubAgentRole.RESEARCHER: (
        "You are a research specialist. Your job is to find accurate, "
        "up-to-date information. Use web_search liberally. Cite sources. "
        "Be thorough but concise."
    ),
    SubAgentRole.CODER: (
        "You are a code specialist. Write correct, secure, well-structured "
        "code. Use run_python to verify your solutions. Follow best practices "
        "for the language and framework in use."
    ),
    SubAgentRole.ANALYZER: (
        "You are an analysis specialist. Break down complex problems "
        "systematically. Consider edge cases, trade-offs, and alternatives. "
        "Use tools to verify assumptions when possible."
    ),
    SubAgentRole.SUMMARIZER: (
        "You are a summarization specialist. Condense information into "
        "clear, concise summaries. Preserve key facts, decisions, and "
        "action items. Remove redundancy."
    ),
    SubAgentRole.REVIEWER: (
        "You are a code review specialist. Examine code for bugs, security "
        "issues, performance problems, and style violations. Be constructive "
        "and specific in your feedback."
    ),
}


@dataclass
class SubAgentTask:
    """A task to be executed by a sub-agent."""
    id: str
    prompt: str
    role: SubAgentRole = SubAgentRole.CUSTOM
    role_prompt: str | None = None
    model: str | None = None  # Override model for this sub-agent


@dataclass
class SubAgentResult:
    """Result from a completed sub-agent task."""
    task_id: str
    role: SubAgentRole
    output: str
    success: bool
    error: str | None = None
    tool_calls: int = 0
    latency_ms: float = 0.0


@dataclass
class OrchestratorConfig:
    """Configuration for the multi-agent orchestrator."""
    max_parallel: int = 5       # Max concurrent sub-agents
    default_model: str | None = None  # Model for sub-agents (defaults to cheap model)
    timeout_per_task: float = 120.0  # Max seconds per sub-agent task


class AgentOrchestrator:
    """Orchestrates multiple sub-agents for parallel task execution.

    Usage:
        orchestrator = AgentOrchestrator()
        tasks = [
            SubAgentTask(id="1", prompt="Research Python async patterns", role=SubAgentRole.RESEARCHER),
            SubAgentTask(id="2", prompt="Research Rust async patterns", role=SubAgentRole.RESEARCHER),
        ]
        results = await orchestrator.run_parallel(tasks)
    """

    def __init__(self, config: OrchestratorConfig | None = None) -> None:
        self.config = config or OrchestratorConfig()
        cfg = get_config()
        self.default_model = self.config.default_model or cfg.llm.cheap_model

    async def run_parallel(
        self,
        tasks: list[SubAgentTask],
        provider: str | None = None,
    ) -> list[SubAgentResult]:
        """Execute multiple sub-agent tasks in parallel.

        Each task gets its own NeoAgent instance with a role-specific
        system prompt. Tasks execute concurrently up to max_parallel.
        """
        semaphore = asyncio.Semaphore(self.config.max_parallel)
        cfg = get_config()
        prov = provider or cfg.llm.default_provider

        async def run_one(task: SubAgentTask) -> SubAgentResult:
            async with semaphore:
                return await self._run_task(task, prov)

        results = await asyncio.gather(*[run_one(t) for t in tasks])
        return list(results)

    async def _run_task(self, task: SubAgentTask, provider: str) -> SubAgentResult:
        """Execute a single sub-agent task."""
        import time
        start = time.time()

        # Build role-specific system prompt
        role_prompt = task.role_prompt or ROLE_PROMPTS.get(task.role, "")
        # The role_prompt is already injected via NeoAgent.system_prompt,
        # so we only need to pass the actual task content here.
        full_prompt = f"Complete this task and return only the result:\n{task.prompt}"

        # Prevent sub-agents from recursively delegating tasks
        from .tools import get_all_tools

        all_tools = get_all_tools()
        filtered_tools = [
            t for t in all_tools if getattr(t, "name", None) != "delegate_to_subagents"
        ]

        agent = NeoAgent(
            provider=provider,
            model=task.model or self.default_model,
            system_prompt=role_prompt,
            session_id=f"subagent_{task.id}",
            tools=filtered_tools,
            router_enabled=False,
        )

        try:
            output = await asyncio.wait_for(
                agent.invoke(full_prompt),
                timeout=self.config.timeout_per_task,
            )
            return SubAgentResult(
                task_id=task.id,
                role=task.role,
                output=output,
                success=True,
                latency_ms=(time.time() - start) * 1000,
            )
        except TimeoutError:
            return SubAgentResult(
                task_id=task.id,
                role=task.role,
                output="",
                success=False,
                error="Task timed out",
                latency_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            import traceback
            error_detail = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            # Log to stderr for diagnostics without breaking the CLI flow
            print(f"[ORCHESTRATOR] Sub-agent {task.id} failed:\n{error_detail}", file=sys.stderr)
            return SubAgentResult(
                task_id=task.id,
                role=task.role,
                output="",
                success=False,
                error=error_detail,
                latency_ms=(time.time() - start) * 1000,
            )

    async def run_sequential(
        self,
        tasks: list[SubAgentTask],
        provider: str | None = None,
    ) -> list[SubAgentResult]:
        """Execute tasks sequentially — each task sees previous results."""
        results: list[SubAgentResult] = []
        for task in tasks:
            # Inject previous results as context (create a copy to avoid mutating input)
            task_to_run = task
            if results:
                context = "\n".join(
                    f"Previous result ({r.task_id}): {r.output[:500]}"
                    for r in results if r.success
                )
                task_to_run = dataclasses.replace(
                    task,
                    prompt=f"Context from previous tasks:\n{context}\n\nTask:\n{task.prompt}",
                )

            result = await self._run_task(task_to_run, provider or get_config().llm.default_provider)
            results.append(result)
        return results

    def create_task(
        self,
        prompt: str,
        role: SubAgentRole = SubAgentRole.CUSTOM,
        task_id: str | None = None,
        **kwargs,
    ) -> SubAgentTask:
        """Factory for creating SubAgentTask objects."""
        import uuid
        return SubAgentTask(
            id=task_id or str(uuid.uuid4())[:8],
            prompt=prompt,
            role=role,
            **kwargs,
        )
