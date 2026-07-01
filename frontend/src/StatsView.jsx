// Stats view: influence leaderboard + recent public questions.

import React, { useEffect, useState } from "react";

const WINDOWS = [
  { label: "7d", days: 7 },
  { label: "30d", days: 30 },
  { label: "All", days: 3650 },
];

function ago(ts) {
  const s = Math.max(1, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export default function StatsView() {
  const [days, setDays] = useState(30);
  const [board, setBoard] = useState(null);
  const [questions, setQuestions] = useState(null);
  const [feedDisabled, setFeedDisabled] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setError("");
    setBoard(null);
    fetch(`/stats/leaderboard?days=${days}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((data) => !cancelled && setBoard(data))
      .catch((e) => !cancelled && setError(`Leaderboard failed (${e})`));
    return () => {
      cancelled = true;
    };
  }, [days]);

  useEffect(() => {
    let cancelled = false;
    fetch(`/stats/questions?limit=50`)
      .then((r) => {
        if (r.status === 404) {
          setFeedDisabled(true);
          return [];
        }
        return r.ok ? r.json() : Promise.reject(r.status);
      })
      .then((data) => !cancelled && setQuestions(data))
      .catch(() => !cancelled && setQuestions([]));
    return () => {
      cancelled = true;
    };
  }, []);

  const maxScore = Math.max(1, ...(board || []).map((r) => r.influence_score));

  return (
    <main className="transcript stats-view">
      <section className="stats-section">
        <div className="stats-head">
          <h2>Influence leaderboard</h2>
          <div className="stats-window">
            {WINDOWS.map((w) => (
              <button
                key={w.days}
                className={`stats-window-btn ${days === w.days ? "is-on" : ""}`}
                onClick={() => setDays(w.days)}
              >
                {w.label}
              </button>
            ))}
          </div>
        </div>
        {error && <div className="note">{error}</div>}
        {board === null && !error && <div className="status">Loading…</div>}
        {board && board.length === 0 && (
          <div className="note">No ballots recorded in this window yet.</div>
        )}
        {board && board.length > 0 && (
          <table className="stats-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Model</th>
                <th>Ballots</th>
                <th>Wins</th>
                <th>Flips toward</th>
                <th>Influence</th>
              </tr>
            </thead>
            <tbody>
              {board.map((row, i) => (
                <tr key={row.name}>
                  <td>{i + 1}</td>
                  <td>
                    <span className="model-toggle-provider">{row.provider}</span>
                    {row.name}
                  </td>
                  <td>{row.ballots_cast}</td>
                  <td>{row.wins}</td>
                  <td>{row.flips_toward}</td>
                  <td>
                    <div className="score-cell">
                      <div
                        className="score-bar"
                        style={{ width: `${(100 * row.influence_score) / maxScore}%` }}
                      />
                      <span>{row.influence_score}</span>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="stats-hint">
          Influence = wins + 2 × (times another panelist flipped toward this
          model's vote in the next debate round).
        </p>
      </section>

      <section className="stats-section">
        <h2>Recent questions</h2>
        {feedDisabled && (
          <div className="note">The public feed is disabled on this deployment.</div>
        )}
        {!feedDisabled && questions === null && (
          <div className="status">Loading…</div>
        )}
        {!feedDisabled && questions && questions.length === 0 && (
          <div className="note">No questions yet — ask the first one.</div>
        )}
        {!feedDisabled && questions && questions.length > 0 && (
          <ul className="questions-feed">
            {questions.map((q, i) => (
              <li key={i}>
                <span className={`mode-badge mode-${q.mode}`}>{q.mode}</span>
                <span className="q-text">{q.question}</span>
                <span className="q-meta">
                  {q.winner ? `→ ${q.winner}` : "—"} · {ago(q.created_at)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}
