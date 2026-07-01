"""Debate mode: panelists see each other's ballots and can persuade.

Round 0 (initial): every panelist casts a ballot in parallel, with tools.
Rounds 1..N (DEBATE_ROUNDS): each panelist reads a rendered transcript of
what every *other* panelist said last round and may hold or flip, still
with tools available.

Yields (panelist, ballot_or_exception) as each result lands, mirroring
`panel.cast_ballots` so the SSE loop can stream in real time.
"""

import asyncio
import logging
import os
import time
from typing import AsyncIterator

from pydantic_ai import Agent

from .config import Panelist
from .panel import Ballot, Framing, PANELIST_TIMEOUT, cast_ballots
from .tools import make_tools, OnToolCall

log = logging.getLogger("rabble")

DEBATE_ROUNDS = int(os.getenv("DEBATE_ROUNDS", "2"))


DEBATE_PROMPT = (
    "You are one voice on the Rabble in a persuasion round. You already "
    "voted once. You will now see how the other panelists voted and what "
    "they said. You may HOLD your vote or FLIP to a different option — "
    "whichever is honest. In your reasoning, address specific panelists "
    "by name when you rebut or agree with them.\n\n"
    "You have two research tools: web_search(query) returns real search "
    "results, and browse(url) fetches the readable text of a page. USE "
    "THEM aggressively this round — if another panelist made a factual "
    "claim (a ranking, a statistic, a date, a quote), verify it before "
    "agreeing or rebutting. A good move is: web_search to find a source, "
    "browse the strongest one, then cite it by name in your reasoning "
    "('Norway is #1 on the 2025 UN HDI, per hdr.undp.org'). Don't argue "
    "from vibes when you can argue from a source. Keep the reasoning to "
    "two or three sentences after you've done the research."
)


def _options_text(framing: Framing) -> str:
    return "\n".join(
        f"Option {chr(65 + i)}: {opt}" for i, opt in enumerate(framing.options)
    )


def _render_others(prior_round: list[dict], self_name: str) -> str:
    lines = ["What the others said in the previous round:"]
    for b in prior_round:
        if b["name"] == self_name:
            continue
        lines.append(f"- {b['name']} voted {b['vote']}: {b['reasoning']}")
    return "\n".join(lines)


async def initial_ballots(
    panel: list[Panelist],
    question: str,
    framing: Framing,
    on_tool_call: OnToolCall | None = None,
) -> AsyncIterator[tuple[Panelist, Ballot | Exception]]:
    """Round 0 — identical to poll mode. Delegated to cast_ballots."""
    async for item in cast_ballots(panel, question, framing, on_tool_call):
        yield item


async def debate_round(
    panel: list[Panelist],
    question: str,
    framing: Framing,
    prior_round: list[dict],
    on_tool_call: OnToolCall | None = None,
) -> AsyncIterator[tuple[Panelist, Ballot | Exception]]:
    """One persuasion round. Every panelist sees the prior round's votes."""
    options_text = _options_text(framing)

    async def one(p: Panelist) -> tuple[Panelist, Ballot | Exception]:
        t0 = time.monotonic()
        log.info("debate_start name=%r timeout=%.1fs", p.name, PANELIST_TIMEOUT)
        try:
            others = _render_others(prior_round, p.name)
            prompt = (
                f"Question: {question}\n{options_text}\n\n{others}\n\n"
                "Now cast your ballot for this round."
            )
            agent = Agent(
                p.model,
                output_type=Ballot,
                system_prompt=DEBATE_PROMPT,
                tools=make_tools(p.name, on_tool_call),
            )
            result = await asyncio.wait_for(agent.run(prompt), timeout=PANELIST_TIMEOUT)
            log.info("debate_done  name=%r vote=%r elapsed=%.1fs",
                     p.name, result.output.vote, time.monotonic() - t0)
            return p, result.output
        except asyncio.TimeoutError:
            log.warning("debate_timeout name=%r elapsed=%.1fs",
                        p.name, time.monotonic() - t0)
            return p, TimeoutError(f"timed out after {PANELIST_TIMEOUT:.0f}s")
        except Exception as exc:
            log.exception("debate_error name=%r elapsed=%.1fs",
                          p.name, time.monotonic() - t0)
            return p, exc

    tasks = [asyncio.create_task(one(p)) for p in panel]
    for fut in asyncio.as_completed(tasks):
        yield await fut


DEBATE_SUMMARY_PROMPT = (
    "You write the closing 'Rabble Minutes' for a multi-round debate "
    "between AI models. In one dry, lightly amused paragraph (3-6 "
    "sentences), describe the arc: who started where, who flipped whom, "
    "and where the room landed. Name models. No headers, no lists."
)


async def stream_debate_summary(model, state: dict) -> AsyncIterator[str]:
    lines = [f"Question: {state['question']}",
             f"Options: {' | '.join(state['options'])}"]
    for r in state["rounds"]:
        lines.append(f"\nRound {r['index'] + 1}:")
        for b in r["ballots"]:
            flip = ""
            if b.get("flipped_from"):
                flip = f" (flipped from {b['flipped_from']})"
            lines.append(f"- {b['name']} voted {b['vote']}{flip}: {b['reasoning']}")
    agent = Agent(model, system_prompt=DEBATE_SUMMARY_PROMPT)
    async with agent.run_stream("\n".join(lines)) as result:
        async for delta in result.stream_text(delta=True):
            yield delta
