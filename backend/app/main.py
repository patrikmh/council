"""AI Council backend — one AG-UI endpoint.

POST /agui takes an AG-UI RunAgentInput and streams AG-UI events back as
SSE. The poll card itself travels inside CUSTOM events as A2UI surface
messages; live vote counts travel as STATE_SNAPSHOT + dataModelUpdate.

Event choreography per run:
  RUN_STARTED
  STEP frame_question        -> derive A/B options
  CUSTOM a2ui beginRendering -> mount the (empty) poll card
  STEP collect_ballots       -> per ballot: STATE_SNAPSHOT + dataModelUpdate
  STEP summarize             -> TEXT_MESSAGE_* streamed into the card's
                                data model and the chat transcript
  CUSTOM a2ui dataModelUpdate (final, with winner flags)
  RUN_FINISHED
"""

import json
import uuid

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from . import a2ui, agui
from .config import build_panel, framer_model
from .panel import cast_ballots, frame_question, stream_summary

app = FastAPI(title="AI Council")
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
    """Return the available panelists so the frontend can render a picker."""
    panel = build_panel()
    return [
        {"name": p.name, "provider": p.provider}
        for p in panel
    ]


@app.post("/agui")
async def run_agent(body: RunAgentInput) -> StreamingResponse:
    question = next(
        (m.content for m in reversed(body.messages) if m.role == "user"), ""
    ).strip()

    async def events():
        yield agui.sse(agui.run_started(body.thread_id, body.run_id))
        try:
            if not question:
                yield agui.sse(agui.run_error("No user question in messages."))
                return

            full_panel = build_panel()
            # Filter to user-selected models, or use the full panel
            if body.selected_models:
                selected_set = set(body.selected_models)
                panel = [p for p in full_panel if p.name in selected_set]
                if not panel:
                    panel = full_panel
            else:
                panel = full_panel
            surface = f"poll-{body.run_id}"

            # 1. Frame the question into two options
            yield agui.sse(agui.step_started("frame_question"))
            framing = await frame_question(framer_model(panel), question)
            yield agui.sse(agui.step_finished("frame_question"))

            state = {
                "question": question,
                "option_a": framing.option_a,
                "option_b": framing.option_b,
                "ballots": [],
                "expected": len(panel),
                "done": False,
                "summary": "",
            }

            # 2. Mount the A2UI poll surface, then seed its data model
            yield agui.sse(agui.custom(
                "a2ui", a2ui.begin_rendering(surface, a2ui.poll_card_root())))
            yield agui.sse(agui.custom(
                "a2ui", a2ui.data_model_update(surface, a2ui.poll_data_model(state))))
            yield agui.sse(agui.state_snapshot(state))

            # 3. Ballots land as each model finishes
            yield agui.sse(agui.step_started("collect_ballots"))
            async for panelist, ballot in cast_ballots(panel, question, framing):
                if isinstance(ballot, Exception):
                    state["expected"] -= 1
                    yield agui.sse(agui.custom("panelist_error", {
                        "name": panelist.name, "error": str(ballot)[:200]}))
                    continue
                state["ballots"].append({
                    "name": panelist.name,
                    "provider": panelist.provider,
                    "vote": ballot.vote,
                    "reasoning": ballot.reasoning,
                })
                yield agui.sse(agui.state_snapshot(state))
                yield agui.sse(agui.custom(
                    "a2ui",
                    a2ui.data_model_update(surface, a2ui.poll_data_model(state))))
            yield agui.sse(agui.step_finished("collect_ballots"))

            # 4. Stream the summary as a chat message + into the card
            yield agui.sse(agui.step_started("summarize"))
            msg_id = str(uuid.uuid4())
            yield agui.sse(agui.text_start(msg_id))
            async for delta in stream_summary(framer_model(panel), state):
                state["summary"] += delta
                yield agui.sse(agui.text_content(msg_id, delta))
            yield agui.sse(agui.text_end(msg_id))
            yield agui.sse(agui.step_finished("summarize"))

            # 5. Final card with winner flags lit
            state["done"] = True
            yield agui.sse(agui.custom(
                "a2ui",
                a2ui.data_model_update(surface, a2ui.poll_data_model(state))))
            yield agui.sse(agui.state_snapshot(state))
            yield agui.sse(agui.run_finished(body.thread_id, body.run_id))

        except Exception as exc:
            yield agui.sse(agui.run_error(str(exc)[:300]))

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
async def health():
    return {"ok": True}
