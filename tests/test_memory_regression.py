"""Regression tests for memory pruning bugs."""

import pytest
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from neoagent.core.memory import ShortTermMemory


class TestShortTermMemoryRegression:
    def test_prune_all_turns_when_everything_exceeds_limits(self):
        """When all turns exceed max_turns, everything except system msgs should be discarded."""
        mem = ShortTermMemory(max_turns=1, max_tokens=10)
        msgs = [
            SystemMessage(content="sys"),
            HumanMessage(content="Question 1"),
            AIMessage(content="Answer 1 with a very long text that exceeds token limit easily"),
            HumanMessage(content="Question 2"),
            AIMessage(content="Answer 2 also very long and definitely over the token limit"),
        ]
        kept, discarded = mem.prune(msgs)
        # System message must be preserved
        assert any(isinstance(m, SystemMessage) for m in kept)
        # All non-system messages should be discarded because even the most recent turn
        # exceeds the tiny token budget.
        non_system_kept = [m for m in kept if not isinstance(m, SystemMessage)]
        non_system_discarded = [m for m in discarded if not isinstance(m, SystemMessage)]
        assert len(non_system_kept) == 0
        assert len(non_system_discarded) == 4

    def test_prune_keeps_only_recent_turns(self):
        mem = ShortTermMemory(max_turns=2, max_tokens=100_000)
        msgs = []
        for i in range(6):
            msgs.append(HumanMessage(content=f"Q{i}"))
            msgs.append(AIMessage(content=f"A{i}"))
        kept, discarded = mem.prune(msgs)
        # 6 turns total, keep last 2 -> discard first 4 turns (8 messages)
        assert len(discarded) == 8
        assert len(kept) == 4  # system msgs (0) + last 2 turns (4 msgs)
