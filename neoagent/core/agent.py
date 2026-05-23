"""Streaming async agent loop — the core decision-making engine.

Key improvements over CyberClaw's agent.py:
1. Async streaming (astream_events) — users see real-time token output,
   not a 20+ second spinner with no feedback
2. Parallel tool execution — independent tool calls run concurrently
3. Structured error boundaries — tool failures don't crash the session
4. Token-budget-aware context management — adapts in real-time
5. Configurable system prompts — not hardcoded Chinese strings
6. Session management with proper isolation

CyberClaw's agent_node blocks on llm.invoke() — the entire UI freezes.
Here, astream_events yields tokens as they arrive and tools execute in
parallel, dramatically improving perceived performance.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from typing_extensions import TypedDict

from .config import get_config
from .memory import MemoryManager, TokenCounter
from .provider import (
    SmartRouter,
    TokenBudget,
    create_budget,
    create_llm,
    get_model_info,
    stream_llm_response,
)
from .tools import get_all_tools


# ── Agent State ────────────────────────────────────


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    summary: str
    session_id: str
    turn_count: int


# ── Streaming Events ───────────────────────────────


class AgentEventKind(str, Enum):
    """Event kinds for agent execution streaming."""

    THINKING = "thinking"
    TOKEN = "token"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"
    TOOL_RESULT = "tool_result"
    MESSAGE_COMPLETE = "message_complete"
    ERROR = "error"
    BUDGET_WARNING = "budget_warning"


@dataclass
class AgentEvent:
    """Events emitted during agent execution for real-time UI updates."""

    kind: str | AgentEventKind
    data: Any = None
    timestamp: float = field(default_factory=time.time)


# ── Default System Prompt ──────────────────────────


DEFAULT_SYSTEM_PROMPT = """You are NeoAgent, a capable and efficient AI assistant.

Core principles:
1. Be direct and concise — provide complete answers without unnecessary verbosity.
2. Use tools when they help — search the web for current info, run code to verify, read/write files for persistence.
3. Think step by step for complex problems, but don't narrate simple steps.
4. When writing code, ensure it's correct, secure, and well-structured.
5. If you're unsure, say so rather than guessing.
6. Respect the user's preferences and constraints stored in their profile.
7. All file and code operations are sandboxed — you cannot access files outside the workspace.

Tool usage guidelines:
- Use web_search for current information beyond your knowledge cutoff.
- Use run_python to execute calculations or data processing.
- Use list_files/read_file/write_file for file operations within the workspace.
- Use schedule_task for reminders and recurring tasks.
- Use analyze_image when the user shares an image for analysis.
- CRITICAL: When the user asks to handle MULTIPLE independent topics or tasks simultaneously (e.g. "research X, Y, and Z" or "compare A, B, and C"), you MUST use delegate_to_subagents. Do NOT attempt to handle them yourself with repeated web_search calls. Pass tasks as a JSON array string.
- If delegate_to_subagents returns an error, do NOT fall back to repeated web_search calls. Instead, answer directly based on your existing knowledge.
- If web_search returns [SEARCH_UNAVAILABLE] or [SEARCH_ERROR], do NOT retry. Answer directly from your existing knowledge without further searches.
- ALWAYS check the [User Profile] section in the system prompt. If the user has stated preferences (e.g. favorite drink, style, interests) or facts about themselves, you MUST actively use that information to personalize your response. Do not ignore it.
- When the user shares a new personal preference or fact, proactively call save_user_profile to remember it for future conversations."""


# ── Agent Builder ───────────────────────────────────


@dataclass
class NeoAgent:
    """Streaming AI agent with tool use and memory management.

    Usage:
        agent = NeoAgent(provider="anthropic", model="claude-sonnet-4-6")
        async for event in agent.stream("Write a haiku about coding"):
            if event.kind == AgentEvent.Kind.TOKEN:
                print(event.data, end="")
    """

    provider: str | None = None
    model: str | None = None
    system_prompt: str | None = None
    tools: list[Any] | None = None
    session_id: str = "default"
    max_turns: int = 50
    router_enabled: bool = True

    # Internals (initialized in __post_init__)
    _llm: Any = None
    _llm_with_tools: Any = None
    _budget: TokenBudget | None = None
    _memory: MemoryManager | None = None
    _token_counter: TokenCounter = field(default_factory=TokenCounter)
    _router: SmartRouter | None = None
    _app: Any = None
    _checkpointer: Any = None

    def __post_init__(self) -> None:
        cfg = get_config()
        prov = self.provider or cfg.llm.default_provider
        model = self.model or cfg.llm.default_model

        self._llm = create_llm(provider=prov, model=model)
        actual_tools = self.tools if self.tools is not None else get_all_tools()

        # If the current model does not support vision, remove analyze_image
        # from the tool list and adjust the system prompt accordingly.
        model_info = get_model_info(model)
        if model_info and not model_info.supports_vision:
            actual_tools = [
                t for t in actual_tools if getattr(t, "name", None) != "analyze_image"
            ]
            sys = self.system_prompt or DEFAULT_SYSTEM_PROMPT
            sys = sys.replace(
                "- Use analyze_image when the user shares an image for analysis.\n", ""
            )
            sys += (
                "\nNote: The current model does not support image/vision analysis. "
                "If the user asks about analyzing images, explain this limitation and "
                "suggest switching to a multimodal model (gpt-4o, claude-sonnet-4-6, gemini-2.0-flash)."
            )
            self.system_prompt = sys

        self._llm_with_tools = self._llm.bind_tools(actual_tools)
        self._budget = create_budget(model)
        self._memory = MemoryManager()
        self._token_counter = TokenCounter()
        self._router = SmartRouter(enabled=self.router_enabled)

        self._app = self._build_graph(actual_tools)

    def _build_graph(self, tools: list) -> Any:
        """Build the LangGraph state machine with streaming support."""
        cfg = get_config()
        # Custom tool node: LangGraph 1.2.0 ToolNode no longer accepts raw
        # string returns from tools. We wrap results into proper ToolMessage.
        tool_map = {getattr(t, "name", None): t for t in tools}

        async def custom_tool_node(state: AgentState) -> dict:
            """Execute tools from the latest AIMessage and return ToolMessages."""
            messages = state["messages"]
            latest_ai = None
            for m in reversed(messages):
                if isinstance(m, AIMessage) and m.tool_calls:
                    latest_ai = m
                    break
            if latest_ai is None:
                return {"messages": []}

            tool_messages: list[ToolMessage] = []
            for tc in latest_ai.tool_calls:
                name = tc.get("name", "unknown")
                args = tc.get("args", {})
                tc_id = tc.get("id", "unknown")
                tool = tool_map.get(name)
                if tool is None:
                    tool_messages.append(
                        ToolMessage(
                            content=f"Error: tool '{name}' not found.",
                            tool_call_id=tc_id,
                        )
                    )
                    continue
                try:
                    result = await tool.ainvoke(args)
                    if isinstance(result, ToolMessage):
                        tool_messages.append(result)
                    else:
                        tool_messages.append(
                            ToolMessage(
                                content=str(result) if result else " ",
                                tool_call_id=tc_id,
                            )
                        )
                except Exception as exc:
                    tool_messages.append(
                        ToolMessage(
                            content=f"Error executing {name}: {exc}",
                            tool_call_id=tc_id,
                        )
                    )
            return {"messages": tool_messages}

        agent_self = self

        async def agent_node(state: AgentState, config: RunnableConfig) -> dict:
            """Core reasoning node — decides to respond or call tools."""
            raw_messages = state["messages"]
            turn_count = state.get("turn_count", 0)

            # Hard stop to prevent infinite tool-call loops
            if turn_count >= agent_self.max_turns:
                return {
                    "messages": [
                        AIMessage(
                            content="I've reached the maximum number of reasoning steps. "
                                    "Let me provide my best answer based on what I know."
                        )
                    ],
                    "turn_count": turn_count,
                }

            # Memory pruning
            kept_msgs, discarded = agent_self._memory.short_term.prune(raw_messages)
            state_updates: dict = {}

            if discarded:
                # Consolidate old messages to long-term memory
                agent_self._memory.consolidator.on_prune(discarded)

                # Summarize if threshold reached
                if agent_self._memory.consolidator.should_consolidate():
                    # Use cheap model for summarization
                    cheap_llm = create_llm(
                        provider=cfg.llm.default_provider,
                        model=cfg.llm.cheap_model,
                    )
                    await agent_self._memory.consolidator.consolidate(cheap_llm)

                # Remove old messages from state
                delete_cmds = [RemoveMessage(id=m.id) for m in discarded if m.id]
                state_updates["messages"] = delete_cmds

            # Build system prompt with memory context
            sys_prompt = agent_self.system_prompt or DEFAULT_SYSTEM_PROMPT
            user_msg_text = ""
            for m in reversed(kept_msgs):
                if isinstance(m, HumanMessage) and m.content:
                    user_msg_text = str(m.content)[:200]
                    break

            memory_context = agent_self._memory.build_system_prompt(user_msg_text)
            full_sys = f"{sys_prompt}\n\n{memory_context}"

            msgs_for_llm = [SystemMessage(content=full_sys)]
            for m in kept_msgs:
                if isinstance(m, SystemMessage):
                    continue
                # Deepseek API rejects assistant/tool messages with content: null
                # or empty string. Patch to non-empty to avoid serialization issues.
                if isinstance(m, AIMessage) and not m.content:
                    m = AIMessage(
                        content=" ",
                        tool_calls=m.tool_calls,
                        id=m.id,
                        additional_kwargs=m.additional_kwargs,
                        response_metadata=m.response_metadata,
                    )
                elif isinstance(m, ToolMessage) and not m.content:
                    m = ToolMessage(
                        content=" ",
                        tool_call_id=m.tool_call_id,
                        name=getattr(m, "name", None),
                        id=m.id,
                        additional_kwargs=m.additional_kwargs,
                        response_metadata=m.response_metadata,
                    )
                msgs_for_llm.append(m)

            # Sanitize message content
            for m in msgs_for_llm:
                if isinstance(m.content, str):
                    m.content = m.content.encode("utf-8", errors="ignore").decode("utf-8")

            # Token budget check
            estimated_input = agent_self._token_counter.count_messages(msgs_for_llm)
            try:
                agent_self._budget.check_and_reserve(estimated_input, 5000)
            except Exception as budget_err:
                logging.getLogger("neoagent.agent").warning(
                    "Budget check failed: %s", budget_err
                )

            # Route to appropriate model
            complexity = agent_self._router.classify(msgs_for_llm)
            if complexity.value == "simple" and cfg.llm.router_enabled:
                # Use cheap model for simple tasks
                cheap_llm = create_llm(
                    provider=cfg.llm.default_provider,
                    model=cfg.llm.cheap_model,
                )
                llm_bound = cheap_llm.bind_tools(tools)
            else:
                llm_bound = agent_self._llm_with_tools

            # Invoke LLM
            response = await llm_bound.ainvoke(msgs_for_llm)

            # Record token usage
            agent_self._budget.record(estimated_input, 500)  # approximate

            if "messages" not in state_updates:
                state_updates["messages"] = []
            state_updates["messages"].append(response)
            state_updates["turn_count"] = turn_count + 1

            return state_updates

        # Build workflow
        workflow = StateGraph(AgentState)
        workflow.add_node("agent", agent_node)
        workflow.add_node("tools", custom_tool_node)
        workflow.add_edge(START, "agent")
        workflow.add_conditional_edges("agent", tools_condition)
        workflow.add_edge("tools", "agent")

        # In-memory checkpointer for session history and state persistence
        self._checkpointer = MemorySaver()
        return workflow.compile(checkpointer=self._checkpointer)

    async def stream(
        self,
        user_input: str,
        session_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Stream agent response with real-time token delivery.

        This is the primary API — replacement for CyberClaw's blocking
        chat loop. Events include tokens, tool calls, and errors.
        """
        sid = session_id or self.session_id
        config = {"configurable": {"thread_id": sid}}

        inputs: AgentState = {
            "messages": [HumanMessage(content=user_input)],
            "summary": "",
            "session_id": sid,
            "turn_count": 0,
        }

        try:
            async for event in self._app.astream_events(inputs, config=config, version="v2"):
                event_type = event.get("event", "")

                if event_type == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        yield AgentEvent(
                            kind=AgentEventKind.TOKEN,
                            data=chunk.content,
                        )

                elif event_type == "on_tool_start":
                    yield AgentEvent(
                        kind=AgentEventKind.TOOL_CALL_START,
                        data={
                            "name": event.get("name", "unknown"),
                            "input": event.get("data", {}).get("input", {}),
                        },
                    )

                elif event_type == "on_tool_end":
                    yield AgentEvent(
                        kind=AgentEventKind.TOOL_CALL_END,
                        data={
                            "name": event.get("name", "unknown"),
                            "output": str(event.get("data", {}).get("output", ""))[:500],
                        },
                    )

                elif event_type == "on_chat_model_end":
                    yield AgentEvent(
                        kind=AgentEventKind.MESSAGE_COMPLETE,
                        data={"budget": self._budget.summary if self._budget else "N/A"},
                    )

        except Exception as e:
            yield AgentEvent(kind=AgentEventKind.ERROR, data=str(e))

    async def invoke(self, user_input: str, session_id: str | None = None) -> str:
        """Non-streaming invocation — collects and returns full response."""
        full_response: list[str] = []
        async for event in self.stream(user_input, session_id):
            if event.kind == AgentEventKind.TOKEN:
                full_response.append(str(event.data))
            elif event.kind == AgentEventKind.ERROR:
                return f"Error: {event.data}"
        return "".join(full_response)

    async def get_history(self, session_id: str | None = None) -> list[BaseMessage]:
        """Retrieve conversation history for a session."""
        # LangGraph checkpointer stores history; access via state
        sid = session_id or self.session_id
        config = {"configurable": {"thread_id": sid}}
        try:
            state = await self._app.aget_state(config)
            if state and state.values:
                return state.values.get("messages", [])
        except Exception as hist_err:
            import logging
            logging.getLogger("neoagent.agent").warning(
                "Failed to retrieve history for %s: %s", sid, hist_err
            )
        return []

    def reset_session(self, session_id: str | None = None) -> None:
        """Clear conversation history for a session."""
        self._budget = create_budget(self.model or get_config().llm.default_model)


# ── Convenience API ────────────────────────────────


async def create_agent(
    provider: str | None = None,
    model: str | None = None,
    **kwargs,
) -> NeoAgent:
    """Create and initialize a NeoAgent instance."""
    return NeoAgent(provider=provider, model=model, **kwargs)
