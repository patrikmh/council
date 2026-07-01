// Debate view: agents talk, argue, and can flip their votes over multiple
// rounds. Same panel picker as poll mode, but the transcript is a
// round-by-round grid of agent cards with tool-call badges.

import React, { useEffect, useLayoutEffect, useReducer, useRef, useState } from "react";
import { runDebate } from "./aguiClient.js";
import Spinner from "./Spinner.jsx";
import { PanelPicker, toneForProvider, initialsOf } from "./App.jsx";

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

function useCountUp(target, duration = 700) {
  const [n, setN] = useState(0);
  useEffect(() => {
    let start = 0;
    let frame = 0;
    function step(t) {
      if (!start) start = t;
      const p = Math.min(1, (t - start) / duration);
      // ease-out cubic
      const eased = 1 - Math.pow(1 - p, 3);
      setN(Math.round(target * eased));
      if (p < 1) frame = requestAnimationFrame(step);
    }
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [target, duration]);
  return n;
}

function CountUp({ target }) {
  const n = useCountUp(target, 700);
  return <>{n}</>;
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
    <section className={`verdict tone-${tone} ${tie ? "is-tied" : "is-carried"}`}>
      <div className="verdict-eyebrow">The verdict</div>
      <div className="verdict-headline">
        {tie ? "Split decision" : winners[0]?.opt}
      </div>
      {!tie && <span className="verdict-stamp" aria-hidden="true">Carried</span>}
      {tie && <span className="verdict-stamp verdict-stamp-tied" aria-hidden="true">Tied</span>}
      <div className="verdict-sub">
        {tie
          ? `${winners.map((w) => w.opt).join(" / ")} tied at ${top} vote${top === 1 ? "" : "s"} each`
          : (
            <>
              <CountUp target={top} /> of {total} panelist{total === 1 ? "" : "s"} sided with {winners[0]?.opt}
            </>
          )}
      </div>
      <ul className="verdict-tally">
        {counts.map((c) => {
          const i = snapshot.options.indexOf(c.opt);
          const won = c.n === top && top > 0;
          return (
            <li key={c.opt} className={`tone-${toneFor(i)} ${won ? "is-won" : ""}`}>
              <span className="verdict-opt">{c.opt}</span>
              <span className="verdict-count">
                <CountUp target={c.n} />
              </span>
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

function VoteMatrix({ snapshot, thinkingSet }) {
  const gridRef = useRef(null);
  const dotRefs = useRef({}); // key: `${name}:${ri}` -> HTMLElement
  const [lines, setLines] = useState([]); // {rowKey, x1, y1, x2, y2, color}
  const [hoveredRow, setHoveredRow] = useState(null);
  const [hoveredCol, setHoveredCol] = useState(null);

  const rounds = snapshot?.rounds;

  // Recompute connecting lines whenever the ballots or viewport change.
  useLayoutEffect(() => {
    if (!gridRef.current || !rounds?.length) return;
    function recompute() {
      const grid = gridRef.current;
      if (!grid) return;
      const gridBox = grid.getBoundingClientRect();
      const next = [];
      for (const rowKey of Object.keys(dotRefs.current)) {
        // rowKey shape: "Name:ri"
      }
      // Iterate row-by-row: connect each dot to the next dot in that row.
      const rows = {};
      for (const key of Object.keys(dotRefs.current)) {
        const [name, ri] = key.split(":");
        (rows[name] ||= []).push({ ri: Number(ri), el: dotRefs.current[key] });
      }
      for (const [name, dots] of Object.entries(rows)) {
        dots.sort((a, b) => a.ri - b.ri);
        for (let i = 0; i < dots.length - 1; i++) {
          const a = dots[i].el?.getBoundingClientRect();
          const b = dots[i + 1].el?.getBoundingClientRect();
          if (!a || !b) continue;
          const x1 = a.left + a.width / 2 - gridBox.left;
          const y1 = a.top + a.height / 2 - gridBox.top;
          const x2 = b.left + b.width / 2 - gridBox.left;
          const y2 = b.top + b.height / 2 - gridBox.top;
          const color = dots[i + 1].el?.dataset.tone
            ? `var(--${dots[i + 1].el.dataset.tone})`
            : "rgba(255,255,255,0.2)";
          next.push({ rowKey: name, x1, y1, x2, y2, color });
        }
      }
      setLines(next);
    }
    recompute();
    const ro = new ResizeObserver(recompute);
    ro.observe(gridRef.current);
    window.addEventListener("resize", recompute);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", recompute);
    };
  }, [rounds?.map((r) => r.ballots.length).join(","), rounds?.length]);

  if (!rounds?.length) return null;
  const seen = new Map();
  for (const r of rounds) {
    for (const b of r.ballots) {
      if (!seen.has(b.name)) seen.set(b.name, { name: b.name, provider: b.provider });
    }
  }
  const panelists = [...seen.values()];
  if (!panelists.length) return null;

  function ballotFor(name, ri) {
    return rounds[ri]?.ballots.find((b) => b.name === name) || null;
  }

  return (
    <section
      className="matrix"
      onMouseLeave={() => {
        setHoveredRow(null);
        setHoveredCol(null);
      }}
    >
      <div className="matrix-head">
        <div className="matrix-eyebrow">The arc</div>
        <div className="matrix-legend">hover a row to trace a panelist · ↷ = flip</div>
      </div>
      <div
        ref={gridRef}
        className="matrix-grid"
        style={{
          gridTemplateColumns: `minmax(140px, 1.4fr) repeat(${rounds.length}, 1fr) minmax(70px, auto)`,
        }}
      >
        {/* SVG overlay for connecting lines. Positioned absolutely,
            recomputed on layout changes. */}
        <svg className="matrix-lines" aria-hidden="true">
          {lines.map((ln, i) => {
            const isHot = hoveredRow === ln.rowKey;
            return (
              <line
                key={i}
                x1={ln.x1}
                y1={ln.y1}
                x2={ln.x2}
                y2={ln.y2}
                stroke={ln.color}
                strokeWidth={isHot ? 2.5 : 1.5}
                strokeLinecap="round"
                opacity={hoveredRow && !isHot ? 0.18 : 0.6}
                className="matrix-line"
                style={{
                  strokeDasharray: 200,
                  strokeDashoffset: 0,
                  animationDelay: `${i * 90}ms`,
                }}
              />
            );
          })}
        </svg>

        <div className="matrix-corner" />
        {rounds.map((r, ri) => (
          <div
            key={r.index}
            className={`matrix-col-head ${hoveredCol === ri ? "is-hot" : ""}`}
            onMouseEnter={() => setHoveredCol(ri)}
          >
            <span className="round-mark">R</span>
            <span className="round-numeral">{romanOf(r.index)}</span>
          </div>
        ))}
        <div className="matrix-col-head matrix-final">Final</div>

        {panelists.map((p, pi) => {
          const cells = rounds.map((r, ri) => ballotFor(p.name, ri));
          const final = [...cells].reverse().find((c) => c) || null;
          const tone = toneForProvider(p.provider);
          const isHot = hoveredRow === p.name;
          return (
            <React.Fragment key={p.name}>
              <div
                className={`matrix-row-head tone-${tone} ${isHot ? "is-hot" : ""}`}
                onMouseEnter={() => setHoveredRow(p.name)}
              >
                <span
                  className={`matrix-work ${thinkingSet?.has(p.name) ? "is-working" : ""}`}
                  aria-hidden="true"
                >
                  <span className="matrix-work-bar" />
                  <span className="matrix-work-bar" />
                  <span className="matrix-work-bar" />
                </span>
                <span className="matrix-disc" aria-hidden="true">
                  <span>{initialsOf(p.name)}</span>
                </span>
                <span className="matrix-row-name">{p.name}</span>
              </div>
              {cells.map((b, ri) => {
                const optIdx = b ? snapshot.options.indexOf(b.vote) : -1;
                const cellTone = optIdx >= 0 ? toneFor(optIdx) : null;
                const flipped = !!b?.flipped_from;
                const dim = hoveredRow && hoveredRow !== p.name;
                return (
                  <div
                    key={ri}
                    className={
                      `matrix-cell ${cellTone ? "tone-" + cellTone : "is-abstain"}` +
                      (isHot ? " is-hot" : "") +
                      (dim ? " is-dim" : "") +
                      (hoveredCol === ri ? " is-colhot" : "")
                    }
                    onMouseEnter={() => setHoveredRow(p.name)}
                    title={
                      b
                        ? `${p.name} · Round ${romanOf(ri)}: ${b.vote}` +
                          (flipped ? ` (flipped from ${b.flipped_from})` : "")
                        : `${p.name} · Round ${romanOf(ri)}: abstained`
                    }
                    style={{ animationDelay: `${(pi * 60) + (ri * 40)}ms` }}
                  >
                    {b ? (
                      <>
                        <span
                          className="matrix-dot"
                          ref={(el) => {
                            const key = `${p.name}:${ri}`;
                            if (el) {
                              el.dataset.tone = cellTone || "";
                              dotRefs.current[key] = el;
                            } else {
                              delete dotRefs.current[key];
                            }
                          }}
                        />
                        {flipped && (
                          <span className="matrix-flip" title={`flipped from ${b.flipped_from}`}>
                            ↷
                          </span>
                        )}
                      </>
                    ) : (
                      <span className="matrix-dash">—</span>
                    )}
                  </div>
                );
              })}
              <div className={`matrix-final-cell ${isHot ? "is-hot" : ""}`}>
                {final ? final.vote : <span className="matrix-dash">—</span>}
              </div>
            </React.Fragment>
          );
        })}
      </div>
    </section>
  );
}

const ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII"];
function romanOf(n) {
  return ROMAN[n] ?? String(n + 1);
}

function narrateRound(round, options) {
  const ballots = round.ballots;
  if (!ballots.length) return "No ballots landed in this round.";
  const byOpt = new Map();
  for (const b of ballots) {
    if (!byOpt.has(b.vote)) byOpt.set(b.vote, []);
    byOpt.get(b.vote).push(b.name);
  }
  // Sort options by votes desc, then by original order
  const buckets = [...byOpt.entries()].sort((a, b) => {
    if (b[1].length !== a[1].length) return b[1].length - a[1].length;
    return options.indexOf(a[0]) - options.indexOf(b[0]);
  });
  const flips = ballots.filter((b) => b.flipped_from);
  const summary = buckets
    .map(([opt, names]) => `${opt} ${names.length}`)
    .join(" · ");
  const parts = [summary];
  if (flips.length) {
    const flipLine = flips
      .map(
        (b) =>
          `${b.name.split(" ").slice(0, 2).join(" ")} → ${b.vote}`,
      )
      .join(", ");
    parts.push(`Flips: ${flipLine}`);
  }
  return parts.join("   ·   ");
}

function Round({ round, toolCalls, snapshot }) {
  const [open, setOpen] = useState(false);
  const narrated = narrateRound(round, snapshot?.options || []);
  return (
    <div className={`debate-round ${open ? "is-open" : "is-collapsed"}`}>
      <button
        type="button"
        className="debate-round-title"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="round-mark">Round</span>
        <span className="round-numeral">{romanOf(round.index)}</span>
        <span className="round-narration">{narrated}</span>
        <span className={`round-chevron ${open ? "is-open" : ""}`} aria-hidden="true">
          ▸
        </span>
      </button>
      {!open && (
        <div className="round-mini">
          {round.ballots.map((b) => {
            const idx = snapshot?.options?.indexOf(b.vote) ?? -1;
            const tone = idx >= 0 ? toneFor(idx) : "aqua";
            return (
              <span key={b.name} className={`round-mini-chip tone-${tone}`} title={`${b.name} voted ${b.vote}`}>
                <span className="round-mini-dot" />
                <span className="round-mini-name">{b.name.split(" ").slice(0, 2).join(" ")}</span>
                {b.flipped_from && <span className="round-mini-flip">↷</span>}
              </span>
            );
          })}
        </div>
      )}
      {open && (
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
      )}
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
        thinkingSet={
          state.running
            ? new Set(
                [...selected].filter((name) => {
                  const lastRound = state.snapshot?.rounds?.length
                    ? state.snapshot.rounds[state.snapshot.rounds.length - 1]
                    : null;
                  const voted = lastRound?.ballots?.some((b) => b.name === name);
                  return !voted;
                }),
              )
            : null
        }
      />
      <div className={`composer ${(!state.snapshot && !state.running && state.transcript.length === 0) ? "is-hero" : "is-slim"}`}>
        <textarea
          value={draft}
          rows={1}
          placeholder={
            !state.snapshot && !state.running
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
          disabled={state.running || state.transcript.length === 0}
          title="Clear debate"
        >
          ✕
        </button>
        <button onClick={submit} disabled={state.running || !draft.trim()}>
          {state.running ? "In session" : "Open the floor"}
        </button>
      </div>
      {state.transcript.length === 0 && !state.snapshot && !state.running && (
        <p className="composer-caption">
          Every panelist casts an opening ballot, then they see each other,
          argue, and may flip. They can search the web to check claims.
        </p>
      )}
      <main className="transcript debate-transcript">
        {state.transcript
          .filter((it) => it.type !== "user")
          .map((it) => (
            <div key={it.id} className="note">
              {it.text}
            </div>
          ))}
        {state.snapshot && (
          <>
            <h1 className="motion-headline">
              <span className="motion-eyebrow">The motion</span>
              <span className="motion-quote">“{state.snapshot.question}”</span>
            </h1>
            <TallyBar snapshot={state.snapshot} />
            <VoteMatrix
              snapshot={state.snapshot}
              thinkingSet={
                state.running
                  ? new Set(
                      [...selected].filter((name) => {
                        const lastRound = state.snapshot?.rounds?.length
                          ? state.snapshot.rounds[state.snapshot.rounds.length - 1]
                          : null;
                        const voted = lastRound?.ballots?.some((b) => b.name === name);
                        return !voted;
                      }),
                    )
                  : null
              }
            />
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
    </>
  );
}
