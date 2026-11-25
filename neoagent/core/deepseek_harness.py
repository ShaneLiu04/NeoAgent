"""DeepSeek-Style Reasoning-Action Agent Harness Primitives.

Inspired by open-source DeepSeek Agent Harness & R1/V3.2 tool-use best practices:
1. **Stateful `<think>` + `reasoning_content` Stream Splitter (`ThinkStreamParser`)**:
   Cleanly separates live Chain-of-Thought tokens from final answer tokens even when
   `<think>` or `</think>` tags split across chunk boundaries.
2. **Inter-Turn Reasoning Context Folding (`fold_completed_turn_reasoning`)**:
   Preserves `reasoning_content` within the active ReAct tool loop (required by
   DeepSeek-R1/V3.2 APIs during multi-step tool calls), while stripping verbose
   `<think>...</think>` blocks and stale `reasoning_content` from *prior* completed
   user turns — saving 60-80% of context budget (Harness L4 40% Smart Zone).
3. **Cognitive Role Router (`HarnessRoleRouter`)**:
   Routes high-depth reasoning roles (`planner`, `architect`, `reviewer`, `debugger`)
   to the Reasoner model tier, and high-throughput execution roles (`executor`,
   `distiller`, `summarizer`) to the fast Chat model tier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ._compat import AIMessage, BaseMessage, HumanMessage

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_OPEN_TAG = "<think>"
_CLOSE_TAG = "</think>"


@dataclass
class StreamDelta:
    """Separated reasoning vs visible answer text for a single streaming chunk."""

    reasoning: str = ""
    answer: str = ""


class ThinkStreamParser:
    """Incremental chunk parser handling `<think>...</think>` across arbitrary chunk splits."""

    def __init__(self) -> None:
        self._in_think: bool = False
        self._buf: str = ""

    def feed(self, text_chunk: str = "", explicit_reasoning: str = "") -> StreamDelta:
        reasoning_parts: list[str] = []
        answer_parts: list[str] = []

        if explicit_reasoning:
            reasoning_parts.append(explicit_reasoning)

        if not text_chunk:
            return StreamDelta("".join(reasoning_parts), "")

        self._buf += text_chunk
        while self._buf:
            lower_buf = self._buf.lower()
            if not self._in_think:
                idx = lower_buf.find(_OPEN_TAG)
                if idx == -1:
                    # Guard partial prefix of `<think>` at end of buffer
                    partial = self._trailing_tag_prefix(lower_buf, _OPEN_TAG)
                    emit_len = len(self._buf) - partial
                    if emit_len > 0:
                        answer_parts.append(self._buf[:emit_len])
                        self._buf = self._buf[emit_len:]
                    break
                if idx > 0:
                    answer_parts.append(self._buf[:idx])
                self._buf = self._buf[idx + len(_OPEN_TAG) :]
                self._in_think = True
            else:
                idx = lower_buf.find(_CLOSE_TAG)
                if idx == -1:
                    partial = self._trailing_tag_prefix(lower_buf, _CLOSE_TAG)
                    emit_len = len(self._buf) - partial
                    if emit_len > 0:
                        reasoning_parts.append(self._buf[:emit_len])
                        self._buf = self._buf[emit_len:]
                    break
                if idx > 0:
                    reasoning_parts.append(self._buf[:idx])
                self._buf = self._buf[idx + len(_CLOSE_TAG) :].lstrip("\r\n")
                self._in_think = False

        return StreamDelta("".join(reasoning_parts), "".join(answer_parts))

    def flush(self) -> StreamDelta:
        if not self._buf:
            return StreamDelta()
        leftover = self._buf
        self._buf = ""
        if self._in_think:
            self._in_think = False
            return StreamDelta(reasoning=leftover)
        return StreamDelta(answer=leftover)

    @staticmethod
    def _trailing_tag_prefix(lower_text: str, tag: str) -> int:
        max_k = min(len(lower_text), len(tag) - 1)
        for k in range(max_k, 0, -1):
            if lower_text.endswith(tag[:k]):
                return k
        return 0


def strip_think_tags(text: str) -> str:
    """Remove completed `<think>...</think>` blocks from text."""
    if not text or "<think>" not in text.lower():
        return text
    return _THINK_BLOCK_RE.sub("", text).strip()


def fold_completed_turn_reasoning(messages: list[BaseMessage]) -> list[BaseMessage]:
    """DeepSeek Agent Harness Context Folding:
    - Find the index of the *latest* `HumanMessage`.
    - For all `AIMessage`s *before* that latest `HumanMessage` (completed historical turns),
      strip `<think>...</think>` from `content` and drop `reasoning_content` from
      `additional_kwargs` so historical CoT traces do not bloat context.
    - For `AIMessage`s *after* the latest `HumanMessage` (active multi-step tool loop),
      keep `reasoning_content` intact as required by DeepSeek-R1/V3.2 tool-call APIs.
    """
    last_human_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            last_human_idx = i
            break

    if last_human_idx <= 0:
        return list(messages)

    folded: list[BaseMessage] = []
    for idx, msg in enumerate(messages):
        if idx < last_human_idx and isinstance(msg, AIMessage):
            raw_content = msg.content if isinstance(msg.content, str) else str(msg.content or "")
            clean_content = strip_think_tags(raw_content) or " "
            clean_kwargs = {
                k: v
                for k, v in (getattr(msg, "additional_kwargs", None) or {}).items()
                if k != "reasoning_content"
            }
            folded.append(
                AIMessage(
                    content=clean_content,
                    tool_calls=getattr(msg, "tool_calls", []),
                    id=getattr(msg, "id", None),
                    additional_kwargs=clean_kwargs,
                    response_metadata=getattr(msg, "response_metadata", {}),
                )
            )
        else:
            folded.append(msg)
    return folded


REASONER_ROLES = frozenset({"planner", "architect", "reviewer", "debugger", "security_auditor"})


class HarnessRoleRouter:
    """Selects optimal model tier (`powerful_model` vs `cheap_model`) for a Harness role."""

    @staticmethod
    def select_model(role: str, powerful_model: str, cheap_model: str) -> str:
        if role.lower().strip() in REASONER_ROLES:
            return powerful_model
        return cheap_model


def extract_chunk_reasoning(chunk: Any) -> str:
    """Extract `reasoning_content` from LangChain/OpenAI-compatible streaming chunk."""
    if chunk is None:
        return ""
    kwargs = getattr(chunk, "additional_kwargs", None) or {}
    if isinstance(kwargs, dict) and kwargs.get("reasoning_content"):
        return str(kwargs["reasoning_content"])
    return ""
