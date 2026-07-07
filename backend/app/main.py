"""Rabble backend — poll, debate, and stats endpoints.

  POST /agui         — original one-shot poll (SSE, AG-UI events)
  POST /agui/debate  — multi-round debate with tools (SSE, AG-UI events)
  POST /agui/news    — generate the current news edition (SSE, AG-UI events)
  GET  /news/latest  — cached news edition for the current slot
  GET  /panel        — panelist chips for the frontend picker
  GET  /stats/*      — leaderboard + recent-questions feed
  GET  /health       — liveness

Panelists have access to two tools in both modes: web_search and browse.
Tool calls are surfaced to the UI via CUSTOM tool_call events so the
transcript can show a badge under each ballot.
"""

import asyncio
import json
import logging
import os
import uuid
from collections import Counter
from contextlib import asynccontextmanager

logging.basicConfig(
    level=os.getenv("RABBLE_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from . import a2ui, agui, openrouter as orcatalog, store, tavily, parallel
from .tools import _context_fn, _search_client, _WEB_PROVIDER
from .memory import RunMemory
from .config import build_panel, framer_model, judge_model
from .debate import (
    DEBATE_ROUNDS,
    debate_round,
    initial_ballots,
    judge_verdict,
    stream_debate_summary,
)
from .guard import GuardError, client_ip, limiter, validate_question
from .news import SOURCES, fetch_headlines
from .newsroom import (
    assess_story,
    cluster_stories,
    current_slot,
    judge_story,
    measured_leans,
    rebuttal_round,
)
from .panel import cast_ballots, frame_question, stream_summary


PUBLIC_FEED = os.getenv("PUBLIC_FEED", "1") == "1"


log = logging.getLogger("rabble")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.init_db()
    if _search_client().available:
        log.info("web_tools_ok provider=%s panelists will have web_search + browse tools",
                 _WEB_PROVIDER)
    else:
        log.warning("web_tools_disabled no API key for %s — panelists will run without web tools",
                    _WEB_PROVIDER)
    yield


app = FastAPI(title="Rabble", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class InputMessage(BaseModel):
    role: str
    content: str


class RunAgentInput(BaseModel):
    """Subset of AG-UI's RunAgentInput (camelCase on the wire)."""
    model_config = ConfigDict(populate_by_name=True)

    thread_id: str = Field(alias="threadId", default_factory=lambda: str(uuid.uuid4()))
    run_id: str = Field(alias="runId", default_factory=lambda: str(uuid.uuid4()))
    messages: list[InputMessage] = []
    selected_models: list[str] = Field(
        alias="selectedModels", default_factory=list,
        description="Names of models the user selected. Empty = use full panel.",
    )


@app.get("/panel")
async def get_panel():
    panel = build_panel()
    catalog = await orcatalog.available_slugs()
    return [
        {
            "name": p.name,
            "provider": p.provider,
            "slug": p.slug,
            # `available` is True when the slug exists on OpenRouter today,
            # False when it definitely doesn't, and None when we couldn't
            # reach the catalog (fail open — treat as available client-side).
            "available": (p.slug in catalog) if catalog is not None else None,
        }
        for p in panel
    ]


async def _error_stream(body: RunAgentInput, message: str):
    yield agui.sse(agui.run_started(body.thread_id, body.run_id))
    yield agui.sse(agui.run_error(message))


import re as _re


def _normalize_vote(raw: str, options: list[str]) -> str | None:
    """Map a model's freeform vote back to one of the option labels.

    Handles common shapes models emit:
      "Denmark"              → "Denmark"          exact
      "C"                    → 3rd option         letter
      "Option C"             → 3rd option         labelled letter
      "C: Denmark"           → "Denmark"          letter prefix + label
      "C. Denmark"           → "Denmark"
      "Option C: Denmark"    → "Denmark"
      "**Denmark**"          → "Denmark"          markdown fluff
      "the answer is Denmark"→ "Denmark"          substring fallback
      "Denm"                 → "Denmark"          truncation fallback

    Returns None when the vote can't be mapped to any option — callers
    must treat that ballot as invalid rather than counting it.
    """
    stripped = raw.strip().strip("*_`\"' ")

    # 1. Exact case-insensitive match.
    lower = stripped.lower()
    for opt in options:
        if lower == opt.lower():
            return opt

    # 2. "Letter[: . - ) ] Label" — pull the label side out and match it.
    m = _re.match(
        r"^(?:option\s+)?([a-z])\s*[\.\:\)\-\]]\s*(.+)$",
        stripped,
        _re.IGNORECASE,
    )
    if m:
        rest = m.group(2).strip().strip("*_`\"' ")
        for opt in options:
            if rest.lower() == opt.lower():
                return opt
        idx = ord(m.group(1).upper()) - 65
        if 0 <= idx < len(options):
            return options[idx]

    # 3. Bare letter or "Option X".
    upper = stripped.upper()
    for idx, opt in enumerate(options):
        letter = chr(65 + idx)
        if upper == letter or upper.startswith(f"OPTION {letter}"):
            return opt

    # 4. Substring fallback: if exactly one option label appears inside the
    #    raw vote, use it. Prevents "the answer is Denmark" from being lost.
    matches = [opt for opt in options if opt.lower() in lower]
    if len(matches) == 1:
        return matches[0]

    # 5. Truncation fallback: the vote is a prefix/fragment of exactly one
    #    option ("Denm" → "Denmark").
    if len(stripped) >= 3:
        matches = [opt for opt in options if lower in opt.lower()]
        if len(matches) == 1:
            return matches[0]

    return None


def _winner(options: list[str], ballots: list[dict]) -> str | None:
    if not ballots:
        return None
    counts = Counter(b["vote"] for b in ballots)
    top = max(counts.values())
    # Deterministic tiebreak: original option order.
    for opt in options:
        if counts.get(opt) == top:
            return opt
    return None


async def _select_panel(body: RunAgentInput):
    full_panel = build_panel()
    catalog = await orcatalog.available_slugs()
    if catalog is not None:
        alive = [p for p in full_panel if p.slug in catalog]
        # Only apply the catalog filter if it still leaves at least one
        # panelist standing — otherwise something is off (stale catalog,
        # unusual private slugs) and we shouldn't lock the user out.
        if alive:
            full_panel = alive
    if body.selected_models:
        chosen = set(body.selected_models)
        panel = [p for p in full_panel if p.name in chosen]
        return panel or full_panel
    return full_panel


def _guard(body: RunAgentInput, request: Request) -> tuple[StreamingResponse | None, str]:
    ip = client_ip(request)
    allowed, reason = limiter.check(ip)
    if not allowed:
        return (
            StreamingResponse(
                _error_stream(body, reason),
                media_type="text/event-stream",
                status_code=429,
            ),
            "",
        )
    raw_question = next(
        (m.content for m in reversed(body.messages) if m.role == "user"), ""
    )
    try:
        question = validate_question(raw_question)
    except GuardError as e:
        return (
            StreamingResponse(
                _error_stream(body, e.reason),
                media_type="text/event-stream",
                status_code=400,
            ),
            "",
        )
    limiter.record(ip)
    return None, question


@app.post("/agui")
async def run_agent(body: RunAgentInput, request: Request) -> StreamingResponse:
    early, question = _guard(body, request)
    if early is not None:
        return early

    async def events():
        yield agui.sse(agui.run_started(body.thread_id, body.run_id))
        try:
            panel = await _select_panel(body)
            surface = f"poll-{body.run_id}"

            yield agui.sse(agui.step_started("frame_question"))
            framing = await frame_question(framer_model(panel), question)
            yield agui.sse(agui.step_finished("frame_question"))

            # Preflight: one web search on the question, injected into
            # every panelist's system prompt so nobody argues from stale
            # training data alone.
            yield agui.sse(agui.step_started("gather_context"))
            context = await _context_fn()(question)
            if context:
                yield agui.sse(agui.custom("tool_call", {
                    "agent": "framer", "tool": "web_search",
                    "query": question[:200]}))
            yield agui.sse(agui.step_finished("gather_context"))

            state = {
                "question": question,
                "options": framing.options,
                "ballots": [],
                "expected": len(panel),
                "done": False,
                "summary": "",
            }

            yield agui.sse(agui.custom(
                "a2ui", a2ui.begin_rendering(
                    surface, a2ui.poll_card_root(num_options=len(framing.options)))))
            yield agui.sse(agui.custom(
                "a2ui", a2ui.data_model_update(surface, a2ui.poll_data_model(state))))
            yield agui.sse(agui.state_snapshot(state))

            tool_events: asyncio.Queue = asyncio.Queue()

            async def on_tool_call(payload: dict) -> None:
                await tool_events.put(payload)

            yield agui.sse(agui.step_started("collect_ballots"))
            memory = RunMemory()
            pump = _pump(cast_ballots(panel, question, framing, on_tool_call,
                                       context, memory=memory))
            get_ballot = asyncio.create_task(pump.results_get())

            while True:
                get_tool = asyncio.create_task(tool_events.get())
                done, _ = await asyncio.wait(
                    {get_ballot, get_tool}, return_when=asyncio.FIRST_COMPLETED
                )
                if get_tool in done and get_ballot not in done:
                    payload = get_tool.result()
                    yield agui.sse(agui.custom("tool_call", payload))
                    continue
                get_tool.cancel()
                item = get_ballot.result()
                if item is _DONE:
                    break
                get_ballot = asyncio.create_task(pump.results_get())
                panelist, ballot = item
                if isinstance(ballot, Exception):
                    state["expected"] -= 1
                    yield agui.sse(agui.custom("panelist_error", {
                        "name": panelist.name, "error": str(ballot)[:200]}))
                    continue
                vote_label = _normalize_vote(ballot.vote, framing.options)
                if vote_label is None:
                    state["expected"] -= 1
                    yield agui.sse(agui.custom("panelist_error", {
                        "name": panelist.name,
                        "error": f"unparseable vote: {ballot.vote[:80]!r}"}))
                    continue
                state["ballots"].append({
                    "name": panelist.name,
                    "provider": panelist.provider,
                    "vote": vote_label,
                    "reasoning": ballot.reasoning,
                    "confidence": ballot.confidence,
                })
                yield agui.sse(agui.state_snapshot(state))
                yield agui.sse(agui.custom(
                    "a2ui",
                    a2ui.data_model_update(surface, a2ui.poll_data_model(state))))
            yield agui.sse(agui.step_finished("collect_ballots"))

            yield agui.sse(agui.step_started("summarize"))
            msg_id = str(uuid.uuid4())
            yield agui.sse(agui.text_start(msg_id))
            async for delta in stream_summary(framer_model(panel), state):
                state["summary"] += delta
                yield agui.sse(agui.text_content(msg_id, delta))
            yield agui.sse(agui.text_end(msg_id))
            yield agui.sse(agui.step_finished("summarize"))

            state["done"] = True
            yield agui.sse(agui.custom(
                "a2ui",
                a2ui.data_model_update(surface, a2ui.poll_data_model(state))))
            yield agui.sse(agui.state_snapshot(state))

            winner = _winner(framing.options, state["ballots"])
            try:
                await store.record_run(
                    thread_id=body.thread_id,
                    mode="poll",
                    question=question,
                    winner=winner,
                    ballots=[
                        {
                            "name": b["name"],
                            "provider": b["provider"],
                            "round_index": 0,
                            "vote": b["vote"],
                            "flipped_from": None,
                            "reasoning": b["reasoning"],
                            "confidence": b.get("confidence"),
                        }
                        for b in state["ballots"]
                    ],
                )
            except Exception:
                pass  # stats are best-effort

            yield agui.sse(agui.run_finished(body.thread_id, body.run_id))
        except Exception as exc:
            yield agui.sse(agui.run_error(str(exc)[:300]))

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Helpers for interleaving tool events with ballot arrivals ────────────────

class _Done:
    pass


_DONE = _Done()


class _AsyncPump:
    """Adapter that lets us call `results_get()` to await the next item
    from an async iterator, returning a sentinel when exhausted."""

    def __init__(self, agen):
        self._agen = agen
        self._exhausted = False

    async def results_get(self):
        if self._exhausted:
            return _DONE
        try:
            return await self._agen.__anext__()
        except StopAsyncIteration:
            self._exhausted = True
            return _DONE


def _pump(agen):
    """Wrap an async iterator so we can `await pump.results_get()`."""
    return _AsyncPump(agen)


@app.post("/agui/debate")
async def run_debate(body: RunAgentInput, request: Request) -> StreamingResponse:
    early, question = _guard(body, request)
    if early is not None:
        return early

    async def events():
        yield agui.sse(agui.run_started(body.thread_id, body.run_id))
        try:
            panel = await _select_panel(body)

            yield agui.sse(agui.step_started("frame_question"))
            framing = await frame_question(framer_model(panel), question)
            yield agui.sse(agui.step_finished("frame_question"))

            yield agui.sse(agui.step_started("gather_context"))
            context = await _context_fn()(question)
            if context:
                yield agui.sse(agui.custom("tool_call", {
                    "agent": "framer", "tool": "web_search",
                    "query": question[:200]}))
            yield agui.sse(agui.step_finished("gather_context"))

            state: dict = {
                "question": question,
                "options": framing.options,
                "criteria": framing.criteria,
                "rounds": [],
                "tally": {opt: 0 for opt in framing.options},
                "summary": "",
                "stopped_early": None,
                "judge": None,
                "done": False,
            }
            yield agui.sse(agui.state_snapshot(state))

            # Stable per-run pseudonyms: debate rounds are anonymized so
            # peers argue against positions, not brands. Real names are
            # restored before anything reaches state or the UI.
            aliases = {name: f"Panelist {i + 1}"
                       for i, name in enumerate(sorted(p.name for p in panel))}

            tool_events: asyncio.Queue = asyncio.Queue()

            # Consecutive failures per panelist; two in a row drops them
            # from later rounds so one dead provider can't stall every
            # remaining round for the full timeout.
            failures: dict[str, int] = {}
            dropped: set[str] = set()

            async def on_tool_call(payload: dict) -> None:
                await tool_events.put(payload)

            async def _run_round(index: int, iterator, dissenter: str | None = None):
                round_ballots: list[dict] = []
                state["rounds"].append({"index": index, "ballots": round_ballots})
                pump = _pump(iterator)
                yield agui.sse(agui.step_started(f"round_{index + 1}"))
                get_ballot = asyncio.create_task(pump.results_get())
                while True:
                    get_tool = asyncio.create_task(tool_events.get())
                    done, _ = await asyncio.wait(
                        {get_ballot, get_tool}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if get_tool in done and get_ballot not in done:
                        payload = get_tool.result()
                        payload = {"round": index, **payload}
                        yield agui.sse(agui.custom("tool_call", payload))
                        continue
                    get_tool.cancel()
                    item = get_ballot.result()
                    if item is _DONE:
                        break
                    get_ballot = asyncio.create_task(pump.results_get())
                    panelist, ballot = item
                    if isinstance(ballot, Exception):
                        failures[panelist.name] = failures.get(panelist.name, 0) + 1
                        yield agui.sse(agui.custom("panelist_error", {
                            "name": panelist.name, "round": index,
                            "error": str(ballot)[:200]}))
                        continue
                    failures.pop(panelist.name, None)
                    vote_label = _normalize_vote(ballot.vote, framing.options)
                    if vote_label is None:
                        yield agui.sse(agui.custom("panelist_error", {
                            "name": panelist.name, "round": index,
                            "error": f"unparseable vote: {ballot.vote[:80]!r}"}))
                        continue
                    prior = _prior_vote(state, panelist.name, index)
                    entry = {
                        "name": panelist.name,
                        "provider": panelist.provider,
                        "round": index,
                        "vote": vote_label,
                        "reasoning": ballot.reasoning,
                        # Round 0 uses the plain Ballot, which has no
                        # argument or steelman.
                        "argument": getattr(ballot, "argument", None),
                        "steelman": getattr(ballot, "steelman", None),
                        "confidence": ballot.confidence,
                        "role": "dissenter" if panelist.name == dissenter else None,
                        "flipped_from": prior if prior and prior != vote_label else None,
                    }
                    round_ballots.append(entry)
                    state["tally"] = _current_tally(state, framing.options)
                    yield agui.sse(agui.state_snapshot(state))
                yield agui.sse(agui.step_finished(f"round_{index + 1}"))

            # Round 0 — initial ballots
            memory = RunMemory()

            async for ev in _run_round(0, initial_ballots(panel, question, framing,
                                                          on_tool_call, context,
                                                          memory=memory)):
                yield ev

            # A unanimous opening ballot leaves nothing to debate.
            round0 = state["rounds"][0]["ballots"] if state["rounds"] else []
            if len(round0) >= 2 and len({b["vote"] for b in round0}) == 1:
                state["stopped_early"] = "unanimous on the opening ballot"
                yield agui.sse(agui.state_snapshot(state))

            # Rounds 1..N — persuasion. Each round sees the immediately
            # prior round for peer rebuttal, but memory keeps each
            # panelist's *own* history across every round.
            for r in range(1, DEBATE_ROUNDS + 1):
                if state["stopped_early"]:
                    break
                prior_round = state["rounds"][r - 1]["ballots"]
                if not prior_round:
                    break
                for p in panel:
                    if failures.get(p.name, 0) >= 2 and p.name not in dropped:
                        dropped.add(p.name)
                        yield agui.sse(agui.custom("panelist_error", {
                            "name": p.name, "round": r,
                            "error": "dropped for the rest of the debate "
                                     "after repeated failures"}))
                active = [p for p in panel if p.name not in dropped]
                if not active:
                    break
                all_prior = [b for rd in state["rounds"] for b in rd["ballots"]]
                # Rotating assigned devil's advocate: one panelist per round
                # must argue against the current leading option.
                dissenter = active[(r - 1) % len(active)].name
                async for ev in _run_round(
                    r, debate_round(active, question, framing, prior_round,
                                     on_tool_call, context,
                                     memory=memory, round_index=r,
                                     all_prior_ballots=all_prior,
                                     aliases=aliases, dissenter=dissenter),
                    dissenter=dissenter,
                ):
                    yield ev
                # A round where nobody flipped means positions are settled
                # — skip the remaining rounds.
                this_round = state["rounds"][-1]["ballots"]
                if (r < DEBATE_ROUNDS and this_round
                        and not any(b["flipped_from"] for b in this_round)):
                    state["stopped_early"] = (
                        f"no flips in round {r + 1} — positions are settled")
                    yield agui.sse(agui.state_snapshot(state))

            # Summary
            yield agui.sse(agui.step_started("summarize"))
            msg_id = str(uuid.uuid4())
            yield agui.sse(agui.text_start(msg_id))
            async for delta in stream_debate_summary(framer_model(panel), state):
                state["summary"] += delta
                yield agui.sse(agui.text_content(msg_id, delta))
            yield agui.sse(agui.text_end(msg_id))
            yield agui.sse(agui.step_finished("summarize"))

            # Judge's ruling — argument quality, not the tally. Best-effort:
            # a dead judge model never kills the run.
            yield agui.sse(agui.step_started("judge"))
            try:
                verdict = await judge_verdict(judge_model(panel), state,
                                              criteria=framing.criteria)
                state["judge"] = {
                    "verdict": _normalize_vote(verdict.winner, framing.options)
                               or verdict.winner,
                    "rationale": verdict.rationale,
                }
            except Exception:
                log.exception("judge_failed")
            yield agui.sse(agui.state_snapshot(state))
            yield agui.sse(agui.step_finished("judge"))

            state["done"] = True
            yield agui.sse(agui.state_snapshot(state))

            # Winner from each panelist's latest vote (matches the tally the
            # UI shows) — final-round-only would erase dropped panelists.
            tally = _current_tally(state, framing.options)
            winner = None
            if any(tally.values()):
                top = max(tally.values())
                winner = next(opt for opt in framing.options if tally[opt] == top)
            # Round-0 majority vs final winner vs judge verdict — the raw
            # material for the "does debate change outcomes" stat.
            round0_ballots = state["rounds"][0]["ballots"] if state["rounds"] else []
            round0_winner = _winner(framing.options, round0_ballots)
            judge_winner = state["judge"]["verdict"] if state["judge"] else None
            try:
                await store.record_run(
                    thread_id=body.thread_id,
                    mode="debate",
                    question=question,
                    winner=winner,
                    round0_winner=round0_winner,
                    judge_winner=judge_winner,
                    ballots=[
                        {
                            "name": b["name"],
                            "provider": b["provider"],
                            "round_index": b["round"],
                            "vote": b["vote"],
                            "flipped_from": b.get("flipped_from"),
                            "reasoning": b["reasoning"],
                            "confidence": b.get("confidence"),
                            "role": b.get("role"),
                        }
                        for r in state["rounds"] for b in r["ballots"]
                    ],
                )
            except Exception:
                pass

            yield agui.sse(agui.run_finished(body.thread_id, body.run_id))
        except Exception as exc:
            yield agui.sse(agui.run_error(str(exc)[:300]))

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _prior_vote(state: dict, name: str, round_index: int) -> str | None:
    if round_index == 0:
        return None
    for b in state["rounds"][round_index - 1]["ballots"]:
        if b["name"] == name:
            return b["vote"]
    return None


def _current_tally(state: dict, options: list[str]) -> dict:
    """Use each panelist's most recent vote across all rounds so far."""
    latest: dict[str, str] = {}
    for r in state["rounds"]:
        for b in r["ballots"]:
            latest[b["name"]] = b["vote"]
    counts = {opt: 0 for opt in options}
    for v in latest.values():
        if v in counts:
            counts[v] += 1
    return counts


# ── Stats endpoints ──────────────────────────────────────────────────────────

@app.get("/stats/leaderboard")
async def stats_leaderboard(days: int = 30):
    if days <= 0 or days > 3650:
        raise HTTPException(400, "days out of range")
    return await store.leaderboard(days=days)


@app.get("/stats/debate-delta")
async def stats_debate_delta(days: int = 30):
    """How often debate changed the outcome vs the round-0 majority, and
    how often the judge's quality verdict disagreed with the final tally."""
    if days <= 0 or days > 3650:
        raise HTTPException(400, "days out of range")
    return await store.debate_delta(days=days)


@app.get("/stats/questions")
async def stats_questions(limit: int = 50):
    if not PUBLIC_FEED:
        raise HTTPException(404, "Public feed disabled")
    if limit <= 0 or limit > 500:
        raise HTTPException(400, "limit out of range")
    return await store.recent_questions(limit=limit)


# ── News edition endpoints ───────────────────────────────────────────────────
# On-demand with cache: no scheduler. The first visitor after an edition
# boundary (09:00/18:00 Stockholm) claims the slot, generates the edition
# live over SSE, and the result is stored; everyone else reads the cache.

@app.get("/news/latest")
async def news_latest():
    slot = current_slot()
    row = await store.news_get(slot)
    if row and row["status"] == "done":
        return {"slot": slot, "status": "done",
                "finished_at": row["finished_at"],
                "edition": row["payload"], "previous": None}
    prev = await store.news_latest_done()
    return {
        "slot": slot,
        "status": row["status"] if row else "none",
        "finished_at": None,
        "edition": None,
        "previous": ({"slot": prev["slot"], "finished_at": prev["finished_at"],
                      "edition": prev["payload"]} if prev else None),
    }


@app.post("/agui/news")
async def run_news(body: RunAgentInput, request: Request) -> StreamingResponse:
    ip = client_ip(request)
    allowed, reason = limiter.check(ip)
    if not allowed:
        return StreamingResponse(_error_stream(body, reason),
                                 media_type="text/event-stream", status_code=429)
    slot = current_slot()
    if not await store.news_claim(slot):
        return StreamingResponse(
            _error_stream(body, "this edition is already generated or being "
                                "generated — reload the News tab"),
            media_type="text/event-stream", status_code=409)
    limiter.record(ip)

    async def events():
        yield agui.sse(agui.run_started(body.thread_id, body.run_id))
        state: dict = {
            "slot": slot,
            "outlets": {s.id: {"name": s.name, "stance": s.stance,
                               "paywalled": s.paywalled} for s in SOURCES},
            "sources": {},
            "source_errors": {},
            "stories": [],
            "blindspots": [],
            "done": False,
        }
        try:
            panel = await _select_panel(body)

            yield agui.sse(agui.step_started("fetch_headlines"))
            feeds = await fetch_headlines()
            state["sources"] = dict(Counter(it["source"] for it in feeds["items"]))
            state["source_errors"] = feeds["errors"]
            yield agui.sse(agui.state_snapshot(state))
            yield agui.sse(agui.step_finished("fetch_headlines"))
            if not feeds["items"]:
                raise RuntimeError("no headlines could be fetched from any source")

            yield agui.sse(agui.step_started("cluster_stories"))
            desk = await cluster_stories(framer_model(panel), feeds["items"])
            state["stories"] = [
                {"title": s["title"], "items": s["items"], "status": "pending",
                 "assessments": [], "rebuttals": [], "report": None, "leans": {}}
                for s in desk["stories"]
            ]
            state["blindspots"] = desk["blindspots"]
            yield agui.sse(agui.state_snapshot(state))
            yield agui.sse(agui.step_finished("cluster_stories"))
            if not state["stories"]:
                raise RuntimeError("the desk editor found no multi-source stories")

            aliases = {name: f"Panelist {i + 1}"
                       for i, name in enumerate(sorted(p.name for p in panel))}
            tool_events: asyncio.Queue = asyncio.Queue()

            async def on_tool_call(payload: dict) -> None:
                await tool_events.put(payload)

            async def _collect(iterator, sink: list, story_index: int):
                """Stream one panelist phase: interleave tool badges with
                results landing, append successful results to `sink`."""
                pump = _pump(iterator)
                get_result = asyncio.create_task(pump.results_get())
                while True:
                    get_tool = asyncio.create_task(tool_events.get())
                    done, _ = await asyncio.wait(
                        {get_result, get_tool}, return_when=asyncio.FIRST_COMPLETED)
                    if get_tool in done and get_result not in done:
                        yield agui.sse(agui.custom("tool_call", {
                            "story": story_index, **get_tool.result()}))
                        continue
                    get_tool.cancel()
                    item = get_result.result()
                    if item is _DONE:
                        break
                    get_result = asyncio.create_task(pump.results_get())
                    panelist, result = item
                    if isinstance(result, Exception):
                        yield agui.sse(agui.custom("panelist_error", {
                            "name": panelist.name, "story": story_index,
                            "error": str(result)[:200]}))
                        continue
                    sink.append({"name": panelist.name,
                                 "provider": panelist.provider,
                                 **result.model_dump()})
                    yield agui.sse(agui.state_snapshot(state))

            # Stories run sequentially — kind to tool-provider rate limits
            # and the event stream stays readable; panelists are parallel
            # within each story.
            for i, story in enumerate(state["stories"]):
                memory = RunMemory()
                src = {"title": story["title"], "items": story["items"]}

                story["status"] = "assessing"
                yield agui.sse(agui.step_started(f"story_{i + 1}_assess"))
                yield agui.sse(agui.state_snapshot(state))
                async for ev in _collect(
                        assess_story(panel, src, on_tool_call, memory),
                        story["assessments"], i):
                    yield ev
                yield agui.sse(agui.step_finished(f"story_{i + 1}_assess"))
                if not story["assessments"]:
                    story["status"] = "failed"
                    continue

                story["status"] = "rebuttal"
                yield agui.sse(agui.step_started(f"story_{i + 1}_rebuttal"))
                yield agui.sse(agui.state_snapshot(state))
                async for ev in _collect(
                        rebuttal_round(panel, src, story["assessments"],
                                       on_tool_call, memory, aliases=aliases),
                        story["rebuttals"], i):
                    yield ev
                yield agui.sse(agui.step_finished(f"story_{i + 1}_rebuttal"))

                # The judge reads each panelist's freshest assessment —
                # the rebuttal-round revision, or round 0 for a panelist
                # whose rebuttal failed.
                latest = {a["name"]: a for a in story["assessments"]}
                latest.update({r["name"]: r for r in story["rebuttals"]})
                final = list(latest.values())

                story["status"] = "judging"
                yield agui.sse(agui.step_started(f"story_{i + 1}_judge"))
                yield agui.sse(agui.state_snapshot(state))
                try:
                    report = await judge_story(judge_model(panel), src, final)
                    story["report"] = report.model_dump()
                except Exception:
                    log.exception("news_judge_failed story=%r", story["title"][:60])
                story["leans"] = measured_leans(final)
                story["status"] = "done" if story["report"] else "failed"
                yield agui.sse(agui.step_finished(f"story_{i + 1}_judge"))
                yield agui.sse(agui.state_snapshot(state))

            state["done"] = True
            await store.news_finish(slot, state)
            yield agui.sse(agui.state_snapshot(state))
            yield agui.sse(agui.run_finished(body.thread_id, body.run_id))
        except asyncio.CancelledError:
            # The triggering visitor closed the tab mid-run; free the slot
            # so the next visitor can regenerate immediately.
            await store.news_fail(slot, "generation interrupted")
            raise
        except Exception as exc:
            log.exception("news_run_failed slot=%r", slot)
            await store.news_fail(slot, str(exc)[:300])
            yield agui.sse(agui.run_error(str(exc)[:300]))

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
async def health():
    return {"ok": True}


# Serve the built frontend as static files (production)
frontend = os.path.join(os.path.dirname(__file__), "../../frontend/dist")
if os.path.isdir(frontend):
    app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
