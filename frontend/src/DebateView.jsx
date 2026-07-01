// Debate view: agents talk, argue, and can flip their votes over multiple
// rounds. Same panel picker as poll mode, but the transcript is a
// round-by-round grid of agent cards with tool-call badges.

import React, { useEffect, useReducer, useRef, useState } from "react";
import { runDebate } from "./aguiClient.js";
import Spinner from "./Spinner.jsx";
import { PanelPicker } from "./App.jsx";

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
        step: "_default",
      };
    case "agui": {
      const e = ev.event;
      switch (e.type) {
        case "STEP_STARTED":
          return { ...state, step: e.stepName };
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
            const err = (e.value.error || "").slice(0, 240);
            return {
              ...state,
              transcript: [
                ...state.transcript,
                {
                  type: "note",
                  id: crypto.randomUUID(),
                  text: `${e.value.name} abstained (error) in round ${
                    (e.value.round ?? 0) + 1
                  }${err ? `: ${err}` : "."}`,
                },
              ],
            };
          }
          return state;
        case "RUN_FINISHED":
          return { ...state, running: false, step: null };
        case "RUN_ERROR":
          return {
            ...state,
            running: false,
            step: null,
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
        step: null,
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
    step: null,
  };
}

const TONES = ["aqua", "clay", "violet", "rose", "mint", "sky", "coral", "gold"];

function toneFor(optionIndex) {
  return TONES[optionIndex % TONES.length];
}

function hostOf(url) {
  try {
    return new URL(url).host.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function pathOf(url) {
  try {
    const u = new URL(url);
    return (u.pathname + u.search).replace(/\/$/, "") || "/";
  } catch {
    return "";
  }
}

// Turn any http(s) URL in a run of text into a clickable link. The link
// label is the host so the reasoning stays readable at a glance; the full
// URL is the href and a title tooltip.
function linkifyReasoning(text) {
  const parts = text.split(/(https?:\/\/[^\s)\]]+)/g);
  return parts.map((part, i) =>
    /^https?:/.test(part) ? (
      <a
        key={i}
        href={part}
        target="_blank"
        rel="noopener noreferrer"
        className="reason-link"
        title={part}
      >
        {hostOf(part)}
      </a>
    ) : (
      <React.Fragment key={i}>{part}</React.Fragment>
    ),
  );
}

function Sources({ toolCalls }) {
  if (!toolCalls?.length) return null;
  const searches = toolCalls.filter((c) => c.tool === "web_search");
  // De-dup browsed URLs so the same link isn't listed twice.
  const browseByUrl = new Map();
  for (const c of toolCalls) {
    if (c.tool !== "browse" || !c.url) continue;
    const existing = browseByUrl.get(c.url);
    if (!existing || (existing.cached && !c.cached)) {
      browseByUrl.set(c.url, c);
    }
  }
  const browses = [...browseByUrl.values()];
  if (!searches.length && !browses.length) return null;
  return (
    <div className="sources">
      {browses.length > 0 && (
        <>
          <div className="sources-label">Sources</div>
          <ul className="sources-list">
            {browses.map((b, i) => (
              <li key={i} className={b.cached ? "is-cached" : ""}>
                <a
                  href={b.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={b.url}
                >
                  <span className="src-host">{hostOf(b.url)}</span>
                  <span className="src-path">{pathOf(b.url).slice(0, 48)}</span>
                </a>
                {b.cached && <span className="src-flag">cached</span>}
              </li>
            ))}
          </ul>
        </>
      )}
      {searches.length > 0 && (
        <>
          <div className="sources-label sources-label-sub">Searches run</div>
          <div className="sources-queries">
            {searches.map((s, i) => (
              <span
                key={i}
                className={`src-query ${s.cached ? "is-cached" : ""}`}
                title={s.query || ""}
              >
                {(s.query || "").slice(0, 80)}
                {s.cached && <span className="src-flag"> · cached</span>}
              </span>
            ))}
          </div>
        </>
      )}
    </div>
  );
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
      <div className="debate-card-reason">
        {linkifyReasoning(ballot.reasoning)}
      </div>
      <Sources toolCalls={toolCalls} />
    </div>
  );
}

// Strip the model-added heading if the summarizer still leads with it.
function cleanSummary(text) {
  return text
    .replace(/^\s*\**\s*rabble\s*(minutes|summary)\s*\**[:.\-—\s]*/i, "")
    .replace(/^\s*[#*]+\s*/, "")
    .trim();
}

function Verdict({ snapshot }) {
  if (!snapshot?.done || !snapshot.rounds?.length) return null;
  const finalRound = snapshot.rounds[snapshot.rounds.length - 1].ballots;
  const tally = snapshot.tally || {};
  const counts = snapshot.options.map((opt) => ({ opt, n: tally[opt] || 0 }));
  const top = Math.max(...counts.map((c) => c.n));
  const winners = counts.filter((c) => c.n === top && top > 0);
  const total = counts.reduce((a, c) => a + c.n, 0);
  const idx = snapshot.options.indexOf(winners[0]?.opt);
  const tone = idx >= 0 ? toneFor(idx) : "aqua";
  const tie = winners.length > 1;

  return (
    <section className={`verdict tone-${tone}`}>
      <div className="verdict-eyebrow">The verdict</div>
      <div className="verdict-headline">
        {tie ? "Split decision" : winners[0]?.opt}
      </div>
      <div className="verdict-sub">
        {tie
          ? `${winners.map((w) => w.opt).join(" / ")} tied at ${top} vote${top === 1 ? "" : "s"} each`
          : `${top} of ${total} panelist${total === 1 ? "" : "s"} sided with ${winners[0]?.opt}`}
      </div>
      <ul className="verdict-tally">
        {counts.map((c) => {
          const i = snapshot.options.indexOf(c.opt);
          const won = c.n === top && top > 0;
          return (
            <li key={c.opt} className={`tone-${toneFor(i)} ${won ? "is-won" : ""}`}>
              <span className="verdict-opt">{c.opt}</span>
              <span className="verdict-count">{c.n}</span>
            </li>
          );
        })}
      </ul>
      <div className="verdict-voters">
        {finalRound.map((b) => (
          <span key={b.name} className="verdict-voter">
            <span className="verdict-voter-name">{b.name}</span>
            <span className="verdict-voter-arrow">→</span>
            <span className="verdict-voter-vote">{b.vote}</span>
          </span>
        ))}
      </div>
    </section>
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
  }, [state.snapshot, state.summary, state.step]);

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
      <PanelPicker
        panelists={panelists}
        selected={selected}
        toggleModel={toggleModel}
        disabled={state.running}
      />
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
              <aside className="minutes">
                <div className="minutes-eyebrow">Rabble minutes</div>
                <p className="minutes-body">{cleanSummary(state.summary)}</p>
              </aside>
            )}
            <Verdict snapshot={state.snapshot} />
          </>
        )}
        {state.running && <Spinner step={state.step} />}
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
