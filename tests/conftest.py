"""Pytest configuration and lightweight async/offline test support."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "asyncio: mark test as an async coroutine")


try:
    import pytest_asyncio  # type: ignore[import-not-found]  # noqa: F401
except ImportError:
    @pytest.hookimpl(tryfirst=True)
    def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
        test_func: Any = pyfuncitem.obj
        if inspect.iscoroutinefunction(test_func):
            sig = inspect.signature(test_func)
            call_kwargs = {
                name: pyfuncitem.funcargs[name]
                for name in sig.parameters
                if name in pyfuncitem.funcargs
            }
            asyncio.run(test_func(**call_kwargs))
            return True
        return None
