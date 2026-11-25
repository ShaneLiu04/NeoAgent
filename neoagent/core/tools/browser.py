"""Web browsing tool using httpx + BeautifulSoup (lightweight, no extra browser).

CyberClaw has no browser capability — this provides headless page fetching
for tasks like scraping and content extraction.
"""

from __future__ import annotations

from ..security import Capability
from .base import RiskLevel, neoagent_tool


@neoagent_tool(
    name="browse_webpage",
    description="Fetch and extract text content from a webpage URL.",
    capabilities=Capability.NETWORK_OUTBOUND,
    risk_level=RiskLevel.MEDIUM,
    timeout_seconds=30.0,
    tags=["web", "browser", "scraping"],
)
async def browse_webpage(url: str, extract_mode: str = "text") -> str:
    """Fetch a webpage and extract its text content.

    Args:
        url: The webpage URL to fetch
        extract_mode: 'text' (readable text), 'html' (raw HTML), or 'links' (list all links)
    """
    if not url.startswith(("http://", "https://")):
        return "Error: URL must start with http:// or https://"

    try:
        import httpx
        from bs4 import BeautifulSoup

        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={
                "User-Agent": "NeoAgent/2.0 (Web Fetcher; +https://github.com/neoagent)",
            },
        ) as client:
            response = await client.get(url)
            response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        # Remove script, style, nav, footer
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        if extract_mode == "links":
            links = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                text = a.get_text(strip=True)
                if text and href:
                    links.append(f"  - {text}: {href}")
            return f"Links from {url}:\n" + "\n".join(links[:100])

        elif extract_mode == "html":
            # Return prettified HTML (truncated)
            html = soup.prettify()
            if len(html) > 50_000:
                html = html[:50_000] + "\n... [truncated]"
            return html

        else:  # text
            text = soup.get_text(separator="\n", strip=True)
            # Remove excessive newlines
            import re
            text = re.sub(r'\n{3,}', '\n\n', text)
            if len(text) > 30_000:
                text = text[:30_000] + "\n\n... [truncated]"
            return f"Content from {url}:\n\n{text}"

    except ImportError as e:
        return f"Missing dependency: {e}. Install with: pip install httpx beautifulsoup4"
    except Exception as e:
        return f"Failed to fetch {url}: {e}"
