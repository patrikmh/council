"""SQLite-backed stats store.

Two tables:
  runs    — one row per completed run (question, mode, thread_id, winner,
            plus round0_winner/judge_winner for debate runs so we can
            measure whether debate actually changed the outcome)
  ballots — one row per (run, model, round). Debate mode records the prior
            round's vote in `flipped_from`, so we can compute how often
            other models flipped *toward* a given model.

`influence_score = wins + 2 * flips_toward` over a rolling window. A flip
only credits the attractor if it *sticks* — the flipper's final-round vote
must still match — so a panelist who flips back later grants no credit.
"""

import json as _json
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

CREATE TABLE IF NOT EXISTS news_editions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slot         TEXT NOT NULL UNIQUE,    -- '2026-07-07-morning' | '-evening'
    status       TEXT NOT NULL,           -- 'running' | 'done' | 'failed'
    started_at   INTEGER NOT NULL,
    finished_at  INTEGER,
    payload      TEXT                     -- full edition JSON when done
);
"""

# Columns added after the initial schema shipped. SQLite has no
# ADD COLUMN IF NOT EXISTS, so each is applied best-effort on startup.
_MIGRATIONS = [
    "ALTER TABLE runs ADD COLUMN round0_winner TEXT",   # round-0 majority option
    "ALTER TABLE runs ADD COLUMN judge_winner TEXT",    # judge's quality verdict
    "ALTER TABLE ballots ADD COLUMN confidence INTEGER",
    "ALTER TABLE ballots ADD COLUMN role TEXT",         # 'dissenter' or NULL
]


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                await db.execute(stmt)
            except aiosqlite.OperationalError:
                pass  # column already exists
        await db.commit()


async def record_run(
    thread_id: str,
    mode: str,
    question: str,
    winner: str | None,
    ballots: list[dict],
    round0_winner: str | None = None,
    judge_winner: str | None = None,
) -> int:
    """Insert a run and its ballots. Returns the run id.

    Each ballot dict: {name, provider, round_index, vote, flipped_from,
    reasoning, confidence?, role?}
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO runs (thread_id, mode, question, winner, "
            "round0_winner, judge_winner, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (thread_id, mode, question, winner,
             round0_winner, judge_winner, int(time.time())),
        )
        run_id = cur.lastrowid
        await db.executemany(
            "INSERT INTO ballots "
            "(run_id, model_name, provider, round_index, vote, flipped_from, "
            "reasoning, confidence, role) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    b["name"],
                    b.get("provider", ""),
                    b["round_index"],
                    b["vote"],
                    b.get("flipped_from"),
                    (b.get("reasoning") or "")[:500],
                    b.get("confidence"),
                    b.get("role"),
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
        # of the same run. Credit each such prior voter (max one credit per
        # flip) — but only when the flip STICKS: the flipper's final-round
        # vote must still be the flipped-to vote, so a flip that later flips
        # back grants the original attractor nothing.
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
              AND flipper.vote = (
                    SELECT b2.vote FROM ballots b2
                    WHERE b2.run_id = flipper.run_id
                      AND b2.model_name = flipper.model_name
                    ORDER BY b2.round_index DESC LIMIT 1
              )
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


async def debate_delta(days: int | None = 30) -> dict:
    """Does debate actually change the outcome? Over the window, compare
    each debate run's round-0 majority against the final tally winner and
    the judge's quality verdict. Also flags runs where confidence-weighting
    the round-0 ballots would already have picked a different option than
    the raw round-0 majority."""
    cutoff = int(time.time() - days * 86400) if days else 0

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        runs = await db.execute_fetchall(
            """
            SELECT id, winner, round0_winner, judge_winner
            FROM runs
            WHERE mode = 'debate' AND created_at >= ?
              AND round0_winner IS NOT NULL
            """,
            (cutoff,),
        )
        round0_ballots = await db.execute_fetchall(
            """
            SELECT b.run_id, b.vote, b.confidence
            FROM ballots b JOIN runs r ON r.id = b.run_id
            WHERE r.mode = 'debate' AND r.created_at >= ?
              AND b.round_index = 0 AND b.confidence IS NOT NULL
            """,
            (cutoff,),
        )

    weighted_by_run: dict[int, dict[str, int]] = {}
    for b in round0_ballots:
        totals = weighted_by_run.setdefault(b["run_id"], {})
        totals[b["vote"]] = totals.get(b["vote"], 0) + b["confidence"]

    total = len(runs)
    changed = sum(1 for r in runs if r["winner"] and r["winner"] != r["round0_winner"])
    judged = [r for r in runs if r["judge_winner"] and r["winner"]]
    judge_disagreed = sum(1 for r in judged if r["judge_winner"] != r["winner"])
    weighted_diverged = 0
    for r in runs:
        totals = weighted_by_run.get(r["id"])
        if not totals:
            continue
        weighted_winner = max(totals, key=lambda opt: totals[opt])
        if weighted_winner != r["round0_winner"]:
            weighted_diverged += 1

    def pct(n: int, of: int) -> float | None:
        return round(100 * n / of, 1) if of else None

    return {
        "debate_runs": total,
        "changed_by_debate": changed,
        "changed_by_debate_pct": pct(changed, total),
        "judge_disagreed_with_tally": judge_disagreed,
        "judge_disagreed_pct": pct(judge_disagreed, len(judged)),
        "confidence_weighted_round0_diverged": weighted_diverged,
    }


# ── News editions ────────────────────────────────────────────────────────────
# On-demand with cache: the first visitor after an edition boundary claims
# the slot and generates; everyone else reads the stored payload. The slot
# row doubles as the lock.

# A 'running' edition older than this is presumed dead (the triggering
# visitor disconnected, the process restarted) and may be reclaimed.
NEWS_STALE_RUNNING_SEC = int(os.getenv("NEWS_STALE_RUNNING_SEC", "1800"))


def _edition_row(row) -> dict:
    return {
        "slot": row["slot"],
        "status": row["status"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "payload": _json.loads(row["payload"]) if row["payload"] else None,
    }


async def news_get(slot: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM news_editions WHERE slot = ?", (slot,))
        return _edition_row(rows[0]) if rows else None


async def news_latest_done() -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM news_editions WHERE status = 'done' "
            "ORDER BY finished_at DESC LIMIT 1")
        return _edition_row(rows[0]) if rows else None


async def news_claim(slot: str) -> bool:
    """Claim a slot for generation. True = caller may generate. A slot that
    is done, or running and fresh, cannot be claimed; failed or stale-running
    slots are reclaimed so a crashed run doesn't block the edition forever."""
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT status, started_at FROM news_editions WHERE slot = ?", (slot,))
        if rows:
            row = rows[0]
            if row["status"] == "done":
                return False
            if (row["status"] == "running"
                    and now - row["started_at"] < NEWS_STALE_RUNNING_SEC):
                return False
            await db.execute(
                "UPDATE news_editions SET status = 'running', started_at = ?, "
                "finished_at = NULL, payload = NULL WHERE slot = ?",
                (now, slot))
            await db.commit()
            return True
        cur = await db.execute(
            "INSERT OR IGNORE INTO news_editions (slot, status, started_at) "
            "VALUES (?, 'running', ?)", (slot, now))
        await db.commit()
        return cur.rowcount == 1


async def news_finish(slot: str, payload: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE news_editions SET status = 'done', finished_at = ?, "
            "payload = ? WHERE slot = ?",
            (int(time.time()), _json.dumps(payload, ensure_ascii=False), slot))
        await db.commit()


async def news_fail(slot: str, error: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE news_editions SET status = 'failed', finished_at = ?, "
            "payload = ? WHERE slot = ?",
            (int(time.time()), _json.dumps({"error": error[:300]}), slot))
        await db.commit()


async def recent_questions(limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT question, mode, winner, created_at "
            "FROM runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]
