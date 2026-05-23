"""Core built-in tools — rewritten from CyberClaw with security fixes.

CyberClaw issues fixed:
- calculator: now uses AST-based SafeEvaluator (not eval())
- save_user_profile: uses structured Pydantic model (not raw markdown)
- scheduler: event-driven via APScheduler (not polling heartbeat)
- All tools: async-first, timed, capability-declared
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from ..config import get_config
from ..security import Capability, get_safe_evaluator
from ..tools.scheduler import get_scheduler
from .base import RiskLevel, neoagent_tool


# ── Structured Output Models ───────────────────────


class TimeResult(BaseModel):
    datetime_iso: str
    date: str
    time: str
    weekday: str
    unix_timestamp: float
    timezone: str = "local"


class CalcResult(BaseModel):
    expression: str
    result: str
    success: bool
    error: str | None = None


class TaskInfo(BaseModel):
    id: str
    target_time: str
    description: str
    repeat: str | None = None
    repeat_count: int | None = None


class TaskListResult(BaseModel):
    count: int
    tasks: list[TaskInfo]


class TaskActionResult(BaseModel):
    success: bool
    message: str
    task: TaskInfo | None = None


class SystemInfo(BaseModel):
    provider: str
    model: str
    framework_version: str = "2.0.0"
    capabilities: list[str] = Field(default_factory=list)


# ── Tools ──────────────────────────────────────────


@neoagent_tool(
    name="get_current_time",
    description="Get the current system time and date with full details.",
    capabilities=Capability.NONE,
    risk_level=RiskLevel.LOW,
    timeout_seconds=5.0,
)
async def get_current_time() -> str:
    now = datetime.now()
    result = TimeResult(
        datetime_iso=now.isoformat(),
        date=now.strftime("%Y-%m-%d"),
        time=now.strftime("%H:%M:%S"),
        weekday=now.strftime("%A"),
        unix_timestamp=now.timestamp(),
    )
    return (
        f"Current time: {result.date} {result.time} ({result.weekday})\n"
        f"ISO: {result.datetime_iso}\n"
        f"Unix: {result.unix_timestamp:.0f}"
    )


@neoagent_tool(
    name="calculator",
    description="Safely evaluate a mathematical expression. Supports +, -, *, /, **, %, sqrt, sin, cos, log, etc.",
    capabilities=Capability.NONE,
    risk_level=RiskLevel.LOW,
    timeout_seconds=5.0,
)
async def calculator(expression: str) -> str:
    evaluator = get_safe_evaluator()
    try:
        result = evaluator.evaluate(expression)
        return f"'{expression}' = {result}"
    except ValueError as e:
        return f"Error evaluating '{expression}': {e}"


@neoagent_tool(
    name="get_system_info",
    description="Get the current LLM provider, model, and framework version.",
    capabilities=Capability.NONE,
    risk_level=RiskLevel.LOW,
    timeout_seconds=5.0,
)
async def get_system_info() -> str:
    cfg = get_config()
    info = SystemInfo(
        provider=cfg.llm.default_provider,
        model=cfg.llm.default_model,
    )
    return (
        f"Provider: {info.provider}\n"
        f"Model: {info.model}\n"
        f"Framework: NeoAgent v{info.framework_version}"
    )


@neoagent_tool(
    name="save_user_profile",
    description=(
        "Persist a user preference or fact to long-term memory. "
        "Call this immediately when the user states a personal preference, habit, or fact about themselves "
        "(e.g. 'I like iced americano', 'I am a programmer', 'keep responses short'). "
        "This data survives across sessions and restarts."
    ),
    capabilities=Capability.USER_DATA_WRITE,
    risk_level=RiskLevel.MEDIUM,
    timeout_seconds=10.0,
)
async def save_user_profile(action: str, key: str = "", value: str = "", category: str = "") -> str:
    """Update user profile.

    Args:
        action: 'set_preference', 'add_fact', or 'add_constraint'
        key: Preference key (for set_preference), leave empty for add_fact/add_constraint
        value: Value to store
        category: Preference category (e.g. 'general', 'food', 'communication')
    """
    # Lazy import to avoid circular dependency
    from ..memory import MemoryManager
    cfg = get_config()
    mgr = MemoryManager(str(cfg.memory_dir / "user_profile.json"))

    if action == "set_preference":
        mgr.profile.set_preference(key, value, category or "general")
        mgr.save_profile()
        return f"Preference '{key}' updated to '{value}'."
    elif action == "add_fact":
        mgr.profile.add_fact(value)
        mgr.save_profile()
        return f"Fact added: {value}"
    elif action == "add_constraint":
        mgr.profile.add_constraint(value)
        mgr.save_profile()
        return f"Constraint added: {value}"
    else:
        return f"Unknown action: {action}. Use set_preference, add_fact, or add_constraint."


# ── Task Scheduler Tools ───────────────────────────


def _read_tasks() -> list[dict]:
    cfg = get_config()
    if not os.path.exists(cfg.tasks_path):
        return []
    with open(cfg.tasks_path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        return json.loads(content) if content else []


def _write_tasks(tasks: list[dict]) -> None:
    cfg = get_config()
    os.makedirs(os.path.dirname(cfg.tasks_path), exist_ok=True)
    with open(cfg.tasks_path, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)


@neoagent_tool(
    name="schedule_task",
    description="Schedule a task or reminder for a future time. Supports one-shot and recurring tasks.",
    capabilities=Capability.SYSTEM_SCHEDULE,
    risk_level=RiskLevel.MEDIUM,
    timeout_seconds=10.0,
)
async def schedule_task(
    target_time: str,
    description: str,
    repeat: str | None = None,
    repeat_count: int | None = None,
) -> str:
    # Validate time format
    try:
        target_dt = datetime.strptime(target_time, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "Error: target_time must be 'YYYY-MM-DD HH:MM:SS' format."

    now = datetime.now()
    if target_dt <= now:
        return f"Error: target_time ({target_time}) must be in the future. Current time: {now.strftime('%Y-%m-%d %H:%M:%S')}"

    if repeat and repeat not in ("hourly", "daily", "weekly", "monthly"):
        return f"Error: repeat must be one of: hourly, daily, weekly, monthly. Got: '{repeat}'"

    task_id = str(uuid.uuid4())[:8]
    tasks = _read_tasks()
    new_task = {
        "id": task_id,
        "target_time": target_time,
        "description": description,
        "repeat": repeat,
        "repeat_count": repeat_count,
    }
    tasks.append(new_task)
    _write_tasks(tasks)

    # Register with the actual scheduler engine
    try:
        scheduler = get_scheduler()
        scheduler.add_task(
            task_id=task_id,
            target_time=target_dt,
            description=description,
            repeat=repeat,
            repeat_count=repeat_count,
        )
    except Exception:
        pass  # Scheduler may not be started yet; task is still persisted in JSON

    msg = f"Task scheduled: '{description}' at {target_time}"
    if repeat:
        msg += f" | Repeats: {repeat}" + (f" x{repeat_count}" if repeat_count else " (indefinitely)")
    return msg


@neoagent_tool(
    name="list_scheduled_tasks",
    description="List all pending scheduled tasks.",
    capabilities=Capability.SYSTEM_SCHEDULE,
    risk_level=RiskLevel.LOW,
    timeout_seconds=5.0,
)
async def list_scheduled_tasks() -> str:
    tasks = _read_tasks()
    if not tasks:
        return "No scheduled tasks."

    tasks.sort(key=lambda t: t["target_time"])
    lines = [f"{len(tasks)} task(s):"]
    for t in tasks:
        repeat_info = f" (repeats {t['repeat']} x{t.get('repeat_count', '∞')})" if t.get("repeat") else ""
        lines.append(f"  [{t['id']}] {t['target_time']} — {t['description']}{repeat_info}")
    return "\n".join(lines)


@neoagent_tool(
    name="delete_scheduled_task",
    description="Delete a scheduled task by its ID.",
    capabilities=Capability.SYSTEM_SCHEDULE,
    risk_level=RiskLevel.HIGH,
    timeout_seconds=5.0,
)
async def delete_scheduled_task(task_id: str) -> str:
    tasks = _read_tasks()
    new_tasks = [t for t in tasks if t["id"] != task_id]
    if len(new_tasks) == len(tasks):
        return f"Task '{task_id}' not found."
    _write_tasks(new_tasks)

    try:
        get_scheduler().remove_task(task_id)
    except Exception:
        pass

    return f"Task '{task_id}' deleted."


@neoagent_tool(
    name="modify_scheduled_task",
    description="Modify an existing scheduled task's time or description by ID.",
    capabilities=Capability.SYSTEM_SCHEDULE,
    risk_level=RiskLevel.HIGH,
    timeout_seconds=10.0,
)
async def modify_scheduled_task(
    task_id: str,
    new_time: str | None = None,
    new_description: str | None = None,
) -> str:
    if not new_time and not new_description:
        return "Error: provide new_time or new_description."

    if new_time:
        try:
            target_dt = datetime.strptime(new_time, "%Y-%m-%d %H:%M:%S")
            if target_dt <= datetime.now():
                return f"Error: new_time must be in the future."
        except ValueError:
            return "Error: new_time must be 'YYYY-MM-DD HH:MM:SS' format."

    tasks = _read_tasks()
    task_obj = None
    for t in tasks:
        if t["id"] == task_id:
            task_obj = t
            if new_time:
                t["target_time"] = new_time
            if new_description:
                t["description"] = new_description
            _write_tasks(tasks)
            break

    if task_obj is None:
        return f"Task '{task_id}' not found."

    # Re-register with scheduler so changes take effect
    try:
        scheduler = get_scheduler()
        scheduler.remove_task(task_id)
        updated_time = datetime.strptime(task_obj["target_time"], "%Y-%m-%d %H:%M:%S")
        scheduler.add_task(
            task_id=task_id,
            target_time=updated_time,
            description=task_obj["description"],
            repeat=task_obj.get("repeat"),
            repeat_count=task_obj.get("repeat_count"),
        )
    except Exception:
        pass

    return f"Task '{task_id}' updated."
