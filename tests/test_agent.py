"""Tests for agent module — AgentEvent, NeoAgent init, checkpointer."""

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from neoagent.core.agent import AgentEvent, AgentEventKind, NeoAgent, create_agent


class TestAgentEventKind:
    def test_enum_values(self):
        assert AgentEventKind.TOKEN == "token"
        assert AgentEventKind.TOOL_CALL_START == "tool_call_start"
        assert AgentEventKind.ERROR == "error"

    def test_event_creation(self):
        evt = AgentEvent(kind=AgentEventKind.TOKEN, data="hello")
        assert evt.kind == "token"
        assert evt.data == "hello"
        assert isinstance(evt.timestamp, float)


class TestNeoAgentInit:
    @patch("neoagent.core.agent.create_llm", return_value=MagicMock())
    def test_default_init(self, mock_create_llm):
        agent = NeoAgent(session_id="test-session")
        assert agent.session_id == "test-session"
        assert agent.max_turns == 50
        assert agent._app is not None

    @patch("neoagent.core.agent.create_llm", return_value=MagicMock())
    def test_reset_session_creates_new_budget(self, mock_create_llm):
        agent = NeoAgent(session_id="s1")
        old_budget = agent._budget
        agent.reset_session()
        assert agent._budget is not old_budget


class TestCreateAgent:
    @pytest.mark.asyncio
    @patch("neoagent.core.agent.create_llm", return_value=MagicMock())
    async def test_create_agent_factory(self, mock_create_llm):
        agent = await create_agent(provider="openai", model="gpt-4o-mini")
        assert isinstance(agent, NeoAgent)
        assert agent.provider == "openai"
        assert agent.model == "gpt-4o-mini"
