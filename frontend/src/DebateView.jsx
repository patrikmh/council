// Debate view: agents talk, argue, and can flip their votes over multiple
// rounds. Same panel picker as poll mode, but the transcript is a
// round-by-round grid of agent cards with tool-call badges.

import React, { useEffect, useReducer, useRef, useState } from "react";
import { runDebate } from "./aguiClient.js";

const STEP_LABELS = {
  frame_question: "Framing the motion…",
  summarize: "Drafting the minutes…",
};

function stepLabel(name) {
  if (STEP_LABELS[name]) return STEP_LABELS[name];
  const m = name.match(/^round_(\d+)$/);
  if (m) return `Round ${m[1]} — panelists debating…`;
  return name;
}

function reducer(state, ev) {
  switch (ev.kind) {
    case "user":
      return {
        ...state,
        transcript: [
          ...state.transcript,
          { type: "user", id: ev.id, text: ev.text },
        ],
        snapshot: null,
        toolCalls: {},
        summary: "",
        running: true,
        status: "Convening the debate…",
      };
    case "agui": {
      const e = ev.event;
      switch (e.type) {
        case "STEP_STARTED":
          return { ...state, status: stepLabel(e.stepName) };
        case "STATE_SNAPSHOT":
          return { ...state, snapshot: e.snapshot };
        case "TEXT_MESSAGE_CONTENT":
          return { ...state, summary: state.summary + e.delta };
        case "CUSTOM":
          if (e.name === "tool_call") {
            const key = `${e.value.round ?? 0}:${e.value.agent}`;
            const list = state.toolCalls[key] || [];
            return {
              ...state,
              toolCalls: { ...state.toolCalls, [key]: [...list, e.value] },
            };
          }
          if (e.name === "panelist_error") {
            return {
              ...state,
              transcript: [
                ...state.transcript,
                {
                  type: "note",
                  id: crypto.randomUUID(),
                  text: `${e.value.name} abstained (error) in round ${
                    (e.value.round ?? 0) + 1
                  }.`,
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
            transcript: [
              ...state.transcript,
              {
                type: "note",
                id: crypto.randomUUID(),
                text: `Run failed: ${e.message}`,
              },
            ],
          };
        default:
          return state;
      }
    }
    case "clear":
      return initialState();
    case "fail":
      return {
        ...state,
        running: false,
        status: "",
        transcript: [
          ...state.transcript,
          { type: "note", id: crypto.randomUUID(), text: ev.text },
        ],
      };
    default:
      return state;
  }
}

function initialState() {
  return {
    transcript: [],
    snapshot: null,
    toolCalls: {},
    summary: "",
    running: false,
    status: "",
  };
}

const TONES = ["aqua", "clay", "violet", "rose", "mint", "sky", "coral", "gold"];

function toneFor(optionIndex) {
  return TONES[optionIndex % TONES.length];
}

function ToolBadge({ call }) {
  if (call.tool === "web_search")
    return <span className="tool-badge">🔎 searched “{(call.query || "").slice(0, 60)}”</span>;
  if (call.tool === "browse") {
    let host = call.url || "";
    try {
      host = new URL(call.url).host;
    } catch {}
    return <span className="tool-badge">🌐 browsed {host}</span>;
  }
  return <span className="tool-badge">🛠 {call.tool}</span>;
}

function optionIndex(snapshot, vote) {
  return snapshot?.options?.findIndex((o) => o === vote) ?? -1;
}

function TallyBar({ snapshot }) {
  if (!snapshot?.tally || !snapshot?.options) return null;
  const total = snapshot.options.reduce(
    (a, o) => a + (snapshot.tally[o] || 0),
    0,
  );
  if (!total) return null;
  return (
    <div className="tally-bar">
      {snapshot.options.map((opt, i) => {
        const n = snapshot.tally[opt] || 0;
        const pct = total ? (100 * n) / total : 0;
        return (
          <div
            key={opt}
            className={`tally-seg tone-${toneFor(i)}`}
            style={{ width: `${pct}%` }}
            title={`${opt}: ${n}`}
          >
            <span className="tally-label">
              {opt} · {n}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function AgentCard({ ballot, toolCalls, snapshot }) {
  const idx = optionIndex(snapshot, ballot.vote);
  const tone = idx >= 0 ? toneFor(idx) : "aqua";
  return (
    <div className={`debate-card tone-${tone}`}>
      <div className="debate-card-head">
        <span className="model-toggle-provider">{ballot.provider}</span>
        <span className="debate-card-name">{ballot.name}</span>
        <span className="debate-card-vote">→ {ballot.vote}</span>
        {ballot.flipped_from && (
          <span className="debate-card-flip">
            flipped from {ballot.flipped_from}
          </span>
        )}
      </div>
      <div className="debate-card-reason">{ballot.reasoning}</div>
      {toolCalls?.length > 0 && (
        <div className="debate-card-tools">
          {toolCalls.map((c, i) => (
            <ToolBadge key={i} call={c} />
          ))}
        </div>
      )}
    </div>
  );
}

function Round({ round, toolCalls, snapshot }) {
  return (
    <div className="debate-round">
      <div className="debate-round-title">Round {round.index + 1}</div>
      <div className="debate-round-grid">
        {round.ballots.map((b) => (
          <AgentCard
            key={`${round.index}-${b.name}`}
            ballot={b}
            snapshot={snapshot}
            toolCalls={toolCalls[`${round.index}:${b.name}`] || []}
          />
        ))}
      </div>
    </div>
  );
}

export default function DebateView({ panelists, selected, toggleModel }) {
  const [state, dispatch] = useReducer(reducer, undefined, initialState);
  const [draft, setDraft] = useState("");
  const threadId = useRef(crypto.randomUUID());
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [state.snapshot, state.summary, state.status]);

  async function submit() {
    const question = draft.trim();
    if (!question || state.running) return;
    setDraft("");
    dispatch({ kind: "user", id: crypto.randomUUID(), text: question });
    try {
      await runDebate({
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
    <>
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
      <main className="transcript debate-transcript">
        {state.transcript.length === 0 && !state.snapshot && !state.running && (
          <div className="empty">
            <img src="/logo.png" alt="" className="empty-logo" />
            <p className="empty-display">Open the floor.</p>
            <p className="empty-hint">
              Ask a question. Every panelist casts an opening ballot, then they
              see each other, argue, and may flip. They can search the web to
              check claims.
            </p>
          </div>
        )}
        {state.transcript.map((it) =>
          it.type === "user" ? (
            <div key={it.id} className="bubble bubble-user">
              {it.text}
            </div>
          ) : (
            <div key={it.id} className="note">
              {it.text}
            </div>
          ),
        )}
        {state.snapshot && (
          <>
            <TallyBar snapshot={state.snapshot} />
            {state.snapshot.rounds?.map((r) => (
              <Round
                key={r.index}
                round={r}
                snapshot={state.snapshot}
                toolCalls={state.toolCalls}
              />
            ))}
            {state.summary && (
              <div className="bubble bubble-assistant debate-summary">
                {state.summary}
              </div>
            )}
          </>
        )}
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
          disabled={state.running || state.transcript.length === 0}
          title="Clear debate"
        >
          ✕
        </button>
        <button onClick={submit} disabled={state.running || !draft.trim()}>
          {state.running ? "In session" : "Open the floor"}
        </button>
      </footer>
    </>
  );
}
