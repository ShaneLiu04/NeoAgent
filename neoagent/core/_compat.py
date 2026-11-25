"""Lightweight compatibility layer for optional heavy dependencies.

When ``pydantic``, ``pydantic-settings``, or ``langchain-core`` are installed,
this module re-exports the real classes untouched (zero overhead or behavior
change). When running in minimal CI/CD containers or lightweight Python 3.11+
environments where only the DevOps control plane is needed, clean stdlib
equivalents keep configuration, security, memory, tools, skills, and the
DevOps engine 100% functional and testable.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import os
from collections.abc import Callable
from typing import Any

# ── Pydantic / Pydantic-Settings ──────────────────────────────────────────────

try:
    from pydantic import BaseModel, Field  # type: ignore[import-not-found]

    HAS_PYDANTIC = True
except ImportError:  # pragma: no cover - exercised in minimal CI envs
    HAS_PYDANTIC = False

    class _FieldSpec:
        def __init__(
            self,
            default: Any = dataclasses.MISSING,
            default_factory: Callable[[], Any] | None = None,
            alias: str | None = None,
            **kwargs: Any,
        ) -> None:
            self.default = default
            self.default_factory = default_factory
            self.alias = alias
            self.extra = kwargs

    def Field(  # type: ignore[misc]
        default: Any = dataclasses.MISSING,
        *,
        default_factory: Callable[[], Any] | None = None,
        alias: str | None = None,
        **kwargs: Any,
    ) -> Any:
        return _FieldSpec(default=default, default_factory=default_factory, alias=alias, **kwargs)

    class BaseModel:  # type: ignore[no-redef]
        """Minimal Pydantic v2 compatible BaseModel backed by stdlib."""

        def __init__(self, **kwargs: Any) -> None:
            cls_fields = self._collect_specs()
            for name, spec in cls_fields.items():
                if name in kwargs:
                    val = kwargs.pop(name)
                elif isinstance(spec, _FieldSpec):
                    if spec.default_factory is not None:
                        val = spec.default_factory()
                    elif spec.default is not dataclasses.MISSING:
                        val = spec.default
                    else:
                        val = None
                else:
                    val = spec
                setattr(self, name, val)
            for k, v in kwargs.items():
                setattr(self, k, v)

        @classmethod
        def _collect_specs(cls) -> dict[str, Any]:
            specs: dict[str, Any] = {}
            for base in reversed(cls.__mro__):
                if base in (object, BaseModel):
                    continue
                ann = getattr(base, "__annotations__", {})
                for name in ann:
                    if name.startswith("_") or name == "model_config":
                        continue
                    specs[name] = getattr(base, name, None)
                for name, val in base.__dict__.items():
                    if name.startswith("_") or name == "model_config" or callable(val) or isinstance(val, property):
                        continue
                    specs.setdefault(name, val)
            return specs

        def model_dump(self) -> dict[str, Any]:
            def _conv(v: Any) -> Any:
                if isinstance(v, BaseModel):
                    return v.model_dump()
                if isinstance(v, list):
                    return [_conv(x) for x in v]
                if isinstance(v, dict):
                    return {k: _conv(val) for k, val in v.items()}
                return v

            return {k: _conv(getattr(self, k)) for k in self._collect_specs() if hasattr(self, k)}

        def model_dump_json(self, indent: int | None = None) -> str:
            return json.dumps(self.model_dump(), ensure_ascii=False, indent=indent, default=str)

        @classmethod
        def model_validate(cls, data: dict[str, Any]) -> Any:
            if not isinstance(data, dict):
                raise ValueError(f"Expected dict for {cls.__name__}, got {type(data).__name__}")
            ann = {}
            for base in reversed(cls.__mro__):
                ann.update(getattr(base, "__annotations__", {}))
            converted = dict(data)
            # Handle nested list[UserPreference]-style models when annotations name a sibling class
            mod_globals = inspect.getmodule(cls).__dict__ if inspect.getmodule(cls) else {}
            for k, v in list(converted.items()):
                hint = str(ann.get(k, ""))
                if isinstance(v, list) and "list[" in hint:
                    inner_name = hint.split("list[", 1)[1].rstrip("]").strip()
                    inner_cls = mod_globals.get(inner_name)
                    if isinstance(inner_cls, type) and issubclass(inner_cls, BaseModel):
                        converted[k] = [
                            inner_cls.model_validate(item) if isinstance(item, dict) else item
                            for item in v
                        ]
            return cls(**converted)


try:
    from pydantic_settings import BaseSettings, SettingsConfigDict  # type: ignore[import-not-found]

    HAS_PYDANTIC_SETTINGS = True
except ImportError:  # pragma: no cover - exercised in minimal CI envs
    HAS_PYDANTIC_SETTINGS = False

    def SettingsConfigDict(**kwargs: Any) -> dict[str, Any]:  # type: ignore[misc]
        return dict(kwargs)


if not HAS_PYDANTIC_SETTINGS and HAS_PYDANTIC:
    # pydantic present without pydantic-settings: resolve env vars, let pydantic coerce types.
    class BaseSettings(BaseModel):  # type: ignore[no-redef]
        model_config = {"populate_by_name": True, "extra": "ignore"}

        def __init__(self, **kwargs: Any) -> None:
            prefix = str(type(self).model_config.get("env_prefix", ""))
            resolved = dict(kwargs)
            for name, field in type(self).model_fields.items():
                if name in resolved or (field.alias and field.alias in resolved):
                    continue
                for env_name in (field.alias, f"{prefix}{name}".upper()):
                    if env_name and env_name in os.environ:
                        resolved[field.alias or name] = os.environ[env_name]
                        break
            super().__init__(**resolved)

elif not HAS_PYDANTIC_SETTINGS:

    def _cast_env(raw: str, hint: str, default: Any) -> Any:
        h = hint.lower()
        if isinstance(default, bool) or "bool" in h:
            return raw.strip().lower() in ("1", "true", "yes", "on")
        if isinstance(default, int) or h in ("int",):
            return int(raw)
        if isinstance(default, float) or h in ("float",):
            return float(raw)
        return raw

    class BaseSettings(BaseModel):  # type: ignore[no-redef]
        model_config: dict[str, Any] = {}

        def __init__(self, **kwargs: Any) -> None:
            prefix = self.model_config.get("env_prefix", "")
            ann: dict[str, Any] = {}
            for base in reversed(self.__class__.__mro__):
                ann.update(getattr(base, "__annotations__", {}))

            resolved = dict(kwargs)
            for name, spec in self._collect_specs().items():
                if name in resolved:
                    continue
                alias = spec.alias if isinstance(spec, _FieldSpec) else None
                default = (
                    spec.default
                    if isinstance(spec, _FieldSpec) and spec.default is not dataclasses.MISSING
                    else None
                )
                env_candidates = []
                if alias:
                    env_candidates.append(alias)
                env_candidates.append(f"{prefix}{name}".upper())
                for env_name in env_candidates:
                    if env_name in os.environ:
                        resolved[name] = _cast_env(
                            os.environ[env_name], str(ann.get(name, "")), default
                        )
                        break
            super().__init__(**resolved)


# ── LangChain Core Messages & Tools ───────────────────────────────────────────

try:
    from langchain_core.messages import (  # type: ignore[import-not-found]
        AIMessage,
        BaseMessage,
        HumanMessage,
        RemoveMessage,
        SystemMessage,
        ToolMessage,
    )
    from langchain_core.tools import BaseTool, StructuredTool  # type: ignore[import-not-found]

    HAS_LANGCHAIN = True
except ImportError:  # pragma: no cover - exercised in minimal CI envs
    HAS_LANGCHAIN = False
    import uuid

    class BaseMessage:  # type: ignore[no-redef]
        type: str = "base"

        def __init__(
            self,
            content: Any = "",
            *,
            id: str | None = None,
            additional_kwargs: dict[str, Any] | None = None,
            response_metadata: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> None:
            self.content = content
            self.id = id or str(uuid.uuid4())
            self.additional_kwargs = additional_kwargs or {}
            self.response_metadata = response_metadata or {}
            for k, v in kwargs.items():
                setattr(self, k, v)

    class HumanMessage(BaseMessage):  # type: ignore[no-redef]
        type = "human"

    class SystemMessage(BaseMessage):  # type: ignore[no-redef]
        type = "system"

    class AIMessage(BaseMessage):  # type: ignore[no-redef]
        type = "ai"

        def __init__(
            self,
            content: Any = "",
            *,
            tool_calls: list[dict[str, Any]] | None = None,
            **kwargs: Any,
        ) -> None:
            super().__init__(content, **kwargs)
            self.tool_calls = tool_calls or []

    class ToolMessage(BaseMessage):  # type: ignore[no-redef]
        type = "tool"

        def __init__(
            self,
            content: Any = "",
            *,
            tool_call_id: str = "",
            name: str | None = None,
            **kwargs: Any,
        ) -> None:
            super().__init__(content, **kwargs)
            self.tool_call_id = tool_call_id
            self.name = name

    class RemoveMessage(BaseMessage):  # type: ignore[no-redef]
        type = "remove"

        def __init__(self, id: str) -> None:
            super().__init__("", id=id)

    class BaseTool:  # type: ignore[no-redef]
        name: str = ""
        description: str = ""

        def invoke(self, input_data: dict[str, Any] | None = None) -> Any:
            return self._run(**(input_data or {}))

        async def ainvoke(self, input_data: dict[str, Any] | None = None) -> Any:
            return await self._arun(**(input_data or {}))

        def _run(self, **kwargs: Any) -> Any:
            raise NotImplementedError

        async def _arun(self, **kwargs: Any) -> Any:
            raise NotImplementedError

    class StructuredTool(BaseTool):  # type: ignore[no-redef]
        def __init__(
            self,
            func: Callable[..., Any] | None,
            coroutine: Callable[..., Any] | None,
            name: str,
            description: str,
        ) -> None:
            self._func = func
            self._coroutine = coroutine
            self.name = name
            self.description = description

        @classmethod
        def from_function(
            cls,
            func: Callable[..., Any] | None = None,
            coroutine: Callable[..., Any] | None = None,
            name: str = "",
            description: str = "",
            **_: Any,
        ) -> StructuredTool:
            return cls(func=func, coroutine=coroutine, name=name, description=description)

        def invoke(self, input_data: dict[str, Any] | None = None) -> Any:
            kwargs = input_data or {}
            if self._func is not None:
                return self._func(**kwargs)
            if self._coroutine is not None:
                return asyncio.run(self._coroutine(**kwargs))
            raise RuntimeError(f"Tool '{self.name}' has no callable")

        async def ainvoke(self, input_data: dict[str, Any] | None = None) -> Any:
            kwargs = input_data or {}
            if self._coroutine is not None:
                return await self._coroutine(**kwargs)
            if self._func is not None:
                return self._func(**kwargs)
            raise RuntimeError(f"Tool '{self.name}' has no callable")
