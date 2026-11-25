"""Standalone demo script for NeoAgent task scheduling + system notifications.

Usage:
    python demo_reminder.py

This sets a reminder 5 seconds in the future so you can verify:
1. Terminal alert (Rich panel style)
2. Desktop toast notification (Windows/macOS/Linux)
3. Auto-cleanup after the task fires
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from neoagent.core.tools.builtins import (
    delete_scheduled_task,
    list_scheduled_tasks,
    schedule_task,
)
from neoagent.core.tools.scheduler import get_scheduler


async def demo():
    print("=" * 60)
    print("NeoAgent Task Scheduler + System Notification Demo")
    print("=" * 60)

    scheduler = get_scheduler()
    scheduler.start()
    restored = scheduler.load_tasks()
    if restored:
        print(f"[Startup] Restored {restored} task(s) from disk\n")

    # Terminal callback
    def on_trigger(task_id: str, description: str):
        print(f"\n{'=' * 50}")
        print(f"  [REMINDER] {description}")
        print(f"  (task_id={task_id})")
        print(f"{'=' * 50}\n")

    scheduler.on_trigger(on_trigger)

    # --- 1. Schedule a reminder 5s from now ---
    future = (datetime.now() + timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[User] Schedule a meeting reminder 5 seconds from now ({future})")
    r1 = await schedule_task.ainvoke({"target_time": future, "description": "开会"})
    print(f"[Agent] {r1}\n")

    # --- 2. List pending tasks ---
    print("[User] What are my pending tasks?")
    r2 = await list_scheduled_tasks.ainvoke({})
    print(f"[Agent] {r2}\n")

    # --- 3. Wait for trigger ---
    print("[System] Waiting for the reminder to fire (~5s)...")
    await asyncio.sleep(7)

    # --- 4. List again (should be empty) ---
    print("[User] Any remaining tasks?")
    r4 = await list_scheduled_tasks.ainvoke({})
    print(f"[Agent] {r4}\n")

    # --- 5. Cancel demo ---
    future2 = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[User] Remind me to drink water in 10 minutes")
    r5 = await schedule_task.ainvoke({"target_time": future2, "description": "喝水"})
    print(f"[Agent] {r5}")

    import json
    from neoagent.core.config import get_config
    cfg = get_config()
    with open(cfg.tasks_path, "r", encoding="utf-8") as f:
        tasks = json.loads(f.read())
    task_id = tasks[-1]["id"]

    print(f"[User] Cancel that reminder (id={task_id})")
    r6 = await delete_scheduled_task.ainvoke({"task_id": task_id})
    print(f"[Agent] {r6}\n")

    print("[User] Any remaining tasks?")
    r7 = await list_scheduled_tasks.ainvoke({})
    print(f"[Agent] {r7}\n")

    scheduler.shutdown(wait=False)
    print("[OK] Demo complete. You should have seen:")
    print("   - A desktop toast notification (Windows/macOS/Linux)")
    print("   - A terminal alert panel")
    print("   - The fired task was auto-removed from the list")


if __name__ == "__main__":
    asyncio.run(demo())
