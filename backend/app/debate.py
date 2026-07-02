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

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from .config import Panelist
from .memory import RunMemory
from .panel import Ballot, Framing, PANELIST_TIMEOUT, _preamble, cast_ballots
from .tools import make_tools, OnToolCall

log = logging.getLogger("rabble")

DEBATE_ROUNDS = int(os.getenv("DEBATE_ROUNDS", "2"))


class DebateBallot(BaseModel):
    """Persuasion-round ballot: a full argument peers can rebut, plus the
    one-liner the UI chips quote. Round 0 keeps the plain `Ballot`."""

    vote: str = Field(
        description=(
            "The label of the option you chose, exactly as written in the "
            "prompt (case-sensitive)."
        )
    )
    argument: str = Field(
        description=(
            "Your case, three to six sentences: rebut or endorse specific "
            "panelists by name and cite evidence with inline raw URLs."
        )
    )
    reasoning: str = Field(description="One-sentence summary of your argument.")


# Opener for a panelist who cast a ballot in an earlier round.
_OPENER_RETURNING = (
    "You are one voice on the Rabble in a persuasion round. You already "
    "voted once. You will now see how the other panelists voted and what "
    "they said. You may HOLD your vote or FLIP to a different option — "
    "whichever is honest. In your reasoning, address specific panelists "
    "by name when you rebut or agree with them.\n\n"
)

# Opener for a panelist whose earlier rounds all failed (timeout/error) —
# they have no prior ballot, so "you already voted" would be a lie.
_OPENER_JOINING = (
    "You are one voice on the Rabble joining a persuasion round. You have "
    "not yet cast a ballot — the debate is already underway. Read what the "
    "other panelists said and cast your first vote. In your reasoning, "
    "address specific panelists by name when you rebut or agree with "
    "them.\n\n"
)

_DEBATE_TAIL = (
    "You have two research tools: web_search(query) returns real search "
    "results, and browse(url) fetches the readable text of a page. USE "
    "THEM aggressively this round — if another panelist made a factual "
    "claim (a ranking, a statistic, a date, a quote), verify it before "
    "agreeing or rebutting.\n\n"
    "When you cite a source, INLINE the raw URL in your reasoning (e.g. "
    "'Norway leads the HDI 2025 — see https://hdr.undp.org/2025'). The UI "
    "renders any http(s) URL as a clickable link, so a bare URL becomes a "
    "proper citation. Do not invent URLs — only cite ones you actually "
    "browsed or that came back from a search. Make your argument three to "
    "six substantive sentences; the one-sentence reasoning is just the "
    "headline."
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
        # Full argument when there is one (round 1+), capped so five
        # panelists' essays can't blow up everyone's round-N context.
        case = b.get("argument") or b["reasoning"]
        lines.append(f"- {b['name']} voted {b['vote']}: {case[:700]}")
    return "\n".join(lines)


async def initial_ballots(
    panel: list[Panelist],
    question: str,
    framing: Framing,
    on_tool_call: OnToolCall | None = None,
    context: str = "",
    memory: RunMemory | None = None,
) -> AsyncIterator[tuple[Panelist, Ballot | Exception]]:
    """Round 0 — identical to poll mode. Delegated to cast_ballots."""
    async for item in cast_ballots(panel, question, framing, on_tool_call,
                                    context, memory=memory, round_index=0):
        yield item


async def debate_round(
    panel: list[Panelist],
    question: str,
    framing: Framing,
    prior_round: list[dict],
    on_tool_call: OnToolCall | None = None,
    context: str = "",
    memory: RunMemory | None = None,
    round_index: int = 1,
    all_prior_ballots: list[dict] | None = None,
) -> AsyncIterator[tuple[Panelist, DebateBallot | Exception]]:
    """One persuasion round. Every panelist sees the prior round's votes."""
    options_text = _options_text(framing)
    all_prior = all_prior_ballots or prior_round

    async def one(p: Panelist) -> tuple[Panelist, DebateBallot | Exception]:
        t0 = time.monotonic()
        log.info("debate_start name=%r timeout=%.1fs", p.name, PANELIST_TIMEOUT)
        try:
            others = _render_others(prior_round, p.name)
            my_history = memory.agent_history(p.name, all_prior) if memory else ""
            parts = [f"Question: {question}\n{options_text}"]
            if my_history:
                parts.append(my_history)
            parts.append(others)
            board = memory.evidence_board() if memory else ""
            if board:
                parts.append(board)
            parts.append("Now cast your ballot for this round.")
            prompt = "\n\n".join(parts)
            has_voted = any(b["name"] == p.name for b in all_prior)
            opener = _OPENER_RETURNING if has_voted else _OPENER_JOINING
            agent = Agent(
                p.model,
                output_type=DebateBallot,
                system_prompt=_preamble(context) + opener + _DEBATE_TAIL,
                tools=make_tools(p.name, on_tool_call, memory=memory,
                                 round_index=round_index),
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
    "between AI models. Rules:\n"
    "  • TWO sentences. Absolute max three. This is a headline, not an essay.\n"
    "  • Dry, wry, one clean observation — no throat-clearing.\n"
    "  • Name at least one model. If someone flipped, that is the story.\n"
    "  • If everyone agreed the whole way, say so bluntly — do not pad.\n"
    "  • No heading like 'Rabble Minutes', no lists, no bold, no quotes "
    "longer than four words.\n"
    "Write it as plain prose. Ship it."
)


def _transcript_lines(state: dict, full: bool = False) -> list[str]:
    """Render the debate as plain lines. ``full`` swaps the one-line
    reasoning for the whole argument — the judge needs the case, the
    two-sentence summarizer doesn't."""
    lines = [f"Question: {state['question']}",
             f"Options: {' | '.join(state['options'])}"]
    for r in state["rounds"]:
        lines.append(f"\nRound {r['index'] + 1}:")
        for b in r["ballots"]:
            flip = ""
            if b.get("flipped_from"):
                flip = f" (flipped from {b['flipped_from']})"
            case = (b.get("argument") or b["reasoning"]) if full else b["reasoning"]
            lines.append(f"- {b['name']} voted {b['vote']}{flip}: {case}")
    return lines


async def stream_debate_summary(model, state: dict) -> AsyncIterator[str]:
    agent = Agent(model, system_prompt=DEBATE_SUMMARY_PROMPT)
    async with agent.run_stream("\n".join(_transcript_lines(state))) as result:
        async for delta in result.stream_text(delta=True):
            yield delta


JUDGE_PROMPT = (
    "You judge a debate between AI panelists. Do NOT count votes — weigh "
    "argument QUALITY only: evidence cited, claims verified, rebuttals "
    "actually answered. The best-argued option may be the tally loser. "
    "Name the winning option exactly as written in the Options line and "
    "explain your ruling in two or three sentences."
)


class Verdict(BaseModel):
    winner: str = Field(
        description=(
            "The option label with the best-argued case, exactly as "
            "written in the Options line."
        )
    )
    rationale: str = Field(
        description="Two or three sentences: why that case was strongest."
    )


async def judge_verdict(model, state: dict) -> Verdict:
    """Rule on argument quality after the final round. Panelist names are
    anonymized so the judge can't grade on brand, then swapped back into
    the rationale so the UI never shows 'Panelist 3'."""
    text = "\n".join(_transcript_lines(state, full=True))
    names = sorted({b["name"] for r in state["rounds"] for b in r["ballots"]})
    for i, name in enumerate(names):
        text = text.replace(name, f"Panelist {i + 1}")
    agent = Agent(model, output_type=Verdict, system_prompt=JUDGE_PROMPT)
    result = await agent.run(text)
    rationale = result.output.rationale
    # Reverse order so "Panelist 12" is restored before "Panelist 1".
    for i in reversed(range(len(names))):
        rationale = rationale.replace(f"Panelist {i + 1}", names[i])
    return Verdict(winner=result.output.winner, rationale=rationale)
