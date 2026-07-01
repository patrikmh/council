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
  const N = panelists.length;

  // Fall back to the compact linear list on narrow screens (media query
  // handles the actual switch; JSX renders both, CSS shows one).
  return (
    <div className="chamber">
      <div className="table-scene" style={{ "--seats": N }}>
        <div className="round-table" aria-hidden="true">
          <img
            src="/logo.png"
            alt=""
            className="table-logo"
            draggable={false}
          />
          <span className="table-wordmark">Rabble</span>
        </div>
        {panelists.map((p, i) => {
          const dead = p.available === false;
          const on = selected.has(p.name);
          const thinking = thinkingSet?.has(p.name);
          const tone = toneForProvider(p.provider);
          // Place chairs evenly around an ellipse, starting at 12 o'clock.
          const angle = (i / N) * 2 * Math.PI - Math.PI / 2;
          const x = Math.cos(angle) * 44; // % of container half-width
          const y = Math.sin(angle) * 38; // % — squished for ellipse
          const style = {
            left: `calc(50% + ${x}%)`,
            top: `calc(50% + ${y}%)`,
          };
          return (
            <button
              key={p.name}
              className={
                `chair tone-${tone}` +
                (on ? " is-on" : "") +
                (dead ? " is-dead" : "") +
                (thinking ? " is-thinking" : "")
              }
              style={style}
              disabled={disabled || dead}
              onClick={() => !dead && toggleModel(p.name)}
              title={
                dead
                  ? `${p.slug} is not on OpenRouter right now`
                  : `${p.provider} · ${p.slug}`
              }
            >
              <span className="chair-disc" aria-hidden="true">
                <span className="chair-disc-inner">{initialsOf(p.name)}</span>
                {thinking && <span className="chair-halo" />}
              </span>
              <span className="chair-name">{p.name}</span>
              <span className="chair-provider">{p.provider}</span>
              {dead && <span className="chair-flag">404</span>}
            </button>
          );
        })}
      </div>
      {deadCount > 0 && (
        <div className="chamber-hint">
          {deadCount} seat{deadCount === 1 ? "" : "s"} not on OpenRouter
        </div>
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

  const isEmpty = state.items.length === 0 && !state.running;
  const composer = (
    <div className={`composer ${isEmpty ? "is-hero" : "is-slim"}`}>
      <textarea
        value={draft}
        rows={1}
        placeholder={
          isEmpty
            ? "Put a motion to the table…"
            : "Ask another question"
        }
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
    </div>
  );

  return (
    <>
      <PanelPicker
        panelists={panelists}
        selected={selected}
        toggleModel={toggleModel}
        disabled={state.running}
        thinkingSet={state.running ? selected : null}
      />
      {composer}
      {isEmpty && (
        <p className="composer-caption">
          The panel frames 2–6 options, each member votes, one writes the
          minutes.
        </p>
      )}
      <main className="transcript">
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
        <nav className="tabs" aria-label="Mode">
          {[
            ["poll", "Poll", "one question, everyone votes"],
            ["debate", "Debate", "argue across rounds"],
            ["stats", "Stats", "who's been most persuasive"],
          ].map(([key, label, hint]) => (
            <button
              key={key}
              className={`tab ${mode === key ? "is-on" : ""}`}
              onClick={() => setMode(key)}
              title={hint}
            >
              <span className="tab-label">{label}</span>
              <span className="tab-hint">{hint}</span>
            </button>
          ))}
        </nav>
      </header>

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
