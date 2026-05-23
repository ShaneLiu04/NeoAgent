"""Tests for built-in tools."""
import pytest
from neoagent.core.tools.builtins import (
    calculator,
    get_current_time,
    get_system_info,
    schedule_task,
    list_scheduled_tasks,
    delete_scheduled_task,
)


class TestCalculatorTool:
    def test_simple_calc(self):
        result = calculator.invoke({"expression": "2 + 3"})
        assert "5" in result

    def test_complex_expr(self):
        result = calculator.invoke({"expression": "sqrt(25) + 3 * 2"})
        assert "11.0" in result or "11" in result

    def test_error_expr(self):
        result = calculator.invoke({"expression": "__import__('os')"})
        assert "Error" in result or "error" in result


class TestTimeTool:
    def test_returns_time(self):
        result = get_current_time.invoke({})
        assert "Current time" in result
        assert ":" in result

    def test_system_info(self):
        result = get_system_info.invoke({})
        assert "Provider" in result or "Model" in result
