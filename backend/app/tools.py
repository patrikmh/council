"""Per-panelist internet tools, backed by Tavily.

Each panelist gets a fresh set of tool closures so we can attribute tool
calls to the calling agent and enforce a per-agent budget. `on_tool_call`
is invoked once per call with a small summary dict so the SSE stream can
render a badge in the UI.
"""

import logging
import os
import time
from typing import Awaitable, Callable

from . import tavily
from .memory import RunMemory

log = logging.getLogger("rabble")

OnToolCall = Callable[[dict], Awaitable[None] | None]

# How many tool calls each panelist may make per round.
# Round 0 (opening ballot) is capped tighter — the framer already ran ONE
# preflight Tavily search and injected the top hits into every panelist's
# system prompt, so a panelist rarely needs to search on their own before
# casting a first ballot. Persuasion rounds get more headroom for
# verifying peers' claims.
_INITIAL_BUDGET = int(os.getenv("TOOL_BUDGET_INITIAL", "2"))
_DEBATE_BUDGET = int(os.getenv("TOOL_BUDGET_DEBATE", "5"))


def _budget_for_round(round_index: int) -> int:
    return _INITIAL_BUDGET if round_index == 0 else _DEBATE_BUDGET


def make_tools(
    agent_name: str,
    on_tool_call: OnToolCall | None = None,
    budget: int | None = None,
    memory: RunMemory | None = None,
    round_index: int = 0,
) -> list:
    if budget is None:
        budget = _budget_for_round(round_index)
    """Return [web_search, browse] tool functions bound to *agent_name*.

    If Tavily is not configured (no ``TAVILY_API_KEY``), returns an empty
    list — better to hand the model no tools than to hand it tools that
    always fail and let it burn its retry budget.
    """
    if not tavily.client.available:
        log.info("tools_disabled name=%r reason=%r", agent_name, "TAVILY_API_KEY not set")
        return []
    state = {"used": 0}

    async def _notify(payload: dict) -> None:
        if on_tool_call is None:
            return
        payload = {"agent": agent_name, **payload}
        result = on_tool_call(payload)
        if hasattr(result, "__await__"):
            await result  # type: ignore[func-returns-value]

    def _over_budget() -> str | None:
        if state["used"] >= budget:
            return (
                f"Tool budget exhausted ({budget} calls). "
                "Decide with what you already have."
            )
        state["used"] += 1
        return None

    async def web_search(query: str) -> str:
        """Search the public web. Returns a short list of title/url/snippet
        triples. Use this to find sources, then call browse(url) to read
        one in detail."""
        # Cache lookup — free hit, doesn't count against budget or Tavily.
        if memory is not None:
            cached = memory.get_search(query)
            if cached is not None:
                await _notify({"tool": "web_search", "query": query[:200],
                              "cached": True})
                log.info("tool_cached name=%r tool=web_search q=%r",
                         agent_name, query[:80])
                if memory is not None:
                    memory.record_tool(agent_name, round_index, "search", query=query)
                return f"(cached from earlier this run)\n{cached}"

        gate = _over_budget()
        if gate:
            log.info("tool_budget name=%r tool=web_search", agent_name)
            return gate
        await _notify({"tool": "web_search", "query": query[:200]})
        t0 = time.monotonic()
        log.info("tool_call name=%r tool=web_search q=%r", agent_name, query[:80])
        try:
            hits = await tavily.client.search(query, limit=5)
        except Exception as exc:
            log.exception("tool_error name=%r tool=web_search", agent_name)
            return f"web_search failed: {type(exc).__name__}: {exc}"
        log.info("tool_ok   name=%r tool=web_search hits=%d elapsed=%.1fs",
                 agent_name, len(hits), time.monotonic() - t0)
        if not hits:
            result = f"No results for {query!r}."
        else:
            lines = [f"Search results for {query!r}:"]
            for i, h in enumerate(hits, 1):
                lines.append(f"{i}. {h.title}\n   {h.url}\n   {h.snippet}")
            result = "\n".join(lines)
        if memory is not None:
            memory.put_search(query, result)
            memory.record_tool(agent_name, round_index, "search", query=query)
        return result

    async def browse(url: str) -> str:
        """Fetch a public URL and return the readable text of the page
        (title + main text, capped). Use after web_search to read a source
        in detail."""
        if memory is not None:
            cached = memory.get_browse(url)
            if cached is not None:
                await _notify({"tool": "browse", "url": url[:300], "cached": True})
                log.info("tool_cached name=%r tool=browse url=%r",
                         agent_name, url[:80])
                memory.record_tool(agent_name, round_index, "browse", url=url)
                return f"(cached from earlier this run)\n{cached}"

        gate = _over_budget()
        if gate:
            log.info("tool_budget name=%r tool=browse", agent_name)
            return gate
        await _notify({"tool": "browse", "url": url[:300]})
        t0 = time.monotonic()
        log.info("tool_call name=%r tool=browse url=%r", agent_name, url[:80])
        try:
            page = await tavily.client.extract(url)
        except Exception as exc:
            log.exception("tool_error name=%r tool=browse", agent_name)
            return f"browse failed: {type(exc).__name__}: {exc}"
        log.info("tool_ok   name=%r tool=browse bytes=%d elapsed=%.1fs",
                 agent_name, len(page.text), time.monotonic() - t0)
        header = f"Title: {page.title}\nURL: {page.final_url}\n\n"
        result = header + (page.text or "(no readable text extracted)")
        if memory is not None:
            memory.put_browse(url, result)
            memory.record_tool(agent_name, round_index, "browse", url=url)
        return result

    return [web_search, browse]
