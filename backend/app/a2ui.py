"""A2UI-inspired declarative UI payloads.

The agent never sends HTML. It sends a component tree plus a data model,
and the client renders it with its own component library — the core A2UI
idea. This is a pragmatic subset: a nested tree instead of A2UI's flat
adjacency list, because it's far easier to read and debug. Bindings use
``{"path": "/json/pointer"}`` objects resolved against the surface's
data model, so live vote updates only touch the data model.

Messages:
  begin_rendering(surface_id, root)   -> mount/replace a surface
  data_model_update(surface_id, data) -> patch the surface's data model
"""

from typing import Any


def begin_rendering(surface_id: str, root: dict) -> dict:
    return {"a2ui": "beginRendering", "surfaceId": surface_id, "root": root}


def data_model_update(surface_id: str, contents: dict) -> dict:
    return {"a2ui": "dataModelUpdate", "surfaceId": surface_id, "contents": contents}


def bind(path: str) -> dict[str, str]:
    return {"path": path}


# ---- component constructors -------------------------------------------------

def component(ctype: str, **props: Any) -> dict:
    children = props.pop("children", None)
    node: dict[str, Any] = {"component": ctype, "props": props}
    if children is not None:
        node["children"] = children
    return node


def poll_card_root() -> dict:
    """The AI Council poll card. Everything dynamic is bound to the data
    model, so vote updates are pure dataModelUpdate messages."""
    return component(
        "Card",
        children=[
            component("Eyebrow", text="AI Council"),
            component("Heading", text=bind("/question")),
            component(
                "PollOption",
                optionId="A",
                label=bind("/optionA"),
                votes=bind("/votesA"),
                voters=bind("/votersA"),
                winner=bind("/winnerA"),
                tone="aqua",
            ),
            component(
                "PollOption",
                optionId="B",
                label=bind("/optionB"),
                votes=bind("/votesB"),
                voters=bind("/votersB"),
                winner=bind("/winnerB"),
                tone="clay",
            ),
            component("SectionLabel", text="Council summary"),
            component("Paragraph", text=bind("/summary")),
        ],
    )


def poll_data_model(state: dict) -> dict:
    voters_a = [v for v in state["ballots"] if v["vote"] == "A"]
    voters_b = [v for v in state["ballots"] if v["vote"] == "B"]
    done = state.get("done", False)
    return {
        "question": state["question"],
        "optionA": state["option_a"],
        "optionB": state["option_b"],
        "votesA": len(voters_a),
        "votesB": len(voters_b),
        "votersA": voters_a,
        "votersB": voters_b,
        "winnerA": done and len(voters_a) > len(voters_b),
        "winnerB": done and len(voters_b) > len(voters_a),
        "summary": state.get("summary", ""),
    }
