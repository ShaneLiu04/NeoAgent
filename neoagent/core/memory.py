"""Hybrid memory system: short-term sliding window + long-term vector store.

Fixes CyberClaw's memory limitations:
1. Vector-based semantic search (Chromadb) — not just raw text matching
2. Structured UserProfile (Pydantic model) — not raw markdown strings
3. Intelligent context pruning based on tiktoken token counting — not
   simple turn-counting (which ignores message length variance)
4. Memory consolidation: periodic background summarization into vectordb

CyberClaw's context.py only does turn-based pruning with hardcoded
trigger_turns=40, keep_turns=10 — this breaks when long code blocks
or tool outputs consume the context window.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ._compat import (
    BaseMessage,
    BaseModel,
    Field,
    HumanMessage,
    SystemMessage,
)
from .config import get_config

# ── User Profile (structured, not raw markdown) ────


class UserPreference(BaseModel):
    """A single user preference with metadata."""
    key: str
    value: str
    category: str = "general"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class UserProfile(BaseModel):
    """Structured user profile — replaces CyberClaw's raw user_profile.md.

    CyberClaw stores preferences as free-form markdown. That's brittle:
    - No structure means LLM must parse natural language to find facts
    - Easy to accidentally overwrite or corrupt
    - No way to query specific facts

    This Pydantic model gives structured, queryable user data with
    serialization support for persistence.
    """
    user_id: str = "default"
    name: str = ""
    preferences: list[UserPreference] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)       # Key facts about the user
    constraints: list[str] = Field(default_factory=list)  # "Never do X", "Always do Y"
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    def get_preference(self, key: str) -> str | None:
        for p in self.preferences:
            if p.key == key:
                return p.value
        return None

    def set_preference(self, key: str, value: str, category: str = "general") -> None:
        for p in self.preferences:
            if p.key == key:
                p.value = value
                p.updated_at = datetime.now(UTC).isoformat()
                return
        self.preferences.append(UserPreference(key=key, value=value, category=category))

    def add_fact(self, fact: str) -> None:
        if fact not in self.facts:
            self.facts.append(fact)
            self.updated_at = datetime.now(UTC).isoformat()

    def add_constraint(self, constraint: str) -> None:
        if constraint not in self.constraints:
            self.constraints.append(constraint)

    def to_system_prompt(self) -> str:
        """Render profile as a system prompt section."""
        parts = [
            "[User Profile — IMPORTANT: you MUST personalize every response based on this data]",
        ]
        if self.name:
            parts.append(f"Name: {self.name}")
        if self.preferences:
            parts.append("Preferences:")
            for p in self.preferences:
                parts.append(f"  - {p.key}: {p.value}")
        if self.facts:
            parts.append("Facts:")
            for f in self.facts:
                parts.append(f"  - {f}")
        if self.constraints:
            parts.append("Constraints:")
            for c in self.constraints:
                parts.append(f"  - {c}")
        if len(parts) == 1:
            parts.append("(empty — no preferences or facts recorded yet)")
        return "\n".join(parts)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.model_dump_json(indent=2))

    @classmethod
    def load(cls, path: str) -> UserProfile:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return cls.model_validate(json.load(f))
        return cls()


# ── Token Counter ──────────────────────────────────


class _OfflineTokenEncoder:
    """Air-gapped / intranet-safe BPE approximation when tiktoken vocab isn't cached locally.

    Avoids blocking corporate CI/CD or intranet engineers on unreachable
    openaipublic.blob.core.windows.net requests.
    """

    import re as _re

    _TOKEN_PAT = _re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9_]{1,4}|[^\sA-Za-z0-9_\u4e00-\u9fff]")

    def encode(self, text: str) -> list[int]:
        if not text:
            return []
        return list(range(len(self._TOKEN_PAT.findall(text))))


def _has_cached_tiktoken_bpe() -> bool:
    """Return True only if cl100k_base BPE file is already present in local disk cache."""
    import hashlib
    import tempfile

    url = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
    cache_key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    candidates = [
        os.environ.get("TIKTOKEN_CACHE_DIR"),
        os.environ.get("DATA_GYM_CACHE_DIR"),
        os.path.join(tempfile.gettempdir(), "data-gym-cache"),
    ]
    return any(c and os.path.isfile(os.path.join(c, cache_key)) for c in candidates)


@dataclass
class TokenCounter:
    """Accurate token counting using tiktoken with air-gapped intranet fallback.

    CyberClaw uses turn counting for context pruning — but a single turn
    can be 10 tokens (a greeting) or 10,000 tokens (a code file + tool output).
    Token-based counting is the only correct approach.
    """
    encoding_name: str = "cl100k_base"  # Default for GPT-4 family
    model_name: str | None = None

    def __post_init__(self) -> None:
        import logging
        if not _has_cached_tiktoken_bpe() and os.getenv("NEOAGENT_TIKTOKEN_ONLINE") != "1":
            self._encoder: Any = _OfflineTokenEncoder()
            return
        try:
            import tiktoken

            if self.model_name:
                self._encoder = tiktoken.encoding_for_model(self.model_name)
            else:
                self._encoder = tiktoken.get_encoding(self.encoding_name)
        except Exception:
            logging.getLogger("neoagent.memory").warning(
                "Failed to load tiktoken encoding '%s', falling back to offline tokenizer",
                self.encoding_name,
            )
            self._encoder = _OfflineTokenEncoder()

    def count(self, text: str) -> int:
        return len(self._encoder.encode(text))

    def count_message(self, msg: BaseMessage) -> int:
        """Count tokens in a single message, including role overhead (~4 tokens)."""
        content = ""
        if isinstance(msg.content, str):
            content = msg.content
        elif isinstance(msg.content, list):
            # Multimodal content — count text parts
            for block in msg.content:
                if isinstance(block, dict) and block.get("type") == "text":
                    content += block.get("text", "")
        return self.count(content) + 4  # role overhead

    def count_messages(self, messages: Sequence[BaseMessage]) -> int:
        return sum(self.count_message(m) for m in messages)


# ── Short-Term Memory ──────────────────────────────


@dataclass
class ShortTermMemory:
    """Sliding window of recent conversation turns with token-aware pruning.

    Unlike CyberClaw's fixed turn-count pruning, this uses actual token
    counts to decide when to trim. Keeps the most recent messages that
    fit within a token budget, plus a configurable number of turns.
    """
    max_turns: int = 20
    max_tokens: int = 80_000  # Reserve headroom for system prompt + response
    token_counter: TokenCounter = field(default_factory=TokenCounter)
    _summary: str = ""

    def prune(
        self,
        messages: list[BaseMessage],
    ) -> tuple[list[BaseMessage], list[BaseMessage]]:
        """Prune messages to fit within limits. Returns (kept, discarded)."""
        # Separate system message (always preserved)
        system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
        non_system = [m for m in messages if not isinstance(m, SystemMessage)]

        if not non_system:
            return system_msgs, []

        # Group into turns (each turn starts with HumanMessage)
        turns: list[list[BaseMessage]] = []
        current: list[BaseMessage] = []
        for msg in non_system:
            if isinstance(msg, HumanMessage) and current:
                turns.append(current)
                current = []
            current.append(msg)
        if current:
            turns.append(current)

        # If under limits, keep all
        total_turns = len(turns)
        total_tokens = self.token_counter.count_messages(non_system)
        if total_turns <= self.max_turns and total_tokens <= self.max_tokens:
            return system_msgs + non_system, []

        # Take turns from the end (most recent) until we hit limits
        kept_turns: list[list[BaseMessage]] = []
        kept_tokens = 0
        for turn in reversed(turns):
            turn_tokens = self.token_counter.count_messages(turn)
            if len(kept_turns) >= self.max_turns or kept_tokens + turn_tokens > self.max_tokens:
                break
            kept_turns.insert(0, turn)
            kept_tokens += turn_tokens

        # Determine discarded turns
        kept_count = len(kept_turns)
        discarded: list[BaseMessage] = []
        if kept_count == 0:
            # All turns exceed limits — discard everything except system msgs
            discarded = list(non_system)
        else:
            for turn in turns[:-kept_count]:
                discarded.extend(turn)

        # Reconstruct
        kept_msgs: list[BaseMessage] = list(system_msgs)
        for turn in kept_turns:
            kept_msgs.extend(turn)

        return kept_msgs, discarded

    @property
    def summary(self) -> str:
        return self._summary

    def update_summary(self, new_summary: str) -> None:
        self._summary = new_summary


# ── Long-Term Memory (Vector Store) ────────────────


class LongTermMemory:
    """Vector-based semantic memory using ChromaDB.

    CyberClaw has no semantic search — old conversations are summarized
    into a single string, losing detail. This stores embeddings for
    later semantic retrieval, enabling queries like "what did we decide
    about the deployment strategy last week?"
    """

    def __init__(self, persist_dir: str, collection_name: str = "neoagent_memory"):
        self.persist_dir = persist_dir
        self.collection_name = collection_name
        self._client = None
        self._collection = None

    @property
    def is_available(self) -> bool:
        try:
            self._ensure_initialized()
            return True
        except Exception:
            return False

    def _ensure_initialized(self) -> None:
        if self._collection is not None:
            return
        import chromadb
        from chromadb.config import Settings

        os.makedirs(self.persist_dir, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=self.persist_dir,
            settings=Settings(anonymized_telemetry=False),
        )
        self._collection = self._client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def store(self, content: str, metadata: dict[str, Any] | None = None, doc_id: str | None = None) -> str:
        """Store a memory entry with embedding."""
        self._ensure_initialized()
        import uuid

        doc_id = doc_id or str(uuid.uuid4())
        meta = metadata or {}
        meta["timestamp"] = datetime.now(UTC).isoformat()

        self._collection.add(
            documents=[content],
            metadatas=[meta],
            ids=[doc_id],
        )
        return doc_id

    def search(self, query: str, n_results: int = 5) -> list[dict[str, Any]]:
        """Semantic search through long-term memory."""
        self._ensure_initialized()
        count = self._collection.count()
        if count == 0:
            return []
        results = self._collection.query(
            query_texts=[query],
            n_results=min(n_results, count),
        )
        entries = []
        if results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                entries.append({
                    "id": doc_id,
                    "content": results["documents"][0][i] if results["documents"] else "",
                    "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                    "distance": results["distances"][0][i] if results["distances"] else None,
                })
        return entries

    def delete(self, doc_id: str) -> None:
        self._ensure_initialized()
        self._collection.delete(ids=[doc_id])

    def count(self) -> int:
        try:
            self._ensure_initialized()
            return self._collection.count()
        except Exception as exc:
            import logging
            logging.getLogger("neoagent.memory").debug("LongTermMemory count failed: %s", exc)
            return 0


# ── Memory Consolidator ────────────────────────────


class MemoryConsolidator:
    """Consolidates old short-term messages into long-term vector memory.

    Runs periodically: when short-term memory overflows, expired messages
    are summarized and stored as vector embeddings for future semantic retrieval.
    """

    def __init__(
        self,
        long_term: LongTermMemory,
        consolidation_interval: int = 10,
    ) -> None:
        self.long_term = long_term
        self.interval = consolidation_interval
        self._turn_counter = 0
        self._pending_consolidation: list[BaseMessage] = []

    def on_prune(self, discarded: list[BaseMessage]) -> None:
        """Queue discarded messages for consolidation."""
        self._pending_consolidation.extend(discarded)

    def should_consolidate(self) -> bool:
        self._turn_counter += 1
        return (
            self._turn_counter % self.interval == 0
            and len(self._pending_consolidation) > 0
            and self.long_term.is_available
        )

    async def consolidate(self, summary_llm) -> str | None:
        """Summarize pending messages and store in vector memory."""
        if not self._pending_consolidation:
            return None

        # Build text from pending messages
        text = "\n".join(
            f"[{m.type}]: {m.content}"
            for m in self._pending_consolidation
            if hasattr(m, "content") and m.content
        )

        if len(text) < 100:  # Too short to be useful
            self._pending_consolidation.clear()
            return None

        # Summarize using LLM
        prompt = (
            "Summarize the following conversation in 2-3 sentences, focusing on "
            "decisions made, facts learned, and actions taken. Be concise.\n\n"
            f"{text}"
        )
        try:
            response = await summary_llm.ainvoke([HumanMessage(content=prompt)])
            summary = response.content if hasattr(response, "content") else str(response)
        except Exception:
            summary = text[:500]

        # Store in vector memory
        if self.long_term.is_available:
            self.long_term.store(
                content=summary,
                metadata={
                    "type": "conversation_summary",
                    "message_count": len(self._pending_consolidation),
                    "char_count": len(text),
                },
            )

        self._pending_consolidation.clear()
        return summary


# ── Unified Memory Manager ─────────────────────────


class MemoryManager:
    """Orchestrates short-term, long-term, and profile memory.

    Single entry point for the agent loop — provides:
    - Token-aware context pruning
    - Automatic consolidation to vector store
    - Structured user profile management
    """

    def __init__(self, profile_path: str | None = None) -> None:
        cfg = get_config()
        self.short_term = ShortTermMemory(max_turns=cfg.memory.short_term_turns)
        self.long_term = LongTermMemory(persist_dir=cfg.chroma_path)
        self.consolidator = MemoryConsolidator(
            long_term=self.long_term,
            consolidation_interval=cfg.memory.consolidation_interval,
        )
        self.profile_path = profile_path or str(cfg.memory_dir / "user_profile.json")
        self.profile = UserProfile.load(self.profile_path)

    def save_profile(self) -> None:
        self.profile.save(self.profile_path)

    def retrieve_context(self, query: str) -> str:
        """Semantic search through long-term memory for relevant context."""
        if not self.long_term.is_available:
            return ""
        entries = self.long_term.search(query, n_results=3)
        if not entries:
            return ""
        return "\n".join(
            f"- {e['content']}" for e in entries if e.get("content")
        )

    def build_system_prompt(self, context_query: str | None = None) -> str:
        """Build a comprehensive system prompt with all memory layers."""
        # Auto-reload profile if the file was modified externally
        # (e.g. by the save_user_profile tool in the same session)
        if os.path.exists(self.profile_path):
            mtime = os.path.getmtime(self.profile_path)
            if not hasattr(self, "_profile_mtime") or mtime > self._profile_mtime:
                self.profile = UserProfile.load(self.profile_path)
                self._profile_mtime = mtime

        parts = [
            self.profile.to_system_prompt(),
            "",
            "[Recent Context]",
            self.short_term.summary or "(start of conversation)",
        ]

        # Add relevant long-term memories
        if context_query:
            relevant = self.retrieve_context(context_query)
            if relevant:
                parts.extend(["", "[Related Past Discussions]", relevant])

        return "\n".join(parts)
