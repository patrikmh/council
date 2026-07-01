"""Per-panelist internet tools.

Each panelist gets a fresh set of tool closures so we can attribute tool
calls to the calling agent and enforce a per-agent budget. `on_tool_call`
is invoked once per call with a small summary dict so the SSE stream can
render a badge in the UI.
"""

from typing import Awaitable, Callable

from . import browser

OnToolCall = Callable[[dict], Awaitable[None] | None]

_DEFAULT_BUDGET = 3


def make_tools(
    agent_name: str,
    on_tool_call: OnToolCall | None = None,
    budget: int = _DEFAULT_BUDGET,
) -> list:
    """Return [web_search, browse] tool functions bound to *agent_name*.

    The tools are plain async functions — pydantic-ai wraps them into Tool
    objects when passed via ``Agent(tools=[...])``.
    """
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
        gate = _over_budget()
        if gate:
            return gate
        await _notify({"tool": "web_search", "query": query[:200]})
        hits = await browser.ddg_search(query, limit=5)
        if not hits:
            return f"No results for {query!r}."
        lines = [f"Search results for {query!r}:"]
        for i, h in enumerate(hits, 1):
            lines.append(f"{i}. {h.title}\n   {h.url}\n   {h.snippet}")
        return "\n".join(lines)

    async def browse(url: str) -> str:
        """Fetch a public URL and return the readable text of the page
        (title + main text, capped). Use after web_search to read a source
        in detail."""
        gate = _over_budget()
        if gate:
            return gate
        await _notify({"tool": "browse", "url": url[:300]})
        page = await browser.fetch_page(url)
        header = f"Title: {page.title}\nURL: {page.final_url}\n\n"
        return header + (page.text or "(no readable text extracted)")

    return [web_search, browse]
