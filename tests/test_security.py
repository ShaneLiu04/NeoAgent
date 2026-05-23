"""Tests for security module — safe evaluator, capability system, path confinement."""
import pytest
from neoagent.core.security import (
    Capability,
    CapabilityToken,
    SafeEvaluator,
    get_safe_evaluator,
)


class TestSafeEvaluator:
    @pytest.fixture
    def evaluator(self):
        return get_safe_evaluator()

    def test_simple_arithmetic(self, evaluator):
        assert evaluator.evaluate("2 + 2") == 4
        assert evaluator.evaluate("3 * 5") == 15
        assert evaluator.evaluate("10 / 3") == pytest.approx(3.333, rel=1e-2)
        assert evaluator.evaluate("2 ** 10") == 1024

    def test_math_functions(self, evaluator):
        assert evaluator.evaluate("sqrt(16)") == 4.0
        assert evaluator.evaluate("abs(-5)") == 5
        assert evaluator.evaluate("round(3.7)") == 4
        assert evaluator.evaluate("min(3, 1, 4)") == 1
        assert evaluator.evaluate("max(3, 1, 4)") == 4

    def test_comparisons(self, evaluator):
        assert evaluator.evaluate("5 > 3") is True
        assert evaluator.evaluate("3 == 3") is True
        assert evaluator.evaluate("5 < 3") is False

    def test_complex_expression(self, evaluator):
        result = evaluator.evaluate("sqrt(16) + 2 * 3 - 4 / 2")
        assert result == pytest.approx(8.0)

    def test_blocks_eval(self, evaluator):
        with pytest.raises(ValueError):
            evaluator.evaluate("__import__('os').system('ls')")

    def test_blocks_attribute_access(self, evaluator):
        with pytest.raises(ValueError):
            evaluator.evaluate("().__class__.__bases__")

    def test_blocks_assignment(self, evaluator):
        with pytest.raises(ValueError):
            evaluator.evaluate("x = 5")

    def test_blocks_lambda(self, evaluator):
        with pytest.raises(ValueError):
            evaluator.evaluate("lambda x: x")


class TestCapabilitySystem:
    def test_has_capability(self):
        token = CapabilityToken(granted=Capability.FILESYSTEM_READ | Capability.NETWORK_OUTBOUND)
        assert token.has(Capability.FILESYSTEM_READ)
        assert token.has(Capability.NETWORK_OUTBOUND)
        assert token.has(Capability.FILESYSTEM_READ | Capability.NETWORK_OUTBOUND)
        assert not token.has(Capability.FILESYSTEM_WRITE)
        assert not token.has(Capability.PROCESS_SANDBOX)

    def test_require_raises(self):
        token = CapabilityToken(granted=Capability.FILESYSTEM_READ)
        token.require(Capability.FILESYSTEM_READ)  # OK
        with pytest.raises(PermissionError):
            token.require(Capability.FILESYSTEM_WRITE)

    def test_read_only_group(self):
        token = CapabilityToken(granted=Capability.READ_ONLY)
        assert token.has(Capability.FILESYSTEM_READ)
        assert token.has(Capability.NETWORK_OUTBOUND)
        assert not token.has(Capability.FILESYSTEM_WRITE)

    def test_default_no_escalation(self):
        token = CapabilityToken(granted=Capability.FILESYSTEM_READ)
        # No escalation callback → auto-deny
        import asyncio
        result = asyncio.run(token.request_escalation(Capability.FILESYSTEM_WRITE, "Need to write"))
        assert result is False
