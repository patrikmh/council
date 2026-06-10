// AI Council chat. The transcript is a list of items: user questions,
// A2UI surfaces (the poll cards), streamed assistant text, and step notes.
// A single reducer folds the AG-UI event stream into that transcript.

import React, { useEffect, useReducer, useRef, useState } from "react";
import { runAgent } from "./aguiClient.js";
import A2UISurface from "./A2UIRenderer.jsx";

const STEP_LABELS = {
  frame_question: "Framing the motion…",
  collect_ballots: "The table is voting…",
  summarize: "Drafting the summary…",
};

function reducer(state, ev) {
  switch (ev.kind) {
    case "user":
      return {
        ...state,
        items: [...state.items, { type: "user", id: ev.id, text: ev.text }],
        running: true,
        status: "Convening the panel…",
      };
    case "agui": {
      const e = ev.event;
      switch (e.type) {
        case "STEP_STARTED":
          return { ...state, status: STEP_LABELS[e.stepName] ?? e.stepName };
        case "TEXT_MESSAGE_START":
          return {
            ...state,
            items: [...state.items, { type: "text", id: e.messageId, text: "" }],
          };
        case "TEXT_MESSAGE_CONTENT":
          return {
            ...state,
            items: state.items.map((it) =>
              it.id === e.messageId ? { ...it, text: it.text + e.delta } : it
            ),
          };
        case "CUSTOM":
          if (e.name === "a2ui") return applyA2UI(state, e.value);
          if (e.name === "panelist_error") {
            return {
              ...state,
              items: [
                ...state.items,
                {
                  type: "note",
                  id: crypto.randomUUID(),
                  text: `${e.value.name} abstained (error).`,
                },
              ],
            };
          }
          return state;
        case "RUN_FINISHED":
          return { ...state, running: false, status: "" };
        case "RUN_ERROR":
          return {
            ...state,
            running: false,
            status: "",
            items: [
              ...state.items,
              { type: "note", id: crypto.randomUUID(), text: `Run failed: ${e.message}` },
            ],
          };
        default:
          return state;
      }
    }
    case "clear":
      return { items: [], running: false, status: "" };
    case "fail":
      return {
        ...state,
        running: false,
        status: "",
        items: [
          ...state.items,
          { type: "note", id: crypto.randomUUID(), text: ev.text },
        ],
      };
    default:
      return state;
  }
}

function applyA2UI(state, msg) {
  if (msg.a2ui === "beginRendering") {
    return {
      ...state,
      items: [
        ...state.items,
        { type: "surface", id: msg.surfaceId, root: msg.root, data: {} },
      ],
    };
  }
  if (msg.a2ui === "dataModelUpdate") {
    return {
      ...state,
      items: state.items.map((it) =>
        it.id === msg.surfaceId ? { ...it, data: { ...it.data, ...msg.contents } } : it
      ),
    };
  }
  return state;
}

export default function App() {
  const [state, dispatch] = useReducer(reducer, {
    items: [],
    running: false,
    status: "",
  });
  const [draft, setDraft] = useState("");
  const [panelists, setPanelists] = useState([]);       // {name, provider}[]
  const [selected, setSelected] = useState(new Set());  // names
  const threadId = useRef(crypto.randomUUID());
  const endRef = useRef(null);

  // Fetch the panel on mount
  useEffect(() => {
    fetch("/panel")
      .then((r) => r.json())
      .then((list) => {
        setPanelists(list);
        setSelected(new Set(list.map((p) => p.name))); // all on by default
      })
      .catch(() => {});
  }, []);

  function toggleModel(name) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [state.items, state.status]);

  async function submit() {
    const question = draft.trim();
    if (!question || state.running) return;
    setDraft("");
    dispatch({ kind: "user", id: crypto.randomUUID(), text: question });
    try {
      await runAgent({
        question,
        threadId: threadId.current,
        selectedModels: [...selected],
        onEvent: (event) => dispatch({ kind: "agui", event }),
      });
    } catch (err) {
      dispatch({ kind: "fail", text: `Could not reach the backend: ${err.message}` });
    }
  }

  return (
    <div className="shell">
      <header className="masthead">
        <img src="/logo.png" alt="AI Council" className="masthead-logo" />
        <span className="masthead-name">AI Council</span>
        <span className="masthead-sub">one question · every model votes</span>
      </header>
      <div className="masthead-rule" />

      {panelists.length > 0 && (
        <div className="model-picker">
          <span className="model-picker-label">Panel</span>
          {panelists.map((p) => (
            <button
              key={p.name}
              className={`model-toggle ${selected.has(p.name) ? "is-on" : ""}`}
              disabled={state.running}
              onClick={() => toggleModel(p.name)}
            >
              <span className="model-toggle-provider">{p.provider}</span>
              {p.name}
            </button>
          ))}
        </div>
      )}

      <main className="transcript">
        {state.items.length === 0 && !state.running && (
          <div className="empty">
            <img src="/logo.png" alt="" className="empty-logo" />
            <p className="empty-display">Put it to the table.</p>
            <p className="empty-hint">
              Ask any question — the panel frames the options (2–6), each member
              votes, and one writes the minutes.
            </p>
          </div>
        )}
        {state.items.map((it) => {
          if (it.type === "user")
            return (
              <div key={it.id} className="bubble bubble-user">
                {it.text}
              </div>
            );
          if (it.type === "surface") return <A2UISurface key={it.id} surface={it} />;
          if (it.type === "text")
            return (
              <div key={it.id} className="bubble bubble-assistant">
                {it.text}
              </div>
            );
          return (
            <div key={it.id} className="note">
              {it.text}
            </div>
          );
        })}
        {state.running && <div className="status">{state.status}</div>}
        <div ref={endRef} />
      </main>

      <footer className="composer">
        <textarea
          value={draft}
          rows={1}
          placeholder="Which city should we move to: Stockholm, Berlin, or Lisbon?"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
        />
        <button
          className="btn-clear"
          onClick={() => {
            dispatch({ kind: "clear" });
            threadId.current = crypto.randomUUID();
          }}
          disabled={state.running || state.items.length === 0}
          title="Clear chat"
        >
          ✕
        </button>
        <button onClick={submit} disabled={state.running || !draft.trim()}>
          {state.running ? "In session" : "Ask the table"}
        </button>
      </footer>
    </div>
  );
}
