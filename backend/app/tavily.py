"""Tavily-backed web tools.

Two endpoints, one API key:

  * https://api.tavily.com/search  → LLM-optimized web search
  * https://api.tavily.com/extract → readable text of a specific URL

We hit both with plain httpx. No browser, no Docker, no OS deps — the
whole "tools" layer stays runnable on Render's Starter plan.

If ``TAVILY_API_KEY`` isn't set, ``available`` is False and the tools
layer hands panelists an empty tool list (same pattern as the old
browser fallback), so the app stays usable without web tools.
"""

import logging
import os
from dataclasses import dataclass

import httpx

log = logging.getLogger("rabble")

_BASE = "https://api.tavily.com"
_TIMEOUT_SEC = 12
_MAX_CONTENT_CHARS = 4000


def _key() -> str | None:
    k = os.getenv("TAVILY_API_KEY")
    return k.strip() if k else None


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str


@dataclass
class Page:
    title: str
    text: str
    final_url: str


class Tavily:
    """Thin wrapper around the Tavily REST API."""

    def __init__(self) -> None:
        self._checked = False

    @property
    def available(self) -> bool:
        return _key() is not None

    async def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        key = _key()
        if not key:
            return []
        payload = {
            "api_key": key,
            "query": query,
            "search_depth": "basic",
            "max_results": limit,
            "include_answer": False,
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT_SEC) as client:
            r = await client.post(f"{_BASE}/search", json=payload)
            r.raise_for_status()
            data = r.json()
        results = data.get("results") or []
        hits: list[SearchHit] = []
        for res in results[:limit]:
            hits.append(SearchHit(
                title=(res.get("title") or "")[:200],
                url=res.get("url") or "",
                snippet=(res.get("content") or "")[:300],
            ))
        return hits

    async def extract(self, url: str) -> Page:
        key = _key()
        if not key:
            return Page(title="", text="Tavily is not configured.", final_url=url)
        payload = {"api_key": key, "urls": [url]}
        async with httpx.AsyncClient(timeout=_TIMEOUT_SEC) as client:
            r = await client.post(f"{_BASE}/extract", json=payload)
            r.raise_for_status()
            data = r.json()
        results = data.get("results") or []
        if not results:
            failed = data.get("failed_results") or []
            reason = (failed[0].get("error") if failed else "no result") if failed else "no result"
            return Page(title="", text=f"Fetch failed: {reason}", final_url=url)
        first = results[0]
        raw = first.get("raw_content") or ""
        return Page(
            title=(first.get("title") or "")[:200],
            text=raw[:_MAX_CONTENT_CHARS],
            final_url=first.get("url") or url,
        )


client = Tavily()


async def context_block(question: str, limit: int = 3) -> str:
    """One preflight Tavily search on the user question, formatted as a
    'Current context' block ready to paste into a system prompt.

    Grounds every panelist in fresh facts even when they don't spontaneously
    decide to search. Returns "" when Tavily isn't configured or the search
    fails — the run continues with no grounding, same as before.
    """
    if not client.available:
        return ""
    try:
        hits = await client.search(question, limit=limit)
    except Exception as exc:
        log.warning("context_search_failed %s: %s",
                    type(exc).__name__, str(exc)[:200])
        return ""
    if not hits:
        return ""
    lines = ["Current web context (fresh search results, use these as ground truth):"]
    for i, h in enumerate(hits, 1):
        lines.append(f"  {i}. {h.title} — {h.url}")
        if h.snippet:
            lines.append(f"     {h.snippet}")
    log.info("context_search_ok hits=%d", len(hits))
    return "\n".join(lines)
