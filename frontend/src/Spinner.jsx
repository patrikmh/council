// A spinner that rotates through playful captions per step, plus a
// braille-dot glyph so there's always something moving. Every 2.5 s a
// fresh caption from the step's pool takes over. Passing an unknown step
// falls back to a generic "the table is deliberating…" pool.

import React, { useEffect, useState } from "react";

const DOTS = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

const POOLS = {
  frame_question: [
    "Framing the motion…",
    "Sharpening the pencils…",
    "Consulting Robert's Rules…",
    "Chalking the options on the board…",
    "The chair is clearing their throat…",
  ],
  collect_ballots: [
    "The table is voting…",
    "Ballots are being folded…",
    "A panelist is chewing their pen…",
    "Someone is triple-checking their answer…",
    "Quiet — the room is thinking…",
    "One panelist keeps rewriting their sentence…",
  ],
  summarize: [
    "Drafting the summary…",
    "The stenographer is catching up…",
    "The chair is scribbling minutes…",
    "Reading it back for the record…",
  ],
  _debate_round: [
    "panelists debating…",
    "a rebuttal is being sharpened…",
    "someone just flipped and everyone noticed…",
    "the room is tense…",
    "a panelist is checking their sources…",
    "eyes are being rolled…",
    "a voice was raised, then lowered again…",
    "a footnote was demanded…",
    "someone is quietly Googling…",
    "the chair called for order…",
  ],
  _default: [
    "The table is deliberating…",
    "Working…",
    "A moment…",
  ],
};

function poolFor(step) {
  if (POOLS[step]) return { pool: POOLS[step], prefix: "" };
  const m = step && step.match(/^round_(\d+)$/);
  if (m) return { pool: POOLS._debate_round, prefix: `Round ${m[1]} — ` };
  return { pool: POOLS._default, prefix: "" };
}

export default function Spinner({ step }) {
  const [tick, setTick] = useState(0);
  const [phraseIdx, setPhraseIdx] = useState(0);

  useEffect(() => {
    setPhraseIdx(Math.floor(Math.random() * 1000));
  }, [step]);

  useEffect(() => {
    const dotTimer = setInterval(() => setTick((t) => t + 1), 90);
    const phraseTimer = setInterval(() => setPhraseIdx((i) => i + 1), 2500);
    return () => {
      clearInterval(dotTimer);
      clearInterval(phraseTimer);
    };
  }, []);

  const { pool, prefix } = poolFor(step);
  const phrase = pool[phraseIdx % pool.length];
  const dot = DOTS[tick % DOTS.length];

  return (
    <div className="status">
      <span className="status-dot" aria-hidden="true">{dot}</span>
      <span className="status-text">{prefix}{phrase}</span>
    </div>
  );
}
