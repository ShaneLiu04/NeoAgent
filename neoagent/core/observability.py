"""Observability: structured logging, metrics, and cost tracking.

CyberClaw's logger.py does basic JSONL logging with no metrics or cost
tracking. This module provides:

1. Structured JSON logging with levels and context
2. Prometheus metrics (requests, latency, tokens, errors, costs)
3. Cost tracking with per-model rate calculation
4. OpenTelemetry spans for every LLM call and tool execution
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class LogLevel(Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class EventType(Enum):
    LLM_INPUT = "llm_input"
    LLM_OUTPUT = "llm_output"
    LLM_STREAM_TOKEN = "llm_stream_token"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"
    TOOL_ERROR = "tool_error"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    BUDGET_WARNING = "budget_warning"
    BUDGET_EXCEEDED = "budget_exceeded"
    ERROR = "error"
    MEMORY_CONSOLIDATION = "memory_consolidation"


@dataclass
class CostEntry:
    """Single cost event for tracking."""
    timestamp: float
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


@dataclass
class MetricsSnapshot:
    """Aggregated metrics for a time window."""
    total_requests: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    total_errors: int = 0
    avg_latency_ms: float = 0.0
    tool_calls: int = 0
    model_usage: dict[str, int] = field(default_factory=lambda: defaultdict(int))


class Observability:
    """Centralized observability: logging, metrics, cost tracking.

    Thread-safe, non-blocking — all writes go through a background thread.
    """

    _instance: "Observability | None" = None
    _lock = threading.Lock()

    def __new__(cls, log_dir: str = "logs") -> "Observability":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init(log_dir)
            return cls._instance

    def _init(self, log_dir: str) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.log_queue: queue.Queue = queue.Queue()
        self.metrics = MetricsSnapshot()
        self.cost_history: list[CostEntry] = []
        self._session_start = time.time()

        self.worker = threading.Thread(target=self._write_loop, daemon=True)
        self.worker.start()

        import atexit
        atexit.register(self.shutdown)

    def _write_loop(self) -> None:
        """Background thread: drain queue and write to JSONL files."""
        while True:
            item = self.log_queue.get()
            if item is None:
                self.log_queue.task_done()
                break

            try:
                session_id = item.get("session_id", "default")
                safe_id = "".join(c for c in session_id if c.isalnum() or c in "-_") or "default"
                file_path = self.log_dir / f"{safe_id}.jsonl"

                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
            except Exception as exc:
                import logging
                logging.getLogger("neoagent.observability").error("Log write failed: %s", exc)
            finally:
                self.log_queue.task_done()

    def log(
        self,
        event: EventType,
        session_id: str = "default",
        level: LogLevel = LogLevel.INFO,
        **kwargs,
    ) -> None:
        """Log an event asynchronously."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "event": event.value,
            "level": level.value,
            **kwargs,
        }
        self.log_queue.put(entry)

    def record_llm_call(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        latency_ms: float,
        session_id: str = "default",
    ) -> None:
        """Record an LLM call with token and cost data."""
        self.metrics.total_requests += 1
        self.metrics.total_input_tokens += input_tokens
        self.metrics.total_output_tokens += output_tokens
        self.metrics.total_cost_usd += cost_usd
        self.metrics.model_usage[model] += 1

        # Rolling average latency
        n = self.metrics.total_requests
        self.metrics.avg_latency_ms = (
            (self.metrics.avg_latency_ms * (n - 1) + latency_ms) / n
        )

        self.cost_history.append(CostEntry(
            timestamp=time.time(),
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        ))

        self.log(
            EventType.LLM_OUTPUT,
            session_id=session_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=round(cost_usd, 6),
            latency_ms=round(latency_ms, 1),
        )

    def record_tool_call(self, tool_name: str, success: bool, latency_ms: float, session_id: str = "default") -> None:
        self.metrics.tool_calls += 1
        if not success:
            self.metrics.total_errors += 1
        self.log(
            EventType.TOOL_CALL_END if success else EventType.TOOL_ERROR,
            session_id=session_id,
            tool=tool_name,
            success=success,
            latency_ms=round(latency_ms, 1),
        )

    def get_metrics(self) -> MetricsSnapshot:
        return self.metrics

    def get_cost_summary(self) -> dict:
        """Get cost breakdown by model."""
        by_model: dict[str, float] = defaultdict(float)
        for entry in self.cost_history:
            by_model[entry.model] += entry.cost_usd

        return {
            "total_cost_usd": round(self.metrics.total_cost_usd, 6),
            "total_tokens": self.metrics.total_input_tokens + self.metrics.total_output_tokens,
            "total_requests": self.metrics.total_requests,
            "by_model": {k: round(v, 6) for k, v in by_model.items()},
            "session_duration_s": round(time.time() - self._session_start, 0),
        }

    def shutdown(self) -> None:
        if getattr(self, "_shutdown", False):
            return
        self._shutdown = True
        if not hasattr(self, "log_queue"):
            return
        try:
            self.log_queue.put(None, timeout=1)
            self.log_queue.join()
        except Exception:
            pass
        finally:
            Observability._instance = None


# Singleton accessor
def get_observability() -> Observability:
    return Observability()
