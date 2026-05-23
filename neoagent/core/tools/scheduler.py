"""Event-driven task scheduler — replacement for CyberClaw's polling heartbeat.

CyberClaw's heartbeat.py polls tasks.json every 10 seconds in an infinite
async loop. That's wasteful for sparse task queues and adds constant
background CPU usage.

This module uses APScheduler for event-driven scheduling:
- Tasks fire exactly at their target time (no polling delay)
- Persistent job store via SQLite
- Cron-style expressions for complex schedules
- Job misfire handling and recovery
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from ..config import get_config
from ..notify import notify_in_thread

logger = logging.getLogger("neoagent.scheduler")

# Module-level callback registry so the top-level _fire_job can reach them
_task_callbacks: list[callable] = []


async def _fire_job(task_id: str, description: str) -> None:
    """Top-level job executor — called by APScheduler when a task fires.

    Kept as a module-level function so it can be pickled by APScheduler's
    SQLAlchemy job store (instance methods that reference a Scheduler fail
    to serialize).
    """
    # 1. Desktop notification (non-blocking, cross-platform)
    notify_in_thread("NeoAgent 提醒", f"⏰ {description}")

    # 2. Terminal / in-app callbacks
    for cb in _task_callbacks:
        try:
            if asyncio.iscoroutinefunction(cb):
                await cb(task_id, description)
            else:
                cb(task_id, description)
        except Exception as e:
            logger.error(f"Task callback error (task={task_id}): {e}")

    # 3. Clean up one-shot tasks from tasks.json
    try:
        # Use the singleton's IO helpers if available, otherwise do it inline
        sched = get_scheduler()
        tasks = sched._read_tasks_json()
        new_tasks = [t for t in tasks if t["id"] != task_id]
        if len(new_tasks) < len(tasks):
            sched._write_tasks_json(new_tasks)
    except Exception:
        pass


class TaskScheduler:
    """Event-driven task scheduler using APScheduler.

    Advantages over CyberClaw's heartbeat polling:
    - Zero polling: tasks fire at exact target time
    - Persistent: survives process restart via SQLite job store
    - Cron support: complex recurring schedules without custom logic
    - Misfire handling: detects and handles missed executions
    - Graceful shutdown: ensures jobs complete before exit
    """

    def __init__(self, db_url: str | None = None) -> None:
        cfg = get_config()
        db_url = db_url or f"sqlite:///{cfg.workspace_dir}/scheduler.sqlite3"

        jobstores = {
            "default": SQLAlchemyJobStore(url=db_url),
        }
        # Use system default timezone (usually local time) so naive datetimes
        # from user input are interpreted correctly.
        self.scheduler = AsyncIOScheduler(jobstores=jobstores)

    def on_trigger(self, callback: callable) -> None:
        """Register a callback for when a task triggers."""
        global _task_callbacks
        _task_callbacks.append(callback)

    def add_task(
        self,
        task_id: str,
        target_time: datetime,
        description: str,
        repeat: str | None = None,
        repeat_count: int | None = None,
    ) -> None:
        """Schedule a one-shot or recurring task."""
        if repeat:
            cron_map = {
                "hourly": CronTrigger(minute=target_time.minute),
                "daily": CronTrigger(hour=target_time.hour, minute=target_time.minute),
                "weekly": CronTrigger(
                    day_of_week=target_time.weekday(),
                    hour=target_time.hour,
                    minute=target_time.minute,
                ),
                "monthly": CronTrigger(
                    day=target_time.day,
                    hour=target_time.hour,
                    minute=target_time.minute,
                ),
            }
            trigger = cron_map.get(repeat, DateTrigger(run_date=target_time))
        else:
            trigger = DateTrigger(run_date=target_time)

        self.scheduler.add_job(
            _fire_job,
            trigger=trigger,
            args=[task_id, description],
            id=task_id,
            replace_existing=True,
            max_instances=1,
        )

    def remove_task(self, task_id: str) -> bool:
        """Remove a scheduled task. Returns True if found and removed."""
        try:
            self.scheduler.remove_job(task_id)
            return True
        except Exception:
            return False

    def get_task(self, task_id: str) -> dict | None:
        """Get info about a scheduled task."""
        job = self.scheduler.get_job(task_id)
        if job is None:
            return None
        return {
            "id": job.id,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            "trigger": str(job.trigger),
        }

    def list_tasks(self) -> list[dict]:
        """List all scheduled tasks."""
        jobs = self.scheduler.get_jobs()
        return [
            {
                "id": job.id,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            }
            for job in jobs
        ]

    def start(self) -> None:
        self.scheduler.start()

    def _read_tasks_json(self) -> list[dict]:
        """Read tasks from tasks.json."""
        cfg = get_config()
        path = cfg.tasks_path
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                return json.loads(content) if content else []
        except Exception:
            return []

    def _write_tasks_json(self, tasks: list[dict]) -> None:
        """Write tasks to tasks.json."""
        cfg = get_config()
        path = cfg.tasks_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)

    def load_tasks(self) -> int:
        """Load tasks from tasks.json into the scheduler (used on startup)."""
        tasks = self._read_tasks_json()
        loaded = 0
        now = datetime.now()
        for t in tasks:
            try:
                target_dt = datetime.strptime(t["target_time"], "%Y-%m-%d %H:%M:%S")
                # Skip one-shot tasks that are already in the past
                if not t.get("repeat") and target_dt <= now:
                    continue
                self.add_task(
                    task_id=t["id"],
                    target_time=target_dt,
                    description=t["description"],
                    repeat=t.get("repeat"),
                    repeat_count=t.get("repeat_count"),
                )
                loaded += 1
            except Exception:
                continue
        return loaded

    def shutdown(self, wait: bool = True) -> None:
        if getattr(self, "_shutdown", False):
            return
        self._shutdown = True
        try:
            self.scheduler.shutdown(wait=wait)
        except Exception:
            pass


# Singleton
_scheduler: TaskScheduler | None = None


def get_scheduler() -> TaskScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = TaskScheduler()
    return _scheduler
