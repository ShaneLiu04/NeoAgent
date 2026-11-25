"""Streaming multi-provider LLM adapter with smart routing and token budgeting.

Key improvements over CyberClaw's provider.py:
- Async-first with streaming iterator support for all providers
- SmartRouter: routes simple tasks to cheap models, complex to powerful ones
- TokenBudget: real-time tracking, hard cap enforcement
- RetryWithBackoff: exponential backoff with jitter on rate limits
- ModelRegistry: typed model metadata for capability-aware selection
"""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from typing import Any

try:
    from langchain_core.language_models.chat_models import BaseChatModel
except ImportError:  # pragma: no cover - minimal CI fallback
    BaseChatModel = Any  # type: ignore[assignment,misc]

from ._compat import AIMessage, BaseMessage

# Monkey-patch langchain-openai to preserve Deepseek reasoning_content
# (deepseek-v4-pro / deepseek-reasoner return reasoning_content in assistant
# messages; the API requires it to be passed back verbatim.)
try:
    import langchain_openai.chat_models.base as _lc_openai_base
    _orig_convert_dict = _lc_openai_base._convert_dict_to_message

    def _deepseek_convert_dict_to_message(_dict):
        msg = _orig_convert_dict(_dict)
        if isinstance(msg, AIMessage) and "reasoning_content" in _dict:
            msg.additional_kwargs["reasoning_content"] = _dict["reasoning_content"]
        return msg

    _lc_openai_base._convert_dict_to_message = _deepseek_convert_dict_to_message
except Exception:
    pass  # If langchain-openai internals change, fail silently

from .config import get_config

# ── Data Models ────────────────────────────────────


class TaskComplexity(Enum):
    SIMPLE = "simple"       # greetings, factual lookup, simple calculation
    MODERATE = "moderate"   # explanation, summarization, translation
    COMPLEX = "complex"     # multi-step reasoning, code generation, analysis


class ProviderKind(Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"
    DEEPSEEK = "deepseek"
    GROQ = "groq"
    GOOGLE = "google"
    ALIYUN = "aliyun"
    TENCENT = "tencent"


@dataclass
class ModelInfo:
    """Typed metadata for a model — enables capability-aware routing."""
    name: str
    provider: ProviderKind
    max_tokens: int
    input_cost_per_1k: float  # USD
    output_cost_per_1k: float
    supports_vision: bool = False
    supports_tools: bool = True
    context_window: int = 128_000

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens / 1000 * self.input_cost_per_1k +
                output_tokens / 1000 * self.output_cost_per_1k)


# Known model catalog — easy to extend.
MODEL_CATALOG: dict[str, ModelInfo] = {
    "gpt-4o-mini": ModelInfo("gpt-4o-mini", ProviderKind.OPENAI, 16384, 0.00015, 0.0006),
    "gpt-4o": ModelInfo("gpt-4o", ProviderKind.OPENAI, 16384, 0.0025, 0.01, supports_vision=True),
    "claude-haiku-4-5-20251001": ModelInfo("claude-haiku-4-5-20251001", ProviderKind.ANTHROPIC, 8192, 0.001, 0.005),
    "claude-sonnet-4-6": ModelInfo("claude-sonnet-4-6", ProviderKind.ANTHROPIC, 8192, 0.003, 0.015, supports_vision=True),
    "claude-opus-4-7": ModelInfo("claude-opus-4-7", ProviderKind.ANTHROPIC, 8192, 0.015, 0.075, supports_vision=True),
    "deepseek-chat": ModelInfo("deepseek-chat", ProviderKind.DEEPSEEK, 8192, 0.00014, 0.00028),
    "deepseek-v4-pro": ModelInfo("deepseek-v4-pro", ProviderKind.DEEPSEEK, 16384, 0.0005, 0.002, supports_vision=False),
    "deepseek-reasoner": ModelInfo("deepseek-reasoner", ProviderKind.DEEPSEEK, 8192, 0.00055, 0.00219),
    "gemini-2.0-flash": ModelInfo("gemini-2.0-flash", ProviderKind.GOOGLE, 8192, 0.0001, 0.0004, supports_vision=True, context_window=1_000_000),
    "gemini-2.5-pro": ModelInfo("gemini-2.5-pro", ProviderKind.GOOGLE, 8192, 0.00125, 0.01, supports_vision=True, context_window=1_000_000),
    "llama-4-maverick": ModelInfo("llama-4-maverick", ProviderKind.GROQ, 8192, 0.0002, 0.0006),
}


# ── Token Budget ───────────────────────────────────


class BudgetExceededError(Exception):
    """Raised when a session exceeds its token or cost budget."""


@dataclass
class TokenBudget:
    """Per-session token and cost tracking with hard enforcement."""
    max_input_tokens: int
    max_output_tokens: int
    max_cost_usd: float
    model_info: ModelInfo

    total_input_tokens: int = 0
    total_output_tokens: int = 0

    def check_and_reserve(self, estimated_input: int, estimated_output: int) -> None:
        """Raise BudgetExceededError if budget would be exceeded."""
        if self.total_input_tokens + estimated_input > self.max_input_tokens:
            raise BudgetExceededError(
                f"Input token budget exhausted: {self.total_input_tokens}/{self.max_input_tokens}"
            )
        if self.total_output_tokens + estimated_output > self.max_output_tokens:
            raise BudgetExceededError(
                f"Output token budget exhausted: {self.total_output_tokens}/{self.max_output_tokens}"
            )
        est_cost = self.model_info.estimate_cost(
            self.total_input_tokens + estimated_input,
            self.total_output_tokens + estimated_output,
        )
        if self.max_cost_usd > 0 and est_cost > self.max_cost_usd:
            raise BudgetExceededError(
                f"Cost budget would be exceeded: ${est_cost:.4f} > ${self.max_cost_usd:.2f}"
            )

    def record(self, input_tokens: int, output_tokens: int) -> None:
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens

    @property
    def total_cost(self) -> float:
        return self.model_info.estimate_cost(self.total_input_tokens, self.total_output_tokens)

    @property
    def summary(self) -> str:
        return (
            f"Tokens: {self.total_input_tokens} in / {self.total_output_tokens} out | "
            f"Cost: ${self.total_cost:.4f}"
        )


# ── Smart Router ───────────────────────────────────


class SmartRouter:
    """Routes requests between cheap and powerful models based on complexity.

    The key insight: use cheap models for 80% of interactions (greetings,
    simple lookups), reserve expensive models for the 20% that need them
    (multi-step reasoning, complex code generation).

    This directly addresses one of CyberClaw's biggest operational issues:
    no cost controls — every request hits the main model at full price.
    """

    COMPLEXITY_INDICATORS: dict[TaskComplexity, list[str]] = {
        TaskComplexity.SIMPLE: [
            "hello", "hi", "thanks", "what time", "what day",
            "how are you", "good morning", "goodbye", "ok", "yes", "no",
        ],
        TaskComplexity.COMPLEX: [
            "implement", "debug", "refactor", "analyze", "explain in detail",
            "write a", "create a", "design", "architecture", "review this code",
            "fix the bug", "optimize", "multi-step", "step by step",
        ],
    }

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def classify(self, messages: list[BaseMessage]) -> TaskComplexity:
        """Classify task complexity from message content."""
        if not self.enabled:
            return TaskComplexity.COMPLEX

        combined = " ".join(
            str(m.content).lower() for m in messages if hasattr(m, "content") and m.content
        )

        # Check complex indicators first
        for indicator in self.COMPLEXITY_INDICATORS[TaskComplexity.COMPLEX]:
            if indicator in combined:
                return TaskComplexity.COMPLEX

        # Check simple indicators
        for indicator in self.COMPLEXITY_INDICATORS[TaskComplexity.SIMPLE]:
            if indicator in combined:
                return TaskComplexity.SIMPLE

        # Default: moderate
        return TaskComplexity.MODERATE


# ── Retry Logic ────────────────────────────────────


async def retry_with_backoff(
    fn,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
):
    """Exponential backoff with jitter for rate-limit resilience."""
    last_exception: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except Exception as e:
            last_exception = e
            if attempt == max_retries:
                raise

            # Only retry on rate limits and transient errors
            error_str = str(e).lower()
            is_retryable = any(
                keyword in error_str
                for keyword in ("rate limit", "too many requests", "429", "503", "timeout", "connection")
            )
            if not is_retryable:
                raise

            delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1), max_delay)
            await asyncio.sleep(delay)

    raise last_exception  # type: ignore[misc]


# ── Provider Factory ───────────────────────────────


def create_llm(
    provider: str,
    model: str,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    base_url: str | None = None,
    api_key: str | None = None,
    **kwargs,
) -> BaseChatModel:
    """Factory for creating LLM instances — async-compatible across all providers.

    Fixes CyberClaw issue: all providers unified under a single factory,
    with streaming enabled by default for every provider that supports it.
    """
    provider = provider.lower()
    cfg = get_config()

    if provider in ("openai", "aliyun", "dashscope", "deepseek", "tencent", "groq"):
        from langchain_openai import ChatOpenAI

        key = api_key or cfg.api_key_for(provider) or cfg.api_key_for("openai")
        if not key:
            raise ValueError(f"No API key found for provider '{provider}'")

        final_url = base_url
        if not final_url and provider in ("aliyun", "dashscope"):
            final_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        elif not final_url and provider == "deepseek":
            final_url = "https://api.deepseek.com/v1"
        elif not final_url and provider == "groq":
            final_url = "https://api.groq.com/openai/v1"

        # Provider-specific kwargs for OpenAI-compatible endpoints
        extra_kwargs = dict(kwargs)
        if provider == "deepseek":
            # Deepseek API does not support parallel_tool_calls
            extra_kwargs.setdefault("model_kwargs", {})
            extra_kwargs["model_kwargs"]["parallel_tool_calls"] = False

            # Deepseek reasoning models (deepseek-v4-pro, deepseek-reasoner) return
            # reasoning_content in assistant messages. The API requires it to be
            # passed back verbatim on subsequent requests. langchain-openai drops it.
            from langchain_core.messages import AIMessage

            class _DeepseekChatOpenAI(ChatOpenAI):
                def _get_request_payload(self, input_, *, stop=None, **kwargs):
                    payload = super()._get_request_payload(input_, stop=stop, **kwargs)
                    messages = self._convert_input(input_).to_messages()
                    if "messages" in payload:
                        for i, msg in enumerate(payload["messages"]):
                            if msg.get("role") == "assistant" and i < len(messages):
                                orig = messages[i]
                                if isinstance(orig, AIMessage) and "reasoning_content" in orig.additional_kwargs:
                                    msg["reasoning_content"] = orig.additional_kwargs["reasoning_content"]
                                # Deepseek thinking-mode models require the field to exist
                                # even when empty; OpenAI client drops it entirely.
                                if "reasoning_content" not in msg:
                                    msg["reasoning_content"] = ""
                    return payload

            return _DeepseekChatOpenAI(
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            api_key=key,
            base_url=final_url,
            streaming=True,
            **extra_kwargs,
        )

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        key = api_key or cfg.api_key_for("anthropic")
        if not key:
            raise ValueError("Missing ANTHROPIC_API_KEY")

        return ChatAnthropic(
            model_name=model,
            temperature=temperature,
            max_tokens=max_tokens,
            api_key=key,
            base_url=base_url or os.getenv("ANTHROPIC_BASE_URL"),
            **kwargs,
        )

    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        key = api_key or cfg.api_key_for("google")
        if not key:
            raise ValueError("Missing GOOGLE_API_KEY")

        return ChatGoogleGenerativeAI(
            model=model,
            temperature=temperature,
            max_output_tokens=max_tokens,
            google_api_key=key,
            **kwargs,
        )

    if provider == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=model,
            temperature=temperature,
            base_url=base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            **kwargs,
        )

    raise ValueError(f"Unsupported provider: {provider}")


# ── Streaming Wrapper ──────────────────────────────


async def stream_llm_response(
    llm: BaseChatModel,
    messages: list[BaseMessage],
    **kwargs,
) -> AsyncIterator[str]:
    """Stream LLM response token by token.

    This is the core improvement: CyberClaw blocks on llm.invoke() —
    users wait 20+ seconds with no feedback. Here, tokens stream in real-time,
    dramatically improving perceived performance.
    """
    async for chunk in llm.astream(messages, **kwargs):
        if hasattr(chunk, "content") and chunk.content:
            yield chunk.content


# ── Convenience Builder ────────────────────────────


def get_provider(
    provider_name: str | None = None,
    model_name: str | None = None,
    temperature: float | None = None,
    **kwargs,
) -> BaseChatModel:
    """One-liner: get a configured LLM from defaults or overrides.

    Usage:
        llm = get_provider()                          # from .env defaults
        llm = get_provider("anthropic", "claude-sonnet-4-6")  # explicit
    """
    cfg = get_config()
    return create_llm(
        provider=provider_name or cfg.llm.default_provider,
        model=model_name or cfg.llm.default_model,
        temperature=temperature if temperature is not None else cfg.llm.temperature,
        max_tokens=kwargs.pop("max_tokens", cfg.llm.max_tokens),
        **kwargs,
    )


def _infer_provider(model_name: str) -> ProviderKind:
    """Infer provider from model name for fallback."""
    name = model_name.lower()
    if "deepseek" in name:
        return ProviderKind.DEEPSEEK
    if "claude" in name or "haiku" in name or "sonnet" in name or "opus" in name:
        return ProviderKind.ANTHROPIC
    if "gemini" in name:
        return ProviderKind.GOOGLE
    if "gpt" in name or "o1" in name or "o3" in name:
        return ProviderKind.OPENAI
    if "llama" in name or "mixtral" in name:
        return ProviderKind.GROQ
    return ProviderKind.OPENAI


def get_model_info(model_name: str) -> ModelInfo | None:
    """Look up model metadata from the catalog."""
    return MODEL_CATALOG.get(model_name)


def create_budget(model_name: str) -> TokenBudget:
    """Create a token budget from config defaults."""
    cfg = get_config()
    info = get_model_info(model_name)
    if info is None:
        provider = _infer_provider(model_name)
        info = ModelInfo(model_name, provider, 16384, 0.001, 0.005)
    return TokenBudget(
        max_input_tokens=cfg.budget.max_input_tokens,
        max_output_tokens=cfg.budget.max_output_tokens,
        max_cost_usd=cfg.budget.max_cost_usd,
        model_info=info,
    )
