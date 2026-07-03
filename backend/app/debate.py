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
import random
import time
from collections import Counter
from typing import AsyncIterator

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings

from .config import Panelist
from .memory import RunMemory
from .panel import Ballot, Framing, PANELIST_TIMEOUT, _preamble, cast_ballots
from .tools import make_tools, OnToolCall

log = logging.getLogger("rabble")

DEBATE_ROUNDS = int(os.getenv("DEBATE_ROUNDS", "2"))

# ── Model settings per role (cap tokens to save cost) ──────────────────────
# Output tokens: what the model generates as visible text.
PANELIST_MAX_TOKENS = int(os.getenv("DEBATE_PANELIST_MAX_TOKENS", "768"))
JUDGE_MAX_TOKENS = int(os.getenv("DEBATE_JUDGE_MAX_TOKENS", "1024"))
SUMMARY_MAX_TOKENS = int(os.getenv("DEBATE_SUMMARY_MAX_TOKENS", "128"))

# Reasoning tokens: internal chain-of-thought before the visible output.
# Reasoning models (Grok 4, GPT-5, Claude Sonnet 5, GLM 5) can burn
# thousands of thinking tokens — these caps keep cost predictable.
# medium effort lets models engage with arguments and browse for evidence;
# the token budget stops runaway reasoning spirals.
REASONING_EFFORT = os.getenv("DEBATE_REASONING_EFFORT", "medium")  # minimal|low|medium|high
REASONING_TOKEN_BUDGET = int(os.getenv("DEBATE_REASONING_TOKEN_BUDGET", "8192"))


def _panelist_settings() -> ModelSettings:
    return ModelSettings(
        max_tokens=PANELIST_MAX_TOKENS,
        thinking=REASONING_EFFORT,
        extra_body={"reasoning": {"max_tokens": REASONING_TOKEN_BUDGET}},
    )


def _judge_settings() -> ModelSettings:
    return ModelSettings(
        max_tokens=JUDGE_MAX_TOKENS,
        thinking=REASONING_EFFORT,
        extra_body={"reasoning": {"max_tokens": REASONING_TOKEN_BUDGET}},
    )


def _summary_settings() -> ModelSettings:
    return ModelSettings(
        max_tokens=SUMMARY_MAX_TOKENS,
        thinking=REASONING_EFFORT,
        extra_body={"reasoning": {"max_tokens": REASONING_TOKEN_BUDGET}},
    )


PANELIST_SETTINGS = _panelist_settings()
JUDGE_SETTINGS = _judge_settings()
SUMMARY_SETTINGS = _summary_settings()


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
    steelman: str = Field(
        description=(
            "One sentence: the strongest argument AGAINST the option you "
            "chose. Be honest — a weak steelman signals a weak position."
        )
    )
    confidence: int = Field(
        ge=0, le=100,
        description="Your confidence that your chosen option is correct, 0-100.",
    )


_ANTI_HERDING = (
    "Flip only because an argument is genuinely stronger, never because "
    "more panelists hold a position — popularity is not evidence. Equally, "
    "do not hold out of stubbornness when you have been out-argued.\n\n"
)

# Opener for a panelist who cast a ballot in an earlier round.
_OPENER_RETURNING = (
    "You are one voice on the Rabble in a persuasion round. You already "
    "voted once. You will now see how the other panelists voted and what "
    "they said. You may HOLD your vote or FLIP to a different option — "
    "whichever is honest. In your reasoning, address specific panelists "
    "by name when you rebut or agree with them.\n\n"
) + _ANTI_HERDING

# Opener for a panelist whose earlier rounds all failed (timeout/error) —
# they have no prior ballot, so "you already voted" would be a lie.
_OPENER_JOINING = (
    "You are one voice on the Rabble joining a persuasion round. You have "
    "not yet cast a ballot — the debate is already underway. Read what the "
    "other panelists said and cast your first vote. In your reasoning, "
    "address specific panelists by name when you rebut or agree with "
    "them.\n\n"
) + _ANTI_HERDING

# The panel is anonymized during debate rounds so arguments are weighed on
# merit, not on which lab's model said them. Real names are restored before
# anything reaches the UI.
_ANON_NOTE = (
    "Panelists appear under neutral pseudonyms (Panelist 1, Panelist 2, …) "
    "so arguments are judged on merit, not brand. To the others you are "
    "{alias}. Refer to panelists by their pseudonym.\n\n"
)

# Rotating assigned dissent: research shows an assigned devil's-advocate
# role produces real scrutiny where a polite "feel free to disagree" does
# nothing. One panelist per round gets this addendum.
_DISSENT_ROLE = (
    "ASSIGNED ROLE this round: devil's advocate. '{leader}' currently "
    "leads the tally. In your argument, make the strongest evidence-backed "
    "case AGAINST '{leader}' — attack its weakest specific claim. Your "
    "vote must still be your honest one; the role shapes your argument, "
    "not your ballot.\n\n"
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


def _map_names(text: str, mapping: dict[str, str]) -> str:
    """Replace every key in `text` with its value, longest keys first so
    'Panelist 12' is rewritten before 'Panelist 1' (and 'GPT-5 Mini'
    before 'GPT-5')."""
    for src in sorted(mapping, key=len, reverse=True):
        text = text.replace(src, mapping[src])
    return text


def _render_others(
    prior_round: list[dict], self_name: str, aliases: dict[str, str]
) -> str:
    lines = ["What the others said in the previous round:"]
    for b in prior_round:
        if b["name"] == self_name:
            continue
        # Full argument when there is one (round 1+), capped so five
        # panelists' essays can't blow up everyone's round-N context.
        # Stored arguments carry real names (the UI wants them), so map
        # them back to pseudonyms before showing them to a peer.
        case = _map_names(b.get("argument") or b["reasoning"], aliases)
        lines.append(f"- {aliases.get(b['name'], b['name'])} voted {b['vote']}: {case[:700]}")
    return "\n".join(lines)


def _leading_option(prior_round: list[dict]) -> str | None:
    votes = Counter(b["vote"] for b in prior_round)
    return votes.most_common(1)[0][0] if votes else None


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
    aliases: dict[str, str] | None = None,
    dissenter: str | None = None,
) -> AsyncIterator[tuple[Panelist, DebateBallot | Exception]]:
    """One persuasion round. Every panelist sees the prior round's votes,
    attributed to stable pseudonyms so brand names can't drive flips;
    pseudonyms in each ballot are mapped back to real names before the
    ballot is yielded. `dissenter` names the panelist assigned to argue
    against the current leading option this round."""
    options_text = _options_text(framing)
    all_prior = all_prior_ballots or prior_round
    aliases = aliases or {}
    real_names = {alias: name for name, alias in aliases.items()}
    leader = _leading_option(prior_round)

    async def one(p: Panelist) -> tuple[Panelist, DebateBallot | Exception]:
        t0 = time.monotonic()
        log.info("debate_start name=%r timeout=%.1fs", p.name, PANELIST_TIMEOUT)
        try:
            others = _render_others(prior_round, p.name, aliases)
            my_history = memory.agent_history(p.name, all_prior,
                                              aliases=aliases) if memory else ""
            parts = [f"Question: {question}\n{options_text}"]
            if framing.criteria:
                parts.append("Arguments are judged against these pre-agreed "
                             "criteria: " + "; ".join(framing.criteria) + ".")
            if my_history:
                parts.append(my_history)
            parts.append(others)
            board = memory.evidence_board(aliases=aliases) if memory else ""
            if board:
                parts.append(board)
            parts.append("Now cast your ballot for this round.")
            prompt = "\n\n".join(parts)
            has_voted = any(b["name"] == p.name for b in all_prior)
            opener = _OPENER_RETURNING if has_voted else _OPENER_JOINING
            if p.name in aliases:
                opener += _ANON_NOTE.format(alias=aliases[p.name])
            if p.name == dissenter and leader:
                opener += _DISSENT_ROLE.format(leader=leader)
            agent = Agent(
                p.model,
                output_type=DebateBallot,
                system_prompt=_preamble(context) + opener + _DEBATE_TAIL,
                tools=make_tools(p.name, on_tool_call, memory=memory,
                                 round_index=round_index),
                model_settings=PANELIST_SETTINGS,
            )
            result = await asyncio.wait_for(agent.run(prompt), timeout=PANELIST_TIMEOUT)
            ballot = result.output
            # Restore real names for the UI and the stored transcript.
            ballot = ballot.model_copy(update={
                "argument": _map_names(ballot.argument, real_names),
                "reasoning": _map_names(ballot.reasoning, real_names),
                "steelman": _map_names(ballot.steelman, real_names),
            })
            log.info("debate_done  name=%r vote=%r elapsed=%.1fs",
                     p.name, ballot.vote, time.monotonic() - t0)
            return p, ballot
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


def _transcript_lines(
    state: dict, full: bool = False, options: list[str] | None = None
) -> list[str]:
    """Render the debate as plain lines. ``full`` swaps the one-line
    reasoning for the whole argument — the judge needs the case, the
    two-sentence summarizer doesn't. ``options`` overrides the order of
    the Options line (the judge shuffles it to wash out order bias)."""
    lines = [f"Question: {state['question']}",
             f"Options: {' | '.join(options or state['options'])}"]
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
    agent = Agent(model, system_prompt=DEBATE_SUMMARY_PROMPT,
                   model_settings=SUMMARY_SETTINGS)
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

JUDGE_SAMPLES = int(os.getenv("JUDGE_SAMPLES", "3"))


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


def _judge_prompt(criteria: list[str] | None) -> str:
    if not criteria:
        return JUDGE_PROMPT
    return JUDGE_PROMPT + (
        "\nGrade against these criteria, fixed before the debate started: "
        + "; ".join(criteria) + "."
    )


async def _judge_sample(model, state: dict, criteria: list[str] | None) -> Verdict:
    """One judge pass over the transcript with panelist pseudonyms and the
    Options line order both freshly shuffled, so neither brand nor
    position can systematically tilt the ruling."""
    names = sorted({b["name"] for r in state["rounds"] for b in r["ballots"]})
    numbers = random.sample(range(1, len(names) + 1), len(names))
    aliases = {name: f"Panelist {n}" for name, n in zip(names, numbers)}
    options = random.sample(state["options"], len(state["options"]))
    text = "\n".join(_transcript_lines(state, full=True, options=options))
    text = _map_names(text, aliases)
    agent = Agent(model, output_type=Verdict, system_prompt=_judge_prompt(criteria),
                   model_settings=JUDGE_SETTINGS)
    result = await agent.run(text)
    real_names = {alias: name for name, alias in aliases.items()}
    return Verdict(
        winner=result.output.winner,
        rationale=_map_names(result.output.rationale, real_names),
    )


async def judge_verdict(model, state: dict, criteria: list[str] | None = None) -> Verdict:
    """Rule on argument quality after the final round. Runs JUDGE_SAMPLES
    independent passes and takes the majority winner; a tie falls back to
    the first sample. Failed samples are ignored unless all fail."""
    samples = await asyncio.gather(
        *(_judge_sample(model, state, criteria) for _ in range(JUDGE_SAMPLES)),
        return_exceptions=True,
    )
    verdicts = [s for s in samples if isinstance(s, Verdict)]
    if not verdicts:
        raise next(s for s in samples if isinstance(s, Exception))
    counts = Counter(v.winner.strip() for v in verdicts)
    top_winner = counts.most_common(1)[0][0]
    return next(v for v in verdicts if v.winner.strip() == top_winner)
