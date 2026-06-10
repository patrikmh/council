"""The AI Council itself: frame the question, collect ballots, summarize.

Three agent roles, all Pydantic AI:
  framer     -> turns a free-text question into 2-6 answer options
  panelists  -> one per model, vote with one-sentence reasoning, in parallel
  summarizer -> writes the dry council summary, streamed token by token
"""

import asyncio
from typing import AsyncIterator

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from .config import Panelist


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
    "You are one voice on the AI Council. You are given a question and a "
    "set of options. Pick exactly one option (by its exact label) and defend "
    "your choice in one or two sentences. Commit to a choice even if the "
    "question is silly or underspecified; if the premise is flawed, vote for "
    "the option that survives the flaw and say why. Be yourself — your "
    "reasoning will be quoted."
)

SUMMARIZER_PROMPT = (
    "You write the 'Council Summary' for an AI model poll: a single dry, "
    "lightly amused paragraph (3-5 sentences) describing how the vote went, "
    "naming the models on each side and the gist of their arguments. If a "
    "model's reasoning was odd, point it out deadpan. No headers, no lists, "
    "no quotes longer than a few words."
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
    panel: list[Panelist], question: str, framing: Framing
) -> AsyncIterator[tuple[Panelist, Ballot | Exception]]:
    """Fan the question out to every panelist; yield ballots as they land."""
    options_text = "\n".join(
        f"Option {chr(65 + i)}: {opt}"
        for i, opt in enumerate(framing.options)
    )
    prompt = f"Question: {question}\n{options_text}"

    async def one(p: Panelist) -> tuple[Panelist, Ballot | Exception]:
        try:
            agent = Agent(p.model, output_type=Ballot,
                          system_prompt=PANELIST_PROMPT)
            result = await agent.run(prompt)
            return p, result.output
        except Exception as exc:  # a dead panelist shouldn't kill the run
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
