// News view: twice a day the council reads the Swedish outlets (mainstream
// plus both partisan flanks),
// debates each multi-source story, and a judge publishes the edition —
// consensus account, declared-vs-measured framing per outlet, disagreement
// callouts and fact-checks. No scheduler: the first visitor after 09:00 /
// 18:00 (Stockholm) generates the edition live; everyone else reads the
// cached result from GET /news/latest.

import React, { useEffect, useReducer, useRef, useState } from "react";
import { runNews } from "./aguiClient.js";
import Spinner from "./Spinner.jsx";

function hostOf(url) {
  try {
    return new URL(url).host.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function linkify(text) {
  if (!text) return null;
  const parts = String(text).split(/(https?:\/\/[^\s)\]]+)/g);
  return parts.map((part, i) =>
    /^https?:/.test(part) ? (
      <a key={i} href={part} target="_blank" rel="noopener noreferrer"
         className="reason-link" title={part}>
        {hostOf(part)}
      </a>
    ) : (
      <React.Fragment key={i}>{part}</React.Fragment>
    ),
  );
}

// "2026-07-07-morning" → "7 Jul · morning" (archive chip label)
function slotShort(slot) {
  const m = slot?.match(/^(\d{4})-(\d{2})-(\d{2})-(morning|evening)$/);
  if (!m) return slot;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const nice = d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  return `${nice} · ${m[4]}`;
}

// "2026-07-07-morning" → "Morning edition · 7 July 2026"
function slotLabel(slot) {
  if (!slot) return "";
  const m = slot.match(/^(\d{4})-(\d{2})-(\d{2})-(morning|evening)$/);
  if (!m) return slot;
  const date = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const nice = date.toLocaleDateString("en-GB", {
    day: "numeric", month: "long", year: "numeric",
  });
  return `${m[4] === "morning" ? "Morning" : "Evening"} edition · ${nice}`;
}

const VERDICT_META = {
  verified: { mark: "✓", label: "verified", cls: "is-verified" },
  unverified: { mark: "?", label: "unverified", cls: "is-unverified" },
  contradicted: { mark: "✗", label: "contradicted", cls: "is-contradicted" },
};

function FactCheckList({ checks, outlets }) {
  if (!checks?.length) return null;
  return (
    <div className="news-facts">
      <div className="news-section-eyebrow">Fact-checks</div>
      <ul className="news-facts-list">
        {checks.map((fc, i) => {
          const meta = VERDICT_META[fc.verdict] || VERDICT_META.unverified;
          return (
            <li key={i} className="news-fact">
              <span className={`fact-verdict ${meta.cls}`}>
                {meta.mark} {meta.label}
              </span>
              {fc.source && outlets?.[fc.source] && (
                <span className="stance-badge" title="the outlet whose article carries this claim">
                  {outlets[fc.source].name}
                </span>
              )}
              <span className="news-fact-claim">“{fc.claim}”</span>
              <span className="news-fact-evidence">{linkify(fc.evidence)}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// Old editions stored a bare left-right number; newer ones store
// {lr, lc} — economic left-right and social liberal-conservative.
function axisValue(lean, axis) {
  if (lean == null) return null;
  if (typeof lean === "number") return axis === "lr" ? lean : null;
  return lean[axis] ?? null;
}

// Declared stance vs the council's measured rating of this article
// (-2..+2, median across panelists). The gap between the two is the point.
function LeanScale({ value, ends }) {
  if (value == null) return null;
  const pct = ((Number(value) + 2) / 4) * 100;
  return (
    <span className="lean-scale"
          title={`measured ${ends[0]}–${ends[1]}: ${value > 0 ? "+" : ""}${value}`}>
      <span className="lean-track">
        <span className="lean-mid" />
        <span className="lean-marker" style={{ left: `${pct}%` }} />
      </span>
      <span className="lean-ends" aria-hidden="true">
        <span>{ends[0]}</span>
        <span>{ends[1]}</span>
      </span>
    </span>
  );
}

function LeanPair({ lean }) {
  const lr = axisValue(lean, "lr");
  const lc = axisValue(lean, "lc");
  if (lr == null && lc == null) return <span className="lean-none">–</span>;
  return (
    <span className="lean-pair">
      <LeanScale value={lr} ends={["left", "right"]} />
      <LeanScale value={lc} ends={["liberal", "conserv."]} />
    </span>
  );
}

function OutletTable({ story, outlets }) {
  const framingBySource = {};
  for (const f of story.report?.outlet_framings || []) {
    framingBySource[f.source] = f.framing;
  }
  return (
    <div className="news-outlets">
      <div className="news-section-eyebrow">How each outlet told it</div>
      {story.items.map((it) => {
        const meta = outlets?.[it.source] || { name: it.source, stance: "", paywalled: false };
        return (
          <div key={it.source + it.link} className="news-outlet-row">
            <div className="news-outlet-id">
              <a href={it.link} target="_blank" rel="noopener noreferrer"
                 className="news-outlet-name" title={it.title}>
                {meta.name}
              </a>
              {meta.paywalled && (
                <span className="news-paywall" title="paywalled — council saw the snippet only">🔒</span>
              )}
              <span className="stance-badge" title="declared editorial stance">
                {meta.stance}
              </span>
            </div>
            <div className="news-outlet-headline">“{it.title}”</div>
            <LeanPair lean={story.leans?.[it.source]} />
            {framingBySource[it.source] && (
              <div className="news-outlet-framing">{framingBySource[it.source]}</div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function CouncilDetail({ story }) {
  const [open, setOpen] = useState(false);
  const finals = story.rebuttals?.length ? story.rebuttals : story.assessments;
  if (!finals?.length) return null;
  return (
    <div className={`news-council ${open ? "is-open" : ""}`}>
      <button type="button" className="news-council-toggle"
              onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <span className={`round-chevron ${open ? "is-open" : ""}`}>▸</span>
        {open ? "Hide the council's debate" : `Read the council's debate (${finals.length} voices)`}
      </button>
      {open && (
        <div className="debate-round-grid">
          {finals.map((a) => (
            <div key={a.name} className="debate-card tone-aqua">
              <div className="debate-card-head">
                <span className="model-toggle-provider">{a.provider}</span>
                <span className="debate-card-name">{a.name}</span>
                <span className="debate-card-vote">confidence {a.confidence}</span>
              </div>
              <div className="debate-card-reason">{linkify(a.account)}</div>
              {a.rebuttal && (
                <div className="news-rebuttal">
                  <span className="news-rebuttal-mark">↩</span> {linkify(a.rebuttal)}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

const STORY_STATUS_HINT = {
  assessing: "the council is reading the coverage…",
  rebuttal: "rebuttal round — voices challenge each other…",
  judging: "the judge is writing the report…",
  failed: "the council failed on this story",
};

function StoryCard({ story, index, outlets, panel, toolCalls, running }) {
  const busy = running && ["assessing", "rebuttal", "judging"].includes(story.status);
  const missing = (panel && story.voices)
    ? panel.filter((name) => !story.voices.includes(name))
    : [];
  return (
    <article className={`news-story ${busy ? "is-busy" : ""}`}>
      <header className="news-story-head">
        <span className="news-story-num">{index + 1}</span>
        <h2 className="news-story-title">{story.title}</h2>
        {missing.length > 0 && (
          <span className="news-story-status is-degraded"
                title={`No answer from: ${missing.join(", ")}`}>
            {story.voices.length}/{panel.length} voices
          </span>
        )}
        {busy && <span className="news-story-status">{STORY_STATUS_HINT[story.status]}</span>}
        {!running && story.status === "failed" && (
          <span className="news-story-status is-failed">{STORY_STATUS_HINT.failed}</span>
        )}
      </header>

      {story.report?.consensus && (
        <div className="news-consensus">
          <div className="news-section-eyebrow">What we can actually say happened</div>
          <p className="news-consensus-body">{linkify(story.report.consensus)}</p>
        </div>
      )}

      <OutletTable story={story} outlets={outlets} />

      {story.report?.disagreements?.length > 0 && (
        <div className="news-disagreements">
          <div className="news-section-eyebrow">Where they don't agree</div>
          <ul>
            {story.report.disagreements.map((d, i) => (
              <li key={i}>⚡ {d}</li>
            ))}
          </ul>
        </div>
      )}

      <FactCheckList checks={story.report?.fact_checks} outlets={outlets} />

      {busy && toolCalls?.length > 0 && (
        <div className="news-toolstrip">
          {toolCalls.slice(-4).map((c, i) => (
            <span key={i} className="tool-badge">
              {c.tool === "web_search"
                ? `🔎 ${c.agent} searched “${(c.query || "").slice(0, 40)}”`
                : `🌐 ${c.agent} read ${hostOf(c.url || "")}`}
            </span>
          ))}
        </div>
      )}

      <CouncilDetail story={story} />
    </article>
  );
}

function Blindspots({ items, outlets }) {
  if (!items?.length) return null;
  return (
    <section className="news-blindspots">
      <div className="news-section-eyebrow">Blindspots — only one outlet has these</div>
      <ul>
        {items.map((it, i) => (
          <li key={i}>
            <span className="news-blindspot-outlet">
              {outlets?.[it.source]?.name || it.source}
            </span>
            <a href={it.link} target="_blank" rel="noopener noreferrer">
              {it.title}
            </a>
          </li>
        ))}
      </ul>
    </section>
  );
}

function EditionHeader({ edition, slot }) {
  const counts = edition?.sources || {};
  const errors = edition?.source_errors || {};
  const outlets = edition?.outlets || {};
  return (
    <header className="news-edition-head">
      <h1 className="motion-headline">
        <span className="motion-eyebrow">The papers, read by the council</span>
        <span className="motion-quote">{slotLabel(edition?.slot || slot)}</span>
      </h1>
      {Object.keys(counts).length > 0 && (
        <div className="news-sources-strip">
          {Object.entries(counts).map(([sid, n]) => (
            <span key={sid} className="news-source-chip" title={outlets[sid]?.stance}>
              {outlets[sid]?.name || sid} · {n}
            </span>
          ))}
          {Object.entries(errors).map(([sid, err]) => (
            <span key={sid} className="news-source-chip is-dead" title={err}>
              {outlets[sid]?.name || sid} · unreachable
            </span>
          ))}
        </div>
      )}
    </header>
  );
}

function reducer(state, ev) {
  switch (ev.kind) {
    case "start":
      return { ...state, running: true, step: "_default", snapshot: null, toolCalls: {}, notes: [] };
    case "agui": {
      const e = ev.event;
      switch (e.type) {
        case "STEP_STARTED":
          return { ...state, step: e.stepName };
        case "STATE_SNAPSHOT":
          return { ...state, snapshot: e.snapshot };
        case "CUSTOM":
          if (e.name === "tool_call") {
            const key = String(e.value.story ?? "desk");
            const list = state.toolCalls[key] || [];
            return { ...state, toolCalls: { ...state.toolCalls, [key]: [...list, e.value] } };
          }
          if (e.name === "panelist_error") {
            return {
              ...state,
              notes: [...state.notes,
                `${e.value.name} dropped out on story ${(e.value.story ?? 0) + 1}.`],
            };
          }
          return state;
        case "RUN_FINISHED":
          return { ...state, running: false, step: null };
        case "RUN_ERROR":
          return { ...state, running: false, step: null,
                   notes: [...state.notes, `Run failed: ${e.message}`] };
        default:
          return state;
      }
    }
    case "fail":
      return { ...state, running: false, step: null, notes: [...state.notes, ev.text] };
    default:
      return state;
  }
}

export default function NewsView() {
  const [latest, setLatest] = useState(null); // GET /news/latest response
  const [editions, setEditions] = useState([]); // archive index
  const [archive, setArchive] = useState(null); // {slot, edition} being browsed
  const [live, dispatch] = useReducer(reducer, {
    running: false, step: null, snapshot: null, toolCalls: {}, notes: [],
  });
  const threadId = useRef(crypto.randomUUID());

  async function refresh() {
    try {
      const r = await fetch("/news/latest");
      setLatest(await r.json());
    } catch {
      setLatest({ status: "unreachable" });
    }
    try {
      const r = await fetch("/news/editions");
      if (r.ok) setEditions(await r.json());
    } catch {}
  }

  async function openEdition(slot) {
    if (!slot) {
      setArchive(null);
      return;
    }
    try {
      const r = await fetch(`/news/editions/${slot}`);
      if (r.ok) setArchive(await r.json());
    } catch {}
  }

  useEffect(() => {
    refresh();
  }, []);

  async function generate() {
    dispatch({ kind: "start" });
    try {
      await runNews({
        threadId: threadId.current,
        onEvent: (event) => dispatch({ kind: "agui", event }),
      });
    } catch (err) {
      dispatch({ kind: "fail", text: `Could not generate: ${err.message}` });
    }
    refresh();
  }

  // Someone else triggered this edition: attach to the live run and
  // watch it stream in. watchOnly never starts a new generation — if the
  // run turns out to be dead, the backend frees the slot and the refresh
  // brings back the call-to-order button.
  async function watchLive() {
    dispatch({ kind: "start" });
    try {
      await runNews({
        threadId: threadId.current,
        watchOnly: true,
        onEvent: (event) => {
          if (event.type === "RUN_ERROR") return; // dead run — stay quiet
          dispatch({ kind: "agui", event });
        },
      });
    } catch {}
    dispatch({ kind: "agui", event: { type: "RUN_FINISHED" } });
    refresh();
  }

  useEffect(() => {
    if (latest?.status === "running" && !live.running) watchLive();
  }, [latest?.status]);

  // What to render: the live run's snapshot wins, then an archive pick,
  // then the cached edition, then the previous edition as a fallback
  // while the current one pends.
  const edition =
    live.snapshot || archive?.edition || latest?.edition ||
    latest?.previous?.edition;
  const showingPrevious =
    !live.snapshot && !archive && !latest?.edition && !!latest?.previous;
  const pending = !live.running && latest && latest.status !== "done" && latest.status !== "unreachable";

  if (!latest && !live.running) {
    return (
      <main className="transcript news-transcript">
        <Spinner step="_default" />
      </main>
    );
  }

  return (
    <main className="transcript news-transcript">
      {pending && (
        <div className="news-pending">
          {latest.status === "running" ? (
            <p className="composer-caption">
              The council is in session — another reader called this edition
              to order. It will appear here when the judge is done.
            </p>
          ) : (
            <>
              <p className="composer-caption">
                {latest.status === "failed"
                  ? "The last attempt at this edition failed — call the council to order again."
                  : "This edition has not been written yet. The first reader to call the council to order gets to watch it live."}
              </p>
              <button className="btn-gilded" onClick={generate}>
                Call the council to order
              </button>
            </>
          )}
        </div>
      )}

      {latest?.status === "unreachable" && (
        <div className="note">Could not reach the backend.</div>
      )}

      {editions.length > 0 && !live.running && (
        <div className="news-archive-strip">
          <button
            className={`news-archive-chip ${!archive ? "is-on" : ""}`}
            onClick={() => openEdition(null)}
          >
            Latest
          </button>
          {editions.map((e) => (
            <button
              key={e.slot}
              className={`news-archive-chip ${archive?.slot === e.slot ? "is-on" : ""}`}
              onClick={() => openEdition(e.slot)}
            >
              {slotShort(e.slot)}
            </button>
          ))}
        </div>
      )}

      {showingPrevious && edition && (
        <div className="note">
          Showing the previous edition while this one pends.
        </div>
      )}

      {edition && (
        <>
          <EditionHeader edition={edition} slot={latest?.slot} />
          {live.notes.map((t, i) => (
            <div key={i} className="note">{t}</div>
          ))}
          {(edition.stories || []).map((story, i) => (
            <StoryCard
              key={i}
              story={story}
              index={i}
              outlets={edition.outlets}
              panel={edition.panel}
              toolCalls={live.toolCalls[String(i)]}
              running={live.running}
            />
          ))}
          <Blindspots items={edition.blindspots} outlets={edition.outlets} />
        </>
      )}

      {live.running && <Spinner step={live.step} />}
    </main>
  );
}
