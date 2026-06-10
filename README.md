# AI Council

A clone of the "AI Council" poll apps: one question goes to a panel of
models, each casts a vote with a one-liner of reasoning, the UI shows a live
poll card, and one model writes the dry summary at the end.

Built on three layers:

- **AG-UI** — the agent↔UI transport. The backend streams AG-UI events
  (`RUN_STARTED`, `STEP_*`, `TEXT_MESSAGE_*`, `STATE_SNAPSHOT`, `CUSTOM`,
  `RUN_FINISHED`) over SSE from a single `POST /agui` endpoint. Hand-rolled
  in `backend/app/agui.py` so the wire format is fully visible.
- **A2UI** — the declarative UI layer. The agent never sends markup; it
  sends `beginRendering` (a component tree with `{path: ...}` bindings) and
  `dataModelUpdate` messages inside AG-UI `CUSTOM` events. The client owns
  the widget library (`frontend/src/A2UIRenderer.jsx`). Note: this is an
  A2UI-*inspired* subset — nested trees instead of the spec's flat
  adjacency list, because it's easier to read and debug.
- **Pydantic AI** — the agents. A framer turns the question into a binary
  A/B motion, panelists vote in parallel (`asyncio.as_completed`, so ballots
  stream in as each model finishes), and a summarizer streams the minutes.

## Run choreography

```
RUN_STARTED
STEP frame_question          → cheap model derives Option A / Option B
CUSTOM a2ui beginRendering   → poll card mounts (empty)
STEP collect_ballots         → per ballot: STATE_SNAPSHOT + dataModelUpdate
                               (division bars fill live, chips stamp in)
STEP summarize               → TEXT_MESSAGE_* streamed into chat
CUSTOM a2ui dataModelUpdate  → final card, winner lamp lit
RUN_FINISHED
```

A failed panelist emits `CUSTOM panelist_error` and is dropped from the
expected count — the run survives a dead provider.

## The panel

Configured entirely through OpenRouter: one `OPENROUTER_API_KEY`, and the
panel is a comma-separated list of model slugs in `ROUNDTABLE_PANEL`
(browse them at https://openrouter.ai/models). Display names are derived
from the slug, or override inline:

```
ROUNDTABLE_PANEL=anthropic/claude-sonnet-4.6,openai/gpt-5.2,google/gemini-3.1-pro,z-ai/glm-5.1|GLM-5.1,deepseek/deepseek-v3.2|DeepSeek V3.2,x-ai/grok-4.3
```

Adding a sixth model to the table is a one-line env change, no code. The
framer/summarizer defaults to Haiku via OpenRouter (`FRAMER_MODEL` to
override). Hover a chip in the UI to read that model's reasoning.

## Running it

Backend:

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example .env   # add your OpenRouter key, pick your panel, then:
set -a; source .env; set +a
uvicorn app.main:app --reload --port 8000
```

Frontend (Vite proxies `/agui` to :8000):

```bash
cd frontend
npm install
npm run dev   # http://localhost:5173
```

Ask it the canonical question: *"I want to wash my car. The car wash is
50 meters away. Should I walk or drive?"*

## Where to take it

- Replace snapshots with JSON Patch `STATE_DELTA`s if state grows.
- Add a debate round: panelists see each other's ballots and may flip.
- Swap the hand-rolled AG-UI layer for `pydantic-ai`'s native AG-UI app
  (`agent.to_ag_ui()`) once you want tool calls and human-in-the-loop.
- N-way polls: the A2UI card is data-driven, so it's mostly a framer change.

Pin note: `pydantic-ai` moves fast — if `OpenAIChatModel` isn't found in
your installed version, the older name is `OpenAIModel` in
`pydantic_ai.models.openai`; `config.py` is the only file touched.
# council
