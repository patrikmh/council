"""store.py: leaderboard math, debate delta, news slot locking."""

import time


async def _seed_debate(store, winner="Sweden"):
    """One debate: Beta flips toward Alpha's option in round 1 and sticks."""
    await store.init_db()
    return await store.record_run(
        thread_id="t1",
        mode="debate",
        question="q",
        winner=winner,
        round0_winner="Norway",
        judge_winner="Sweden",
        ballots=[
            {"name": "Alpha", "provider": "test", "round_index": 0,
             "vote": "Sweden", "flipped_from": None, "reasoning": "",
             "confidence": 90},
            {"name": "Beta", "provider": "test", "round_index": 0,
             "vote": "Norway", "flipped_from": None, "reasoning": "",
             "confidence": 40},
            {"name": "Alpha", "provider": "test", "round_index": 1,
             "vote": "Sweden", "flipped_from": None, "reasoning": ""},
            {"name": "Beta", "provider": "test", "round_index": 1,
             "vote": "Sweden", "flipped_from": "Norway", "reasoning": ""},
        ],
    )


async def test_leaderboard_wins_and_sticking_flip(tmp_db):
    await _seed_debate(tmp_db)
    rows = {r["name"]: r for r in await tmp_db.leaderboard(days=1)}
    # Both ended on the winner; only Alpha attracted a flip that stuck.
    assert rows["Alpha"]["wins"] == 1
    assert rows["Beta"]["wins"] == 1
    assert rows["Alpha"]["flips_toward"] == 1
    assert rows["Beta"]["flips_toward"] == 0
    assert rows["Alpha"]["influence_score"] == 1 + 2 * 1


async def test_flip_that_flips_back_grants_no_credit(tmp_db):
    await tmp_db.init_db()
    await tmp_db.record_run(
        thread_id="t2", mode="debate", question="q", winner="Norway",
        round0_winner="Norway",
        ballots=[
            {"name": "Alpha", "provider": "t", "round_index": 0,
             "vote": "Sweden", "flipped_from": None, "reasoning": ""},
            {"name": "Beta", "provider": "t", "round_index": 0,
             "vote": "Norway", "flipped_from": None, "reasoning": ""},
            # Beta flips to Sweden…
            {"name": "Beta", "provider": "t", "round_index": 1,
             "vote": "Sweden", "flipped_from": "Norway", "reasoning": ""},
            {"name": "Alpha", "provider": "t", "round_index": 1,
             "vote": "Sweden", "flipped_from": None, "reasoning": ""},
            # …then flips back. Alpha gets nothing.
            {"name": "Beta", "provider": "t", "round_index": 2,
             "vote": "Norway", "flipped_from": "Sweden", "reasoning": ""},
            {"name": "Alpha", "provider": "t", "round_index": 2,
             "vote": "Sweden", "flipped_from": None, "reasoning": ""},
        ],
    )
    rows = {r["name"]: r for r in await tmp_db.leaderboard(days=1)}
    assert rows["Alpha"]["flips_toward"] == 0


async def test_debate_delta_counts_changes(tmp_db):
    await _seed_debate(tmp_db)  # round0 Norway → final Sweden
    delta = await tmp_db.debate_delta(days=1)
    assert delta["debate_runs"] == 1
    assert delta["changed_by_debate"] == 1
    assert delta["judge_disagreed_with_tally"] == 0
    # Round-0 confidence weighting: Sweden 90 vs Norway 40 → diverges from
    # the raw round-0 majority (Norway).
    assert delta["confidence_weighted_round0_diverged"] == 1


# ── news slot locking ────────────────────────────────────────────────────


async def test_news_claim_lifecycle(tmp_db):
    await tmp_db.init_db()
    slot = "2026-07-07-morning"
    assert await tmp_db.news_claim(slot) is True       # first visitor
    assert await tmp_db.news_claim(slot) is False      # fresh 'running'
    await tmp_db.news_finish(slot, {"stories": []})
    assert await tmp_db.news_claim(slot) is False      # done is final
    row = await tmp_db.news_get(slot)
    assert row["status"] == "done"
    assert row["payload"] == {"stories": []}


async def test_news_failed_and_stale_runs_are_reclaimed(tmp_db):
    await tmp_db.init_db()
    slot = "2026-07-07-evening"
    assert await tmp_db.news_claim(slot)
    await tmp_db.news_fail(slot, "boom")
    assert await tmp_db.news_claim(slot) is True       # failed → reclaim

    # Stale 'running': backdate started_at beyond the threshold.
    import aiosqlite

    async with aiosqlite.connect(tmp_db.DB_PATH) as db:
        await db.execute(
            "UPDATE news_editions SET started_at = ? WHERE slot = ?",
            (int(time.time()) - tmp_db.NEWS_STALE_RUNNING_SEC - 1, slot))
        await db.commit()
    assert await tmp_db.news_claim(slot) is True


async def test_news_prune_drops_old_editions(tmp_db):
    await tmp_db.init_db()
    await tmp_db.news_claim("old-slot")
    await tmp_db.news_finish("old-slot", {"stories": []})
    import aiosqlite

    async with aiosqlite.connect(tmp_db.DB_PATH) as db:
        await db.execute(
            "UPDATE news_editions SET finished_at = ?",
            (int(time.time()) - 10 * 86400,))
        await db.commit()
    await tmp_db.news_prune(days=7)
    assert await tmp_db.news_get("old-slot") is None
