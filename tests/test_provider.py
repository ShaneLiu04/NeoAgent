"""Tests for provider module — model routing, budget tracking."""
import pytest
from neoagent.core.provider import (
    BudgetExceededError,
    ModelInfo,
    ProviderKind,
    SmartRouter,
    TaskComplexity,
    TokenBudget,
    get_model_info,
)


class TestSmartRouter:
    @pytest.fixture
    def router(self):
        return SmartRouter(enabled=True)

    def test_simple_greeting(self, router):
        from langchain_core.messages import HumanMessage
        msgs = [HumanMessage(content="hello how are you")]
        assert router.classify(msgs) == TaskComplexity.SIMPLE

    def test_complex_code_gen(self, router):
        from langchain_core.messages import HumanMessage
        msgs = [HumanMessage(content="implement a red-black tree in Python")]
        assert router.classify(msgs) == TaskComplexity.COMPLEX

    def test_moderate_default(self, router):
        from langchain_core.messages import HumanMessage
        msgs = [HumanMessage(content="explain how garbage collection works")]
        assert router.classify(msgs) == TaskComplexity.MODERATE

    def test_disabled_router(self):
        router = SmartRouter(enabled=False)
        from langchain_core.messages import HumanMessage
        msgs = [HumanMessage(content="hello")]
        assert router.classify(msgs) == TaskComplexity.COMPLEX  # always complex when disabled


class TestTokenBudget:
    def test_within_budget(self):
        info = ModelInfo("test", ProviderKind.OPENAI, 4096, 0.001, 0.005)
        budget = TokenBudget(
            max_input_tokens=10000,
            max_output_tokens=5000,
            max_cost_usd=1.0,
            model_info=info,
        )
        budget.check_and_reserve(500, 200)  # should not raise

    def test_exceed_input(self):
        info = ModelInfo("test", ProviderKind.OPENAI, 4096, 0.001, 0.005)
        budget = TokenBudget(
            max_input_tokens=100,
            max_output_tokens=5000,
            max_cost_usd=100.0,
            model_info=info,
        )
        with pytest.raises(BudgetExceededError):
            budget.check_and_reserve(200, 50)

    def test_exceed_cost(self):
        info = ModelInfo("test", ProviderKind.OPENAI, 4096, 1.0, 1.0)  # $1/1k tokens — expensive
        budget = TokenBudget(
            max_input_tokens=100000,
            max_output_tokens=100000,
            max_cost_usd=0.01,
            model_info=info,
        )
        with pytest.raises(BudgetExceededError):
            budget.check_and_reserve(100, 100)

    def test_record_and_summary(self):
        info = ModelInfo("test", ProviderKind.OPENAI, 4096, 0.001, 0.005)
        budget = TokenBudget(100000, 50000, 100.0, info)
        budget.record(1000, 500)
        assert budget.total_input_tokens == 1000
        assert budget.total_output_tokens == 500
        assert budget.total_cost > 0


class TestModelCatalog:
    def test_get_model_info(self):
        info = get_model_info("gpt-4o-mini")
        assert info is not None
        assert info.provider == ProviderKind.OPENAI
        assert info.input_cost_per_1k > 0

    def test_unknown_model(self):
        info = get_model_info("nonexistent-model-123")
        assert info is None

    def test_estimate_cost(self):
        info = ModelInfo("test", ProviderKind.OPENAI, 4096, 0.01, 0.03)
        cost = info.estimate_cost(1000, 1000)
        assert cost == pytest.approx(0.04, rel=1e-6)
