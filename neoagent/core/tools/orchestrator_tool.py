"""Orchestrator tool for multi-agent delegation.

Enables the main agent to spawn parallel sub-agents for independent tasks.
"""

from __future__ import annotations

import json

from .base import RiskLevel
from .base import neoagent_tool
from ..security import Capability


@neoagent_tool(
    name="delegate_to_subagents",
    description=(
        "Delegate multiple independent tasks to sub-agents for parallel or sequential execution. "
        "Use this when the user asks to research, analyze, or process multiple topics "
        "simultaneously (e.g. 'compare A, B, and C' or 'research X, Y, and Z then summarize'). "
        "Each sub-agent runs in its own context with appropriate tools. "
        "Parameters: "
        "tasks_json (str) – a JSON string containing a list of task objects. Each object has: "
        "  id (str): unique task identifier; "
        "  description (str): the task prompt; "
        "  role (str, optional): one of researcher, coder, analyzer, summarizer, reviewer, custom. "
        "mode (str, optional) – 'parallel' (default) or 'sequential'."
    ),
    capabilities=Capability.NONE,
    risk_level=RiskLevel.MEDIUM,
    timeout_seconds=300.0,
    tags=["orchestrator", "multi-agent", "parallel"],
)
async def delegate_to_subagents(tasks_json: str, mode: str = "parallel") -> str:
    """Delegate tasks to sub-agents and return aggregated results."""
    # Delayed imports avoid circular dependency: orchestrator -> agent -> tools -> this module
    from ..orchestrator import AgentOrchestrator
    from ..orchestrator import SubAgentRole
    from ..orchestrator import SubAgentTask

    try:
        raw_tasks = json.loads(tasks_json)
    except json.JSONDecodeError as exc:
        return f"[ORCHESTRATOR_ERROR] Invalid JSON: {exc}"

    if not isinstance(raw_tasks, list):
        return "[ORCHESTRATOR_ERROR] tasks_json must be a JSON array."

    if not raw_tasks:
        return "[ORCHESTRATOR_ERROR] No tasks provided."

    tasks: list[SubAgentTask] = []
    for idx, item in enumerate(raw_tasks):
        if not isinstance(item, dict):
            return f"[ORCHESTRATOR_ERROR] Task at index {idx} is not an object."

        role_str = item.get("role", "custom")
        try:
            role = SubAgentRole(role_str)
        except ValueError:
            role = SubAgentRole.CUSTOM

        tasks.append(
            SubAgentTask(
                id=item.get("id", f"task-{idx}"),
                prompt=item.get("description", ""),
                role=role,
            )
        )

    from ..config import get_config

    cfg = get_config()
    orchestrator = AgentOrchestrator(
        OrchestratorConfig(default_model=cfg.llm.default_model)
    )

    try:
        if mode == "sequential":
            results = await orchestrator.run_sequential(tasks)
        else:
            results = await orchestrator.run_parallel(tasks)
    except Exception as exc:
        return f"[ORCHESTRATOR_ERROR] Execution failed: {exc}"

    lines: list[str] = []
    for r in results:
        status = "OK" if r.success else "FAIL"
        lines.append(
            f"\n--- Task {r.task_id} ({r.role.value}) [{status}] {r.latency_ms:.0f}ms ---"
        )
        if r.error:
            lines.append(f"Error: {r.error}")
        lines.append(r.output.strip() if r.output else "(no output)")

    return "\n".join(lines)
