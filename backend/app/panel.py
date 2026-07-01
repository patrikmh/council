"""The Rabble itself: frame the question, collect ballots, summarize.

Three agent roles, all Pydantic AI:
  framer     -> turns a free-text question into 2-6 answer options
  panelists  -> one per model, vote with one-sentence reasoning, in parallel
  summarizer -> writes the dry Rabble summary, streamed token by token
"""

import asyncio
import logging
import os
import time
from typing import AsyncIterator

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from .config import Panelist
from .tools import make_tools, OnToolCall

log = logging.getLogger("rabble")

PANELIST_TIMEOUT = float(os.getenv("PANELIST_TIMEOUT_SEC", "60"))


class Framing(BaseModel):
    options: list[str] = Field(
        description=(
            "Two to six mutually exclusive answer options as short labels "
            "(1-4 words each). If the question already names options, keep them. "
            "Use exactly as many options as the question implies: two for a "
            "simple either/or, more for ranking or multi-way choices."
        ),
        min_length=2,
        max_length=6,
    )


class Ballot(BaseModel):
    vote: str = Field(
        description=(
            "The label of the option you chose, exactly as written in the "
            "prompt (case-sensitive)."
        )
    )
    reasoning: str = Field(description="One or two sentences defending the vote")


FRAMER_PROMPT = (
    "You frame questions for a poll of AI models. Given the user's question, "
    "produce 2-6 mutually exclusive answer options, each a label of 1-4 words. "
    "If the question already names options, keep them. Use exactly as many "
    "options as the question implies: two for a simple either/or, more for "
    "multi-way choices (e.g. 'Which season is best?' -> Spring, Summer, "
    "Autumn, Winter). Do not answer the question yourself."
)

PANELIST_PROMPT = (
    "You are one voice on the Rabble. You are given a question and a "
    "set of options. Pick exactly one option (by its exact label) and defend "
    "your choice in one or two sentences. Commit to a choice even if the "
    "question is silly or underspecified; if the premise is flawed, vote for "
    "the option that survives the flaw and say why. Be yourself — your "
    "reasoning will be quoted.\n\n"
    "You have two research tools: web_search(query) returns a list of "
    "titles/urls/snippets from a real web search, and browse(url) fetches "
    "the readable text of a page. USE THEM whenever the question depends on "
    "current facts, numbers, rankings, news, prices, sports results, or "
    "anything you might be out of date on — do not guess from stale "
    "training data. A good pattern is: one web_search to find sources, one "
    "or two browse calls to read the strongest ones, then vote. Cite the "
    "site or source in your reasoning (e.g. 'per the UN HDI 2025 report…') "
    "so the panel can tell your ballot from a vibes-based one."
)

SUMMARIZER_PROMPT = (
    "You write the 'Rabble Minutes' for a one-shot AI model poll. Rules:\n"
    "  • TWO sentences. Max three.\n"
    "  • Dry, wry, one clean observation — no throat-clearing.\n"
    "  • Name at least one model. If a reasoning was odd, point it out "
    "deadpan.\n"
    "  • No heading, no lists, no bold, no quotes longer than four words.\n"
    "Plain prose. Ship it."
)


async def frame_question(model, question: str) -> Framing:
    agent = Agent(model, output_type=Framing, system_prompt=FRAMER_PROMPT)
    result = await agent.run(question)
    return result.output


# Tone palette cycled across options for visual variety
_TONES = ["aqua", "clay", "violet", "rose", "mint", "sky", "coral", "gold"]


def tone_for(index: int) -> str:
    return _TONES[index % len(_TONES)]


async def cast_ballots(
    panel: list[Panelist],
    question: str,
    framing: Framing,
    on_tool_call: OnToolCall | None = None,
) -> AsyncIterator[tuple[Panelist, Ballot | Exception]]:
    """Fan the question out to every panelist; yield ballots as they land."""
    options_text = "\n".join(
        f"Option {chr(65 + i)}: {opt}"
        for i, opt in enumerate(framing.options)
    )
    prompt = f"Question: {question}\n{options_text}"

    async def one(p: Panelist) -> tuple[Panelist, Ballot | Exception]:
        t0 = time.monotonic()
        log.info("panelist_start name=%r timeout=%.1fs", p.name, PANELIST_TIMEOUT)
        try:
            agent = Agent(
                p.model,
                output_type=Ballot,
                system_prompt=PANELIST_PROMPT,
                tools=make_tools(p.name, on_tool_call),
            )
            result = await asyncio.wait_for(agent.run(prompt), timeout=PANELIST_TIMEOUT)
            log.info("panelist_done  name=%r vote=%r elapsed=%.1fs",
                     p.name, result.output.vote, time.monotonic() - t0)
            return p, result.output
        except asyncio.TimeoutError:
            log.warning("panelist_timeout name=%r elapsed=%.1fs",
                        p.name, time.monotonic() - t0)
            return p, TimeoutError(f"timed out after {PANELIST_TIMEOUT:.0f}s")
        except Exception as exc:  # a dead panelist shouldn't kill the run
            log.exception("panelist_error name=%r elapsed=%.1fs",
                          p.name, time.monotonic() - t0)
            return p, exc

    tasks = [asyncio.create_task(one(p)) for p in panel]
    for fut in asyncio.as_completed(tasks):
        yield await fut


async def stream_summary(model, state: dict) -> AsyncIterator[str]:
    options_line = " | ".join(state["options"])
    lines = [f"Question: {state['question']}", f"Options: {options_line}"]
    for b in state["ballots"]:
        lines.append(f"- {b['name']} voted {b['vote']}: {b['reasoning']}")
    agent = Agent(model, system_prompt=SUMMARIZER_PROMPT)
    async with agent.run_stream("\n".join(lines)) as result:
        async for delta in result.stream_text(delta=True):
            yield delta
