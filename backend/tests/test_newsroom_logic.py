"""newsroom.py + debate.py + memory.py pure logic."""

import datetime as dt
from zoneinfo import ZoneInfo

from app.debate import _leading_option, _map_names
from app.memory import RunMemory
from app.newsroom import (
    Desk,
    LEAN_QUORUM,
    MAX_BLINDSPOTS,
    MAX_STORIES,
    Story,
    _resolve,
    current_slot,
    measured_leans,
)

STOCKHOLM = ZoneInfo("Europe/Stockholm")


def _at(hour: int) -> dt.datetime:
    return dt.datetime(2026, 7, 7, hour, 0, tzinfo=STOCKHOLM)


# ── current_slot ─────────────────────────────────────────────────────────


def test_slot_boundaries():
    assert current_slot(_at(8)) == "2026-07-06-evening"
    assert current_slot(_at(9)) == "2026-07-07-morning"
    assert current_slot(_at(17)) == "2026-07-07-morning"
    assert current_slot(_at(18)) == "2026-07-07-evening"
    assert current_slot(_at(23)) == "2026-07-07-evening"


# ── _resolve: the desk proposes, code disposes ───────────────────────────


def _items(*sources):
    return [{"source": s, "title": f"t{i}", "summary": "", "link": ""}
            for i, s in enumerate(sources)]


def test_resolve_requires_two_distinct_outlets():
    items = _items("dn", "dn", "svt")
    desk = Desk(
        stories=[
            Story(title="same outlet twice", item_numbers=[1, 2]),
            Story(title="two outlets", item_numbers=[1, 3]),
        ],
        blindspots=[],
    )
    out = _resolve(desk, items)
    assert [s["title"] for s in out["stories"]] == ["two outlets"]


def test_resolve_never_reuses_an_item():
    items = _items("dn", "svt", "etc")
    desk = Desk(
        stories=[
            Story(title="first", item_numbers=[1, 2]),
            # Item 1 is spent; only item 3 remains → single outlet → dropped.
            Story(title="second", item_numbers=[1, 3]),
        ],
        blindspots=[1, 3],  # 1 is used; 3 is free
    )
    out = _resolve(desk, items)
    assert [s["title"] for s in out["stories"]] == ["first"]
    assert [b["source"] for b in out["blindspots"]] == ["etc"]


def test_resolve_ignores_out_of_range_numbers_and_caps():
    items = _items(*(["dn", "svt"] * (MAX_STORIES + 3)))
    stories = [
        Story(title=f"s{i}", item_numbers=[2 * i + 1, 2 * i + 2])
        for i in range(MAX_STORIES + 3)
    ]
    desk = Desk(stories=stories, blindspots=[999, 0, -3])
    out = _resolve(desk, items)
    assert len(out["stories"]) == MAX_STORIES
    assert out["blindspots"] == []


# ── measured_leans ───────────────────────────────────────────────────────


def _assessment(*reads):
    return {"outlet_reads": [
        {"source": s, "lean": lr, "social": lc} for s, lr, lc in reads]}


def test_leans_median_not_mean():
    final = [
        _assessment(("dn", -2, 0)),
        _assessment(("dn", 0, 0)),
        _assessment(("dn", 0, 0)),
    ]
    # Median resists the one outlier; a mean would be dragged to -0.67.
    assert measured_leans(final)["dn"]["lr"] == 0


def test_leans_quorum_suppresses_single_rater():
    assert LEAN_QUORUM >= 2
    final = [
        _assessment(("dn", -1, 0), ("etc", -2, -2)),
        _assessment(("dn", -1, 0)),
    ]
    out = measured_leans(final)
    assert "dn" in out and out["dn"]["n"] == 2
    assert "etc" not in out


# ── _map_names / pseudonymization ────────────────────────────────────────


def test_map_names_longest_first():
    aliases = {"Panelist 1": "GPT", "Panelist 12": "Grok"}
    out = _map_names("Panelist 12 agrees with Panelist 1", aliases)
    assert out == "Grok agrees with GPT"


def test_map_names_round_trip():
    aliases = {"GPT-5 Mini": "Panelist 2", "GPT-5": "Panelist 1"}
    real = {v: k for k, v in aliases.items()}
    text = "GPT-5 Mini rebuts GPT-5"
    assert _map_names(_map_names(text, aliases), real) == text


def test_leading_option():
    assert _leading_option([]) is None
    prior = [{"vote": "A"}, {"vote": "B"}, {"vote": "B"}]
    assert _leading_option(prior) == "B"


# ── RunMemory ────────────────────────────────────────────────────────────


def test_search_cache_normalizes_queries():
    m = RunMemory()
    m.put_search("  Saab   GlobalEye ", "result")
    assert m.get_search("saab globaleye") == "result"


def test_evidence_board_dedupes_and_aliases():
    m = RunMemory()
    m.record_tool("Alpha", 0, "search", query="saab deal")
    m.record_tool("Beta", 0, "search", query="SAAB   deal")  # same, normalized
    m.record_tool("Alpha", 0, "browse", url="https://x.se/a")
    board = m.evidence_board(aliases={"Alpha": "Panelist 1"})
    assert board.count("saab deal") == 1
    assert "Panelist 1" in board and "Alpha" not in board


def test_agent_history_own_activity_only():
    m = RunMemory()
    m.record_tool("Alpha", 0, "search", query="q1")
    prior = [
        {"name": "Alpha", "round": 0, "vote": "A", "reasoning": "Beta is wrong"},
        {"name": "Beta", "round": 0, "vote": "B", "reasoning": "obvious"},
    ]
    hist = m.agent_history("Alpha", prior, aliases={"Beta": "Panelist 2"})
    assert "you voted A" in hist
    assert "obvious" not in hist            # peers' ballots excluded
    assert "Panelist 2 is wrong" in hist    # names anonymized in quotes
