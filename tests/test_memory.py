"""Tests for memory system — profile, short-term, token counting."""
import pytest

from neoagent.core._compat import AIMessage, HumanMessage, SystemMessage
from neoagent.core.memory import (
    ShortTermMemory,
    TokenCounter,
    UserProfile,
)


class TestTokenCounter:
    @pytest.fixture
    def counter(self):
        return TokenCounter()

    def test_count_simple(self, counter):
        tokens = counter.count("Hello, world!")
        assert tokens > 0

    def test_count_empty(self, counter):
        assert counter.count("") == 0

    def test_count_message(self, counter):
        msg = HumanMessage(content="Hello")
        tokens = counter.count_message(msg)
        assert tokens > 0  # content tokens + 4 role overhead

    def test_count_messages(self, counter):
        msgs = [
            HumanMessage(content="Hi"),
            AIMessage(content="Hello!"),
        ]
        tokens = counter.count_messages(msgs)
        assert tokens > 0


class TestUserProfile:
    def test_default_profile(self):
        p = UserProfile()
        assert p.name == ""
        assert len(p.preferences) == 0
        assert len(p.facts) == 0

    def test_set_preference(self):
        p = UserProfile()
        p.set_preference("language", "python")
        assert p.get_preference("language") == "python"

    def test_update_preference(self):
        p = UserProfile()
        p.set_preference("theme", "dark")
        p.set_preference("theme", "light")
        assert len(p.preferences) == 1
        assert p.get_preference("theme") == "light"

    def test_add_fact(self):
        p = UserProfile()
        p.add_fact("Works at Acme Corp")
        assert "Works at Acme Corp" in p.facts

    def test_add_fact_deduplication(self):
        p = UserProfile()
        p.add_fact("Uses VSCode")
        p.add_fact("Uses VSCode")
        assert len(p.facts) == 1

    def test_to_system_prompt(self):
        p = UserProfile(name="Alice")
        p.set_preference("language", "TypeScript")
        prompt = p.to_system_prompt()
        assert "Alice" in prompt
        assert "TypeScript" in prompt

    def test_serialize_deserialize(self, tmp_path):
        p = UserProfile(name="Bob")
        p.add_fact("Senior engineer")
        path = str(tmp_path / "profile.json")
        p.save(path)

        loaded = UserProfile.load(path)
        assert loaded.name == "Bob"
        assert "Senior engineer" in loaded.facts


class TestShortTermMemory:
    def test_no_prune_under_limit(self):
        mem = ShortTermMemory(max_turns=10, max_tokens=100_000)
        msgs = [
            HumanMessage(content="Question 1"),
            AIMessage(content="Answer 1"),
            HumanMessage(content="Question 2"),
            AIMessage(content="Answer 2"),
        ]
        kept, discarded = mem.prune(msgs)
        assert len(kept) == len(msgs)
        assert len(discarded) == 0

    def test_keeps_system_message(self):
        mem = ShortTermMemory(max_turns=10, max_tokens=100_000)
        msgs = [
            SystemMessage(content="You are an assistant"),
            HumanMessage(content="Hello"),
            AIMessage(content="Hi"),
        ]
        kept, discarded = mem.prune(msgs)
        assert any(isinstance(m, SystemMessage) for m in kept)

    def test_prune_excess_turns(self):
        mem = ShortTermMemory(max_turns=2, max_tokens=100_000)
        msgs = []
        for i in range(10):
            msgs.append(HumanMessage(content=f"Q{i}"))
            msgs.append(AIMessage(content=f"A{i}"))
        kept, discarded = mem.prune(msgs)
        assert len(discarded) > 0
        assert len(kept) < len(msgs)
