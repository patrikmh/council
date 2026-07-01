"""Rabble backend — poll, debate, and stats endpoints.

  POST /agui         — original one-shot poll (SSE, AG-UI events)
  POST /agui/debate  — multi-round debate with tools (SSE, AG-UI events)
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

from . import a2ui, agui, openrouter as orcatalog, store, tavily
from .config import build_panel, framer_model
from .debate import (
    DEBATE_ROUNDS,
    debate_round,
    initial_ballots,
    stream_debate_summary,
)
from .guard import GuardError, client_ip, limiter, validate_question
from .panel import cast_ballots, frame_question, stream_summary


PUBLIC_FEED = os.getenv("PUBLIC_FEED", "1") == "1"


log = logging.getLogger("rabble")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.init_db()
    if tavily.client.available:
        log.info("tavily_ok panelists will have web_search + browse tools")
    else:
        log.warning("tavily_disabled TAVILY_API_KEY not set — panelists will run without web tools")
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


def _normalize_vote(raw: str, options: list[str]) -> str:
    """Map a model's freeform vote back to one of the option labels."""
    stripped = raw.strip()
    for opt in options:
        if stripped.lower() == opt.lower():
            return opt
    for idx, opt in enumerate(options):
        letter = chr(65 + idx)
        u = stripped.upper()
        if u == letter or u.startswith(f"OPTION {letter}"):
            return opt
    return raw


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
            pump = _pump(cast_ballots(panel, question, framing, on_tool_call))
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
                state["ballots"].append({
                    "name": panelist.name,
                    "provider": panelist.provider,
                    "vote": vote_label,
                    "reasoning": ballot.reasoning,
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

            state: dict = {
                "question": question,
                "options": framing.options,
                "rounds": [],
                "tally": {opt: 0 for opt in framing.options},
                "summary": "",
                "done": False,
            }
            yield agui.sse(agui.state_snapshot(state))

            tool_events: asyncio.Queue = asyncio.Queue()

            async def on_tool_call(payload: dict) -> None:
                await tool_events.put(payload)

            async def _run_round(index: int, iterator):
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
                        yield agui.sse(agui.custom("panelist_error", {
                            "name": panelist.name, "round": index,
                            "error": str(ballot)[:200]}))
                        continue
                    vote_label = _normalize_vote(ballot.vote, framing.options)
                    prior = _prior_vote(state, panelist.name, index)
                    entry = {
                        "name": panelist.name,
                        "provider": panelist.provider,
                        "round": index,
                        "vote": vote_label,
                        "reasoning": ballot.reasoning,
                        "flipped_from": prior if prior and prior != vote_label else None,
                    }
                    round_ballots.append(entry)
                    state["tally"] = _current_tally(state, framing.options)
                    yield agui.sse(agui.state_snapshot(state))
                yield agui.sse(agui.step_finished(f"round_{index + 1}"))

            # Round 0 — initial ballots
            async for ev in _run_round(0, initial_ballots(panel, question, framing, on_tool_call)):
                yield ev

            # Rounds 1..N — persuasion
            for r in range(1, DEBATE_ROUNDS + 1):
                prior_round = state["rounds"][r - 1]["ballots"]
                if not prior_round:
                    break
                async for ev in _run_round(
                    r, debate_round(panel, question, framing, prior_round, on_tool_call)
                ):
                    yield ev

            # Summary
            yield agui.sse(agui.step_started("summarize"))
            msg_id = str(uuid.uuid4())
            yield agui.sse(agui.text_start(msg_id))
            async for delta in stream_debate_summary(framer_model(panel), state):
                state["summary"] += delta
                yield agui.sse(agui.text_content(msg_id, delta))
            yield agui.sse(agui.text_end(msg_id))
            yield agui.sse(agui.step_finished("summarize"))

            state["done"] = True
            yield agui.sse(agui.state_snapshot(state))

            final_round = state["rounds"][-1]["ballots"] if state["rounds"] else []
            winner = _winner(framing.options, final_round)
            try:
                await store.record_run(
                    thread_id=body.thread_id,
                    mode="debate",
                    question=question,
                    winner=winner,
                    ballots=[
                        {
                            "name": b["name"],
                            "provider": b["provider"],
                            "round_index": b["round"],
                            "vote": b["vote"],
                            "flipped_from": b.get("flipped_from"),
                            "reasoning": b["reasoning"],
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
        counts[v] = counts.get(v, 0) + 1
    return counts


# ── Stats endpoints ──────────────────────────────────────────────────────────

@app.get("/stats/leaderboard")
async def stats_leaderboard(days: int = 30):
    if days <= 0 or days > 3650:
        raise HTTPException(400, "days out of range")
    return await store.leaderboard(days=days)


@app.get("/stats/questions")
async def stats_questions(limit: int = 50):
    if not PUBLIC_FEED:
        raise HTTPException(404, "Public feed disabled")
    if limit <= 0 or limit > 500:
        raise HTTPException(400, "limit out of range")
    return await store.recent_questions(limit=limit)


@app.get("/health")
async def health():
    return {"ok": True}


# Serve the built frontend as static files (production)
frontend = os.path.join(os.path.dirname(__file__), "../../frontend/dist")
if os.path.isdir(frontend):
    app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
