"""Per-run memory: shared tool cache + per-agent activity history.

One RunMemory lives for the duration of a single debate. It solves two
concrete waste problems observed in real runs:

1. **Redundant searching.** Multiple panelists (or the same panelist
   across rounds) issue near-identical Tavily queries, burning quota
   for the same result. The ``search_cache`` and ``browse_cache`` here
   short-circuit identical queries/URLs — first caller pays Tavily,
   everyone else gets the cached string plus a "(cached)" marker so
   they know it's not a fresh lookup.

2. **Amnesia across rounds.** A fresh ``Agent`` is built each round, so
   Claude in Round 3 doesn't remember what Claude in Round 1 voted or
   searched. We record every ballot and tool call in ``tool_calls`` and
   surface a compact "Your prior activity" block into each agent's
   round-N prompt via ``agent_history()``.

The whole object is discarded when the run ends — no persistence.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field


def _normalize_query(q: str) -> str:
    return re.sub(r"\s+", " ", q or "").strip().lower()


@dataclass
class RunMemory:
    search_cache: dict[str, str] = field(default_factory=dict)
    browse_cache: dict[str, str] = field(default_factory=dict)
    # agent name -> list of {round, kind: "search"|"browse", query|url, snippet}
    tool_calls: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))

    def get_search(self, query: str) -> str | None:
        return self.search_cache.get(_normalize_query(query))

    def put_search(self, query: str, result: str) -> None:
        self.search_cache[_normalize_query(query)] = result

    def get_browse(self, url: str) -> str | None:
        return self.browse_cache.get(url.strip())

    def put_browse(self, url: str, result: str) -> None:
        self.browse_cache[url.strip()] = result

    def record_tool(
        self,
        agent: str,
        round_index: int,
        kind: str,
        *,
        query: str | None = None,
        url: str | None = None,
    ) -> None:
        entry: dict = {"round": round_index, "kind": kind}
        if query is not None:
            entry["query"] = query
        if url is not None:
            entry["url"] = url
        self.tool_calls[agent].append(entry)

    def agent_history(
        self,
        agent: str,
        prior_ballots: list[dict],
    ) -> str:
        """Build a compact 'your prior activity in this debate' block for
        *agent*, listing their own past votes and tool calls only. Empty
        string when they've done nothing yet (round 0)."""
        my_ballots = [b for b in prior_ballots if b.get("name") == agent]
        my_calls = self.tool_calls.get(agent, [])
        if not my_ballots and not my_calls:
            return ""
        lines = ["Your prior activity in this debate (yours only, don't repeat yourself):"]
        for b in my_ballots:
            lines.append(f"  - Round {b['round'] + 1}: you voted {b['vote']}"
                         f" — {b.get('reasoning', '')[:140]}")
        if my_calls:
            lines.append("  Tools you already ran:")
            for c in my_calls:
                if c["kind"] == "search":
                    lines.append(f"    · search: {c.get('query', '')[:100]}")
                else:
                    lines.append(f"    · browse: {c.get('url', '')[:100]}")
            lines.append("  Do not repeat these queries. Cite what you already found, "
                         "or search for something genuinely new.")
        return "\n".join(lines)
