// Rabble shell: three tabs — Poll (original), Debate (agents argue + browse),
// Stats (influence + questions feed). Poll and Debate share the panel picker
// and thread lifecycle; Stats is a read-only view.

import React, { useEffect, useReducer, useRef, useState } from "react";
import { runAgent } from "./aguiClient.js";
import A2UISurface from "./A2UIRenderer.jsx";
import DebateView from "./DebateView.jsx";
import StatsView from "./StatsView.jsx";
import Spinner from "./Spinner.jsx";

function cleanSummary(text) {
  return text
    .replace(/^\s*\**\s*rabble\s*(minutes|summary)\s*\**[:.\-—\s]*/i, "")
    .replace(/^\s*[#*]+\s*/, "")
    .trim();
}

// Map each provider to a stable tone from the palette. Hash-based fallback
// keeps unknown providers deterministically colored across renders.
const PROVIDER_TONES = {
  anthropic: "clay",
  openai: "mint",
  google: "sky",
  "x-ai": "coral",
  "z-ai": "violet",
  moonshotai: "aqua",
  deepseek: "rose",
  qwen: "gold",
  minimax: "coral",
  stepfun: "clay",
  meta: "sky",
  "meta-llama": "sky",
  mistralai: "rose",
};
const TONE_CYCLE = ["aqua", "clay", "violet", "rose", "mint", "sky", "coral", "gold"];

export function toneForProvider(provider) {
  if (PROVIDER_TONES[provider]) return PROVIDER_TONES[provider];
  let h = 0;
  for (const ch of provider) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return TONE_CYCLE[h % TONE_CYCLE.length];
}

// Two-letter initials from the display name. "Claude Sonnet 4.6" → "CS".
export function initialsOf(name) {
  const parts = name.replace(/[.\-_]/g, " ").split(/\s+/).filter(Boolean);
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

export function PanelPicker({ panelists, selected, toggleModel, disabled, thinkingSet }) {
  if (panelists.length === 0) return null;
  const deadCount = panelists.filter((p) => p.available === false).length;
  return (
    <div className="seating">
      <span className="seating-label">The table</span>
      <div className="seating-row">
        {panelists.map((p) => {
          const dead = p.available === false;
          const on = selected.has(p.name);
          const thinking = thinkingSet?.has(p.name);
          const tone = toneForProvider(p.provider);
          return (
            <button
              key={p.name}
              className={
                `seat tone-${tone}` +
                (on ? " is-on" : "") +
                (dead ? " is-dead" : "") +
                (thinking ? " is-thinking" : "")
              }
              disabled={disabled || dead}
              onClick={() => !dead && toggleModel(p.name)}
              title={
                dead
                  ? `${p.slug} is not on OpenRouter right now`
                  : `${p.provider} · ${p.slug}`
              }
            >
              <span className="seat-disc" aria-hidden="true">
                <span className="seat-disc-inner">{initialsOf(p.name)}</span>
                {thinking && <span className="seat-halo" />}
              </span>
              <span className="seat-name">{p.name}</span>
              <span className="seat-provider">{p.provider}</span>
              {dead && <span className="seat-flag">404</span>}
            </button>
          );
        })}
      </div>
      {deadCount > 0 && (
        <span className="seating-hint">
          {deadCount} seat{deadCount === 1 ? "" : "s"} not on OpenRouter
        </span>
      )}
    </div>
  );
}

function reducer(state, ev) {
  switch (ev.kind) {
    case "user":
      return {
        ...state,
        items: [...state.items, { type: "user", id: ev.id, text: ev.text }],
        running: true,
        step: "_default",
      };
    case "agui": {
      const e = ev.event;
      switch (e.type) {
        case "STEP_STARTED":
          return { ...state, step: e.stepName };
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
          if (e.name === "tool_call") {
            return {
              ...state,
              items: [
                ...state.items,
                {
                  type: "tool",
                  id: crypto.randomUUID(),
                  agent: e.value.agent,
                  tool: e.value.tool,
                  query: e.value.query,
                  url: e.value.url,
                },
              ],
            };
          }
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
          return { ...state, running: false, step: null };
        case "RUN_ERROR":
          return {
            ...state,
            running: false,
            step: null,
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
      return { items: [], running: false, step: null };
    case "fail":
      return {
        ...state,
        running: false,
        step: null,
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

function ToolNote({ item }) {
  const cachedMark = item.cached ? " · cached" : "";
  if (item.tool === "web_search")
    return (
      <div className="tool-note">
        <span className="tool-note-agent">{item.agent}</span>
        <span className={`tool-badge ${item.cached ? "is-cached" : ""}`}>
          🔎 searched “{(item.query || "").slice(0, 60)}”{cachedMark}
        </span>
      </div>
    );
  if (item.tool === "browse") {
    let host = item.url || "";
    try {
      host = new URL(item.url).host.replace(/^www\./, "");
    } catch {}
    return (
      <div className="tool-note">
        <span className="tool-note-agent">{item.agent}</span>
        <a
          className={`tool-badge tool-badge-link ${item.cached ? "is-cached" : ""}`}
          href={item.url}
          target="_blank"
          rel="noopener noreferrer"
          title={item.url}
        >
          🌐 {host}{cachedMark}
        </a>
      </div>
    );
  }
  return null;
}

function PollView({ panelists, selected, toggleModel }) {
  const [state, dispatch] = useReducer(reducer, {
    items: [],
    running: false,
    step: null,
  });
  const [draft, setDraft] = useState("");
  const threadId = useRef(crypto.randomUUID());
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [state.items, state.step]);

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
    <>
      <PanelPicker
        panelists={panelists}
        selected={selected}
        toggleModel={toggleModel}
        disabled={state.running}
        thinkingSet={state.running ? selected : null}
      />
      <main className="transcript">
        {state.items.length === 0 && !state.running && (
          <div className="empty">
            <img src="/logo.png" alt="" className="empty-logo" />
            <p className="empty-display">Put it to the table.</p>
            <p className="empty-hint">
              Ask any question — the panel frames the options (2–6), each member
              votes, and one writes the minutes. Panelists can browse the web
              if a fact needs checking.
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
              <aside key={it.id} className="minutes">
                <div className="minutes-eyebrow">Rabble minutes</div>
                <p className="minutes-body">{cleanSummary(it.text)}</p>
              </aside>
            );
          if (it.type === "tool") return <ToolNote key={it.id} item={it} />;
          return (
            <div key={it.id} className="note">
              {it.text}
            </div>
          );
        })}
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
          disabled={state.running || state.items.length === 0}
          title="Clear chat"
        >
          ✕
        </button>
        <button onClick={submit} disabled={state.running || !draft.trim()}>
          {state.running ? "In session" : "Ask the table"}
        </button>
      </footer>
    </>
  );
}

export default function App() {
  const [mode, setMode] = useState("poll");
  const [panelists, setPanelists] = useState([]);
  const [selected, setSelected] = useState(new Set());

  useEffect(() => {
    fetch("/panel")
      .then((r) => r.json())
      .then((list) => {
        setPanelists(list);
        // Default: select every panelist except ones the backend
        // confirmed are missing from OpenRouter's catalog.
        setSelected(
          new Set(
            list
              .filter((p) => p.available !== false)
              .map((p) => p.name),
          ),
        );
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

  return (
    <div className="shell">
      <header className="masthead">
        <img src="/logo.png" alt="Rabble" className="masthead-logo" />
        <span className="masthead-name">Rabble</span>
        <span className="masthead-sub">one question · every model votes</span>
        <nav className="tabs">
          {[
            ["poll", "Poll"],
            ["debate", "Debate"],
            ["stats", "Stats"],
          ].map(([key, label]) => (
            <button
              key={key}
              className={`tab ${mode === key ? "is-on" : ""}`}
              onClick={() => setMode(key)}
            >
              {label}
            </button>
          ))}
        </nav>
      </header>
      <div className="masthead-rule" />

      {mode === "poll" && (
        <PollView
          panelists={panelists}
          selected={selected}
          toggleModel={toggleModel}
        />
      )}
      {mode === "debate" && (
        <DebateView
          panelists={panelists}
          selected={selected}
          toggleModel={toggleModel}
        />
      )}
      {mode === "stats" && <StatsView />}
    </div>
  );
}
