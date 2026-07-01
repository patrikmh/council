"""SQLite-backed stats store.

Two tables:
  runs    — one row per completed run (question, mode, thread_id, winner)
  ballots — one row per (run, model, round). Debate mode records the prior
            round's vote in `flipped_from`, so we can compute how often
            other models flipped *toward* a given model.

`influence_score = wins + 2 * flips_toward` over a rolling window.
"""

import os
import time

import aiosqlite

DB_PATH = os.getenv("RABBLE_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "rabble.db"))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id    TEXT NOT NULL,
    mode         TEXT NOT NULL,           -- 'poll' | 'debate'
    question     TEXT NOT NULL,
    winner       TEXT,                    -- winning option label (may be tie: first alphabetical)
    created_at   INTEGER NOT NULL         -- unix seconds
);

CREATE INDEX IF NOT EXISTS runs_created_at ON runs(created_at);

CREATE TABLE IF NOT EXISTS ballots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    model_name    TEXT NOT NULL,
    provider      TEXT NOT NULL,
    round_index   INTEGER NOT NULL,       -- 0 = initial round
    vote          TEXT NOT NULL,
    flipped_from  TEXT,                   -- prior-round vote if changed, else NULL
    reasoning     TEXT
);

CREATE INDEX IF NOT EXISTS ballots_run   ON ballots(run_id);
CREATE INDEX IF NOT EXISTS ballots_model ON ballots(model_name);
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        await db.commit()


async def record_run(
    thread_id: str,
    mode: str,
    question: str,
    winner: str | None,
    ballots: list[dict],
) -> int:
    """Insert a run and its ballots. Returns the run id.

    Each ballot dict: {name, provider, round_index, vote, flipped_from, reasoning}
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO runs (thread_id, mode, question, winner, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (thread_id, mode, question, winner, int(time.time())),
        )
        run_id = cur.lastrowid
        await db.executemany(
            "INSERT INTO ballots "
            "(run_id, model_name, provider, round_index, vote, flipped_from, reasoning) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    b["name"],
                    b.get("provider", ""),
                    b["round_index"],
                    b["vote"],
                    b.get("flipped_from"),
                    (b.get("reasoning") or "")[:500],
                )
                for b in ballots
            ],
        )
        await db.commit()
        return int(run_id)


async def leaderboard(days: int | None = 30) -> list[dict]:
    """Compute the model influence leaderboard.

    Wins = final-round ballots on the winning option.
    Flips-toward = other panelists whose next-round vote matched this model's
    prior-round vote AND changed from what they had before.
    """
    cutoff = int(time.time() - days * 86400) if days else 0

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cast_rows = await db.execute_fetchall(
            """
            SELECT b.model_name, MAX(b.provider) AS provider, COUNT(*) AS ballots_cast
            FROM ballots b JOIN runs r ON r.id = b.run_id
            WHERE r.created_at >= ?
            GROUP BY b.model_name
            """,
            (cutoff,),
        )
        win_rows = await db.execute_fetchall(
            """
            SELECT b.model_name, COUNT(*) AS wins
            FROM ballots b
            JOIN runs r ON r.id = b.run_id
            WHERE r.created_at >= ?
              AND b.round_index = (SELECT MAX(round_index) FROM ballots WHERE run_id = b.run_id)
              AND b.vote = r.winner
            GROUP BY b.model_name
            """,
            (cutoff,),
        )
        # Flips toward: count ballots where flipped_from IS NOT NULL and the
        # new vote matches what some other panelist voted in the prior round
        # of the same run. Credit each such prior voter (max one credit per flip).
        flip_rows = await db.execute_fetchall(
            """
            SELECT prior.model_name AS model_name, COUNT(DISTINCT flipper.id) AS flips_toward
            FROM ballots flipper
            JOIN ballots prior
              ON prior.run_id = flipper.run_id
             AND prior.round_index = flipper.round_index - 1
             AND prior.model_name != flipper.model_name
             AND prior.vote = flipper.vote
            JOIN runs r ON r.id = flipper.run_id
            WHERE flipper.flipped_from IS NOT NULL
              AND r.created_at >= ?
            GROUP BY prior.model_name
            """,
            (cutoff,),
        )

        wins_by = {r["model_name"]: r["wins"] for r in win_rows}
        flips_by = {r["model_name"]: r["flips_toward"] for r in flip_rows}

        out = []
        for r in cast_rows:
            name = r["model_name"]
            wins = int(wins_by.get(name, 0))
            flips = int(flips_by.get(name, 0))
            out.append({
                "name": name,
                "provider": r["provider"],
                "ballots_cast": int(r["ballots_cast"]),
                "wins": wins,
                "flips_toward": flips,
                "influence_score": wins + 2 * flips,
            })
        out.sort(key=lambda x: (-x["influence_score"], -x["wins"], x["name"]))
        return out


async def recent_questions(limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT question, mode, winner, created_at "
            "FROM runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]
