"""AG-UI protocol: event builders + SSE encoding.

Implements the AG-UI event vocabulary (https://docs.ag-ui.com) by hand so
there is no magic: every event is a plain dict serialized as one SSE frame.
Field names are camelCase per the AG-UI spec.
"""

import json
import time
from typing import Any


def sse(event: dict[str, Any]) -> str:
    """Encode one AG-UI event as a Server-Sent Events frame."""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _base(event_type: str) -> dict[str, Any]:
    return {"type": event_type, "timestamp": int(time.time() * 1000)}


def run_started(thread_id: str, run_id: str) -> dict:
    return _base("RUN_STARTED") | {"threadId": thread_id, "runId": run_id}


def run_finished(thread_id: str, run_id: str) -> dict:
    return _base("RUN_FINISHED") | {"threadId": thread_id, "runId": run_id}


def run_error(message: str, code: str = "PANEL_ERROR") -> dict:
    return _base("RUN_ERROR") | {"message": message, "code": code}


def step_started(name: str) -> dict:
    return _base("STEP_STARTED") | {"stepName": name}


def step_finished(name: str) -> dict:
    return _base("STEP_FINISHED") | {"stepName": name}


def text_start(message_id: str, role: str = "assistant") -> dict:
    return _base("TEXT_MESSAGE_START") | {"messageId": message_id, "role": role}


def text_content(message_id: str, delta: str) -> dict:
    return _base("TEXT_MESSAGE_CONTENT") | {"messageId": message_id, "delta": delta}


def text_end(message_id: str) -> dict:
    return _base("TEXT_MESSAGE_END") | {"messageId": message_id}


def state_snapshot(snapshot: dict) -> dict:
    """Full shared-state snapshot. We snapshot on every vote rather than
    emitting JSON Patch STATE_DELTAs — simpler on both ends, same UX."""
    return _base("STATE_SNAPSHOT") | {"snapshot": snapshot}


def custom(name: str, value: Any) -> dict:
    """CUSTOM events carry app-specific payloads — we use them to ship
    A2UI surface messages to the client."""
    return _base("CUSTOM") | {"name": name, "value": value}
