"""main.py vote algebra: normalization, tally, winner."""

from app.main import _current_tally, _normalize_vote, _prior_vote, _winner

OPTIONS = ["Norway", "Sweden", "Denmark", "Finland"]


def test_exact_and_case_insensitive():
    assert _normalize_vote("Denmark", OPTIONS) == "Denmark"
    assert _normalize_vote("denmark", OPTIONS) == "Denmark"


def test_letter_forms():
    assert _normalize_vote("C", OPTIONS) == "Denmark"
    assert _normalize_vote("Option C", OPTIONS) == "Denmark"
    assert _normalize_vote("C: Denmark", OPTIONS) == "Denmark"
    assert _normalize_vote("C. Denmark", OPTIONS) == "Denmark"
    assert _normalize_vote("Option C: Denmark", OPTIONS) == "Denmark"


def test_markdown_fluff_stripped():
    assert _normalize_vote("**Denmark**", OPTIONS) == "Denmark"
    assert _normalize_vote("'Denmark'", OPTIONS) == "Denmark"


def test_substring_and_truncation_fallbacks():
    assert _normalize_vote("the answer is Denmark", OPTIONS) == "Denmark"
    assert _normalize_vote("Denm", OPTIONS) == "Denmark"


def test_ambiguous_and_garbage_return_none():
    assert _normalize_vote("Denmark or Sweden", OPTIONS) is None
    assert _normalize_vote("whatever", OPTIONS) is None
    assert _normalize_vote("", OPTIONS) is None


def test_letter_prefix_with_mismatched_label_prefers_label():
    # "A: Denmark" — letter says Norway, label says Denmark; label wins.
    assert _normalize_vote("A: Denmark", OPTIONS) == "Denmark"


def test_option_labels_that_contain_each_other():
    options = ["Norway", "Norway and Sweden"]
    assert _normalize_vote("Norway", options) == "Norway"
    assert _normalize_vote("Norway and Sweden", options) == "Norway and Sweden"


def _state(rounds):
    return {"rounds": [{"index": i, "ballots": b} for i, b in enumerate(rounds)]}


def test_current_tally_uses_latest_vote_per_panelist():
    state = _state([
        [{"name": "Alpha", "vote": "Norway"}, {"name": "Beta", "vote": "Sweden"}],
        [{"name": "Alpha", "vote": "Sweden"}],  # Beta errored this round
    ])
    tally = _current_tally(state, OPTIONS)
    # Beta's round-0 vote still counts; Alpha's latest (flipped) vote counts.
    assert tally == {"Norway": 0, "Sweden": 2, "Denmark": 0, "Finland": 0}


def test_prior_vote_looks_at_previous_round_only():
    state = _state([
        [{"name": "Alpha", "vote": "Norway"}],
        [{"name": "Alpha", "vote": "Sweden"}],
    ])
    assert _prior_vote(state, "Alpha", 1) == "Norway"
    assert _prior_vote(state, "Alpha", 0) is None
    assert _prior_vote(state, "Beta", 1) is None


def test_winner_majority_and_tiebreak():
    ballots = [{"vote": "Sweden"}, {"vote": "Sweden"}, {"vote": "Norway"}]
    assert _winner(OPTIONS, ballots) == "Sweden"
    # Tie: original option order breaks it deterministically.
    tie = [{"vote": "Sweden"}, {"vote": "Norway"}]
    assert _winner(OPTIONS, tie) == "Norway"
    assert _winner(OPTIONS, []) is None
