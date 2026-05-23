"""Web search integration with caching.

CyberClaw had no built-in web search — requires an external skill.
This provides first-class Tavily API integration with result caching.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

from ..config import get_config
from ..security import Capability
from .base import RiskLevel
from .base import neoagent_tool


class SearchCache:
    """Simple file-based search cache to reduce API calls and cost."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir or (get_config().workspace_dir / ".cache" / "search")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = 3600  # 1 hour

    def _key(self, query: str, max_results: int) -> str:
        raw = f"{query.lower().strip()}|{max_results}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def get(self, query: str, max_results: int) -> list[dict] | None:
        key = self._key(query, max_results)
        cache_file = self.cache_dir / f"{key}.json"
        if not cache_file.exists():
            return None
        try:
            data = json.loads(cache_file.read_text())
            if time.time() - data.get("timestamp", 0) < self.ttl_seconds:
                return data.get("results")
        except Exception:
            pass
        return None

    def set(self, query: str, max_results: int, results: list[dict]) -> None:
        key = self._key(query, max_results)
        cache_file = self.cache_dir / f"{key}.json"
        cache_file.write_text(json.dumps({
            "timestamp": time.time(),
            "query": query,
            "results": results,
        }))


_cache = SearchCache()


def _format_results(results: list[dict]) -> str:
    """Format search results for display."""
    if not results:
        return "No results found."

    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        content = r.get("content", "")
        # Truncate long content
        if len(content) > 300:
            content = content[:300] + "..."
        lines.append(f"{i}. {title}")
        if url:
            lines.append(f"   URL: {url}")
        lines.append(f"   {content}")
        lines.append("")
    return "\n".join(lines)


@neoagent_tool(
    name="web_search",
    description="Search the web for current information. Returns titles, URLs, and snippets.",
    capabilities=Capability.NETWORK_OUTBOUND,
    risk_level=RiskLevel.MEDIUM,
    timeout_seconds=30.0,
    tags=["search", "web", "tavily"],
)
async def web_search(query: str, max_results: int = 5) -> str:
    """Search the web using Tavily API with caching."""
    # Check cache first
    cached = _cache.get(query, max_results)
    if cached is not None:
        return _format_results(cached) + "\n(cached result)"

    api_key = get_config().api_key_for("tavily")
    if not api_key:
        return (
            "[SEARCH_UNAVAILABLE] Web search is not configured (missing TAVILY_API_KEY). "
            "Do NOT attempt further web searches. Answer directly from your existing knowledge."
        )

    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=api_key)
        response = await asyncio.to_thread(
            client.search,
            query=query,
            max_results=max_results,
            include_answer=True,
        )

        results = []
        if response.get("answer"):
            results.append({
                "title": "AI Summary",
                "url": "",
                "content": response["answer"],
            })
        for r in response.get("results", []):
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", ""),
            })

        _cache.set(query, max_results, results)
        return _format_results(results)

    except ImportError:
        return "[SEARCH_ERROR] Tavily package not installed. Run: pip install tavily-python"
    except Exception as e:
        return f"[SEARCH_ERROR] {e}"
