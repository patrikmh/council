"""Parallel AI-backed web tools.

Drop-in alternative to tavily.py — same SearchHit/Page data classes,
same method signatures, different backend.

Endpoints:
  * POST https://api.parallel.ai/v1/search  → keyword search with objective
  * POST https://api.parallel.ai/v1/extract → extract content from URLs

Auth: x-api-key header (set PARALLEL_API_KEY env var).

Set WEB_PROVIDER=parallel (env) to use this instead of Tavily.
Both can coexist; only one is active per run.
"""

import logging
import os
from dataclasses import dataclass

import httpx

from .tavily import SearchHit, Page  # reuse the same data classes

log = logging.getLogger("rabble")

_BASE = "https://api.parallel.ai"
_TIMEOUT_SEC = float(os.getenv("WEB_TOOLS_TIMEOUT_SEC", "12"))
_MAX_CONTENT_CHARS = 4000


def _key() -> str | None:
    k = os.getenv("PARALLEL_API_KEY")
    return k.strip() if k else None


class Parallel:
    """Thin wrapper around the Parallel AI REST API."""

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
            "search_queries": [query],
            "objective": f"Find the most relevant and recent results for: {query}",
        }
        headers = {"x-api-key": key, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=_TIMEOUT_SEC) as client:
            r = await client.post(f"{_BASE}/v1/search", json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        results = data.get("results") or []
        hits: list[SearchHit] = []
        for res in results[:limit]:
            excerpts = res.get("excerpts") or []
            snippet = excerpts[0][:300] if excerpts else (res.get("snippet") or "")[:300]
            hits.append(SearchHit(
                title=(res.get("title") or "")[:200],
                url=res.get("url") or "",
                snippet=snippet,
            ))
        return hits

    async def extract(self, url: str) -> Page:
        key = _key()
        if not key:
            return Page(title="", text="Parallel is not configured.", final_url=url)
        payload = {
            "urls": [url],
            "objective": "Extract the main readable text content of this page",
        }
        headers = {"x-api-key": key, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=_TIMEOUT_SEC) as client:
            r = await client.post(f"{_BASE}/v1/extract", json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        results = data.get("results") or []
        errors = data.get("errors") or []
        if not results:
            if errors:
                err = errors[0]
                reason = err.get("content") or err.get("error_type") or "unknown"
                return Page(title="", text=f"Fetch failed: {reason}", final_url=url)
            return Page(title="", text="No result returned.", final_url=url)
        first = results[0]
        full = first.get("full_content") or ""
        excerpts = first.get("excerpts") or []
        text = full if full else "\n\n".join(excerpts)
        return Page(
            title=(first.get("title") or "")[:200],
            text=text[:_MAX_CONTENT_CHARS],
            final_url=first.get("url") or url,
        )


client = Parallel()


async def context_block(question: str, limit: int = 3) -> str:
    """One preflight Parallel search on the user question, formatted as a
    'Current context' block ready to paste into a system prompt.

    Same contract as tavily.context_block — grounds every panelist in
    fresh facts even when they don't spontaneously decide to search.
    Returns "" when Parallel isn't configured or the search fails.
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
    log.info("context_search_ok hits=%d provider=parallel", len(hits))
    return "\n".join(lines)
