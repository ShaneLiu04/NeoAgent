"""Enhanced tool base with capability declarations, timeouts, and structured output.

Key improvements over CyberClaw's base.py:
1. Every tool declares required capabilities — enforced by security system
2. Built-in timeout with asyncio.wait_for
3. Structured output via Pydantic return models
4. Retry policy declaration
5. Audit metadata (tool version, author, risk level)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .._compat import BaseTool, StructuredTool
from ..security import Capability, CapabilityToken


class RiskLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class ToolMetadata:
    """Rich metadata for every tool — enables security auditing and discovery."""
    name: str
    description: str
    version: str = "1.0.0"
    author: str = "neoagent"
    capabilities: Capability = Capability.NONE
    risk_level: RiskLevel = RiskLevel.LOW
    timeout_seconds: float = 30.0
    max_retries: int = 2
    tags: list[str] = field(default_factory=list)


# Global registry: tool_name -> ToolMetadata
_tool_registry: dict[str, ToolMetadata] = {}


def get_tool_metadata(tool_name: str) -> ToolMetadata | None:
    """Look up metadata for a registered tool."""
    return _tool_registry.get(tool_name)


def list_tools() -> dict[str, ToolMetadata]:
    """Get all registered tools and their metadata."""
    return dict(_tool_registry)


class NeoAgentTool(BaseTool, ABC):
    """Base class for class-based NeoAgent tools (advanced use).

    Prefer the @neoagent_tool decorator for simple tools.
    """
    capability_token: CapabilityToken | None = None

    @abstractmethod
    def get_metadata(self) -> ToolMetadata:
        ...

    def _validate_capabilities(self) -> None:
        meta = self.get_metadata()
        if self.capability_token and meta.capabilities != Capability.NONE:
            self.capability_token.require(meta.capabilities)

    def _run(self, **kwargs: Any) -> Any:
        return asyncio.run(self._arun(**kwargs))

    async def _arun(self, **kwargs: Any) -> Any:
        self._validate_capabilities()
        try:
            return await asyncio.wait_for(
                self._execute(**kwargs),
                timeout=self.get_metadata().timeout_seconds,
            )
        except TimeoutError:
            return {
                "error": f"Tool '{self.get_metadata().name}' timed out",
                "status": "timeout",
            }

    @abstractmethod
    async def _execute(self, **kwargs: Any) -> Any:
        ...


def neoagent_tool(
    *,
    name: str | None = None,
    description: str | None = None,
    capabilities: Capability = Capability.NONE,
    risk_level: RiskLevel = RiskLevel.LOW,
    timeout_seconds: float = 30.0,
    max_retries: int = 2,
    tags: list[str] | None = None,
):
    """Decorator to create a NeoAgent tool from a function.

    Stores metadata in a global registry to avoid Pydantic field conflicts.

    Usage:
        @neoagent_tool(
            name="search_web",
            capabilities=Capability.NETWORK_OUTBOUND,
            risk_level=RiskLevel.MEDIUM,
        )
        async def search_web(query: str) -> dict:
            ...
    """
    def decorator(func: Callable) -> StructuredTool:
        tool_name = name or func.__name__
        meta = ToolMetadata(
            name=tool_name,
            description=description or func.__doc__ or "",
            capabilities=capabilities,
            risk_level=risk_level,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            tags=tags or [],
        )

        if asyncio.iscoroutinefunction(func):
            # Helper to run a coroutine safely even when an event loop is already running.
            def _run_in_thread(coro: Any) -> Any:
                loop = asyncio.new_event_loop()
                try:
                    return loop.run_until_complete(coro)
                finally:
                    loop.close()

            def _run_coro_sync(coro: Any) -> Any:
                try:
                    return asyncio.run(coro)
                except RuntimeError:
                    # Already inside a running event loop — offload to a fresh thread.
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        return pool.submit(_run_in_thread, coro).result()

            @functools.wraps(func)
            def sync_wrapper(**kwargs: Any) -> Any:
                return _run_coro_sync(asyncio.wait_for(func(**kwargs), timeout=timeout_seconds))

            tool_obj = StructuredTool.from_function(
                func=sync_wrapper,
                coroutine=func,
                name=meta.name,
                description=meta.description,
            )
        else:
            @functools.wraps(func)
            def wrapper(**kwargs: Any) -> Any:
                return func(**kwargs)

            tool_obj = StructuredTool.from_function(
                func=wrapper,
                name=meta.name,
                description=meta.description,
            )

        # Register in global registry (no Pydantic field conflicts)
        _tool_registry[meta.name] = meta
        return tool_obj

    return decorator
