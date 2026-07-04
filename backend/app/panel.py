"""The Rabble itself: frame the question, collect ballots, summarize.

Three agent roles, all Pydantic AI:
  framer     -> turns a free-text question into 2-6 answer options
  panelists  -> one per model, vote with one-sentence reasoning, in parallel
  summarizer -> writes the dry Rabble summary, streamed token by token
"""

import asyncio
import datetime as _dt
import logging
import os
import time
from typing import AsyncIterator

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings

from .config import Panelist
from .memory import RunMemory
from .tools import make_tools, OnToolCall

log = logging.getLogger("rabble")

PANELIST_TIMEOUT = float(os.getenv("PANELIST_TIMEOUT_SEC", "90"))

# ── Model settings per role (cap tokens to save cost) ──────────────────────
# Output tokens
POLL_PANELIST_MAX_TOKENS = int(os.getenv("POLL_PANELIST_MAX_TOKENS", "2000"))
FRAMER_MAX_TOKENS = int(os.getenv("POLL_FRAMER_MAX_TOKENS", "256"))
SUMMARY_MAX_TOKENS = int(os.getenv("POLL_SUMMARY_MAX_TOKENS", "128"))

# Reasoning tokens — same idea as debate.py
# NOTE: reasoning.max_tokens only (no reasoning.effort) — OpenAI rejects both.
REASONING_TOKEN_BUDGET = int(os.getenv("POLL_REASONING_TOKEN_BUDGET", "7000"))

POLL_PANELIST_SETTINGS = ModelSettings(
    max_tokens=POLL_PANELIST_MAX_TOKENS,
    extra_body={"reasoning": {"max_tokens": REASONING_TOKEN_BUDGET}},
)
FRAMER_SETTINGS = ModelSettings(
    max_tokens=FRAMER_MAX_TOKENS,
    extra_body={"reasoning": {"max_tokens": REASONING_TOKEN_BUDGET}},
)
POLL_SUMMARY_SETTINGS = ModelSettings(
    max_tokens=SUMMARY_MAX_TOKENS,
    extra_body={"reasoning": {"max_tokens": REASONING_TOKEN_BUDGET}},
)


def today_iso() -> str:
    return _dt.date.today().isoformat()


def _preamble(context: str = "") -> str:
    """Injected at the top of every panelist system prompt so the model
    knows what today is and (optionally) has fresh search results to
    ground itself in."""
    date = today_iso()
    lines = [
        f"Today's date is {date}. Your training data is older than that; "
        "treat any event dated on or after your cutoff as possibly having "
        "already happened."
    ]
    if context:
        lines.append("")
        lines.append(context)
    return "\n".join(lines) + "\n\n"


class Framing(BaseModel):
    options: list[str] = Field(
        description=(
            "Between 2 and 6 mutually exclusive answer options as short labels "
            "(1-4 words each). Default to 3-6 options for open-ended questions "
            "so the panel has real choices to weigh. Only use exactly 2 options "
            "when the question itself is explicitly binary — either it literally "
            "names two alternatives ('X or Y?', 'Should we A or B?') or the "
            "answer space is truly boolean (yes/no, true/false). For 'What/"
            "which/who is best…' style questions, list the real contenders "
            "(usually 4-6). If the question already names candidates, keep "
            "them all, even if that means 3+ options."
        ),
        min_length=2,
        max_length=6,
    )
    criteria: list[str] = Field(
        description=(
            "2-4 short judging criteria tailored to the question (e.g. "
            "'evidence quality', 'recency of data', 'practical feasibility'). "
            "Arguments in the debate will be graded against these."
        ),
        min_length=2,
        max_length=4,
    )


class Ballot(BaseModel):
    vote: str = Field(
        description=(
            "The label of the option you chose, exactly as written in the "
            "prompt (case-sensitive)."
        )
    )
    reasoning: str = Field(description="One or two sentences defending the vote")
    confidence: int = Field(
        ge=0, le=100,
        description="Your confidence that your chosen option is correct, 0-100.",
    )


FRAMER_PROMPT = (
    "You frame questions for a poll of AI models. Given the user's question, "
    "produce mutually exclusive answer options as short labels (1-4 words). "
    "\n\n"
    "Default: 3-6 options — enough real contenders to make the vote "
    "interesting. Examples:\n"
    "  • 'Which season is best?' → Spring, Summer, Autumn, Winter\n"
    "  • 'What Nordic country is best at sports?' → Norway, Sweden, "
    "Denmark, Finland, Iceland\n"
    "  • 'Best programming language for a beginner?' → Python, JavaScript, "
    "Go, Ruby, C\n"
    "  • 'Who wins the 2026 World Cup?' → France, Argentina, Brazil, "
    "Spain, England, Germany\n"
    "\n"
    "Only use exactly 2 options when the question is EXPLICITLY binary: "
    "it names two alternatives ('Sweden or Norway?', 'Coffee or tea?', "
    "'Should we A or B?'), or the answer space is truly boolean (yes/no, "
    "true/false). Do not force 2 options on an open-ended question — "
    "'What country is best?' deserves the real contenders, not just two.\n"
    "\n"
    "If the question already names candidates, KEEP THEM ALL. Do not answer "
    "the question yourself.\n"
    "\n"
    "Also set 2-4 short judging criteria tailored to the question — the "
    "yardsticks a fair judge would grade arguments against (e.g. 'evidence "
    "quality', 'recency of data', 'practical feasibility'). Fixing the "
    "criteria before the debate starts keeps the ruling honest."
)

PANELIST_PROMPT = (
    "You are one voice on the Rabble. You are given a question and a "
    "set of options. Pick exactly one option (by its exact label) and defend "
    "your choice in one or two sentences. Commit to a choice even if the "
    "question is silly or underspecified; if the premise is flawed, vote for "
    "the option that survives the flaw and say why. Be yourself — your "
    "reasoning will be quoted.\n\n"
    "You have two research tools: web_search(query) and browse(url). The "
    "framer already ran ONE preflight search on the user's question and "
    "the top hits are inlined above under 'Current web context'. FIRST "
    "read that context. If it already answers the question, skip the "
    "tools and just cite one of the sources it lists. Only call "
    "web_search yourself when the preflight context is missing a "
    "specific fact you need, and stop after one search + at most one "
    "browse — you're voting, not writing an essay.\n\n"
    "When you cite, inline the raw URL in your reasoning (e.g. 'per "
    "hdr.undp.org — https://hdr.undp.org/2025 …'). The UI renders "
    "http(s) URLs as clickable links. Do not invent URLs — only cite "
    "ones that came from the preflight context, a real search result, "
    "or a page you actually browsed."
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
    agent = Agent(model, output_type=Framing, system_prompt=FRAMER_PROMPT,
                   model_settings=FRAMER_SETTINGS)
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
    context: str = "",
    memory: RunMemory | None = None,
    round_index: int = 0,
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
                system_prompt=_preamble(context) + PANELIST_PROMPT,
                tools=make_tools(p.name, on_tool_call, memory=memory,
                                 round_index=round_index),
                model_settings=POLL_PANELIST_SETTINGS,
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
    agent = Agent(model, system_prompt=SUMMARIZER_PROMPT,
                   model_settings=POLL_SUMMARY_SETTINGS)
    async with agent.run_stream("\n".join(lines)) as result:
        async for delta in result.stream_text(delta=True):
            yield delta
