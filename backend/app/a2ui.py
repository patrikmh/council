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

# Tone palette cycled across options for visual variety
_TONES = ("aqua", "clay", "violet", "rose", "mint", "sky")


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


def poll_card_root(num_options: int = 2) -> dict:
    """The AI Council poll card. Everything dynamic is bound to the data
    model, so vote updates are pure dataModelUpdate messages.

    Generates one PollOption child per option with bindings like
    /options/0/votes, /options/0/voters, etc.
    """
    option_components = []
    for i in range(num_options):
        option_components.append(
            component(
                "PollOption",
                optionId=str(i),
                label=bind(f"/options/{i}/label"),
                votes=bind(f"/options/{i}/votes"),
                voters=bind(f"/options/{i}/voters"),
                winner=bind(f"/options/{i}/winner"),
                tone=_TONES[i % len(_TONES)],
            )
        )

    return component(
        "Card",
        children=[
            component("Eyebrow", text="AI Council"),
            component("Heading", text=bind("/question")),
            *option_components,
            component("SectionLabel", text="Council summary"),
            component("Paragraph", text=bind("/summary")),
        ],
    )


def poll_data_model(state: dict) -> dict:
    """Build the data model for the poll card from the current state.

    Expects state["options"] to be a list of option label strings and
    state["ballots"] to have {"vote": <label>, ...} entries.
    """
    option_labels = state["options"]
    num_options = len(option_labels)

    # Build a lookup that accepts index letters (A, B, C…) or exact labels
    vote_lookup: dict[str, int] = {}
    for i, label in enumerate(option_labels):
        vote_lookup[label] = i
        vote_lookup[label.lower()] = i
        vote_lookup[chr(65 + i)] = i       # A, B, C …
        vote_lookup[chr(65 + i).lower()] = i
        vote_lookup[f"Option {chr(65 + i)}"] = i
        vote_lookup[f"option {chr(65 + i)}"] = i
        vote_lookup[f"Option {chr(65 + i)}: {label}"] = i
        vote_lookup[f"option {chr(65 + i)}: {label}"] = i

    voters_by_option: list[list[dict]] = [[] for _ in range(num_options)]
    for ballot in state["ballots"]:
        idx = vote_lookup.get(ballot["vote"]) or vote_lookup.get(ballot["vote"].strip())
        if idx is not None:
            voters_by_option[idx].append(ballot)
        else:
            # Fuzzy: try substring match
            v = ballot["vote"].lower()
            for i, label in enumerate(option_labels):
                if v in label.lower() or label.lower() in v:
                    voters_by_option[i].append(ballot)
                    break

    done = state.get("done", False)
    vote_counts = [len(v) for v in voters_by_option]
    max_votes = max(vote_counts) if vote_counts else 0

    options_data = []
    for i, label in enumerate(option_labels):
        options_data.append({
            "label": label,
            "votes": vote_counts[i],
            "voters": voters_by_option[i],
            "winner": done and vote_counts[i] == max_votes and max_votes > 0,
        })

    return {
        "question": state["question"],
        "options": options_data,
        "summary": state.get("summary", ""),
    }
