"""debate.py judge: sample normalization and majority counting."""

import pytest

import app.debate as debate
from app.debate import Verdict, judge_verdict

STATE = {"options": ["Norway", "Sweden"], "rounds": []}


def _stub_samples(monkeypatch, winners):
    it = iter(winners)

    async def fake_sample(model, state, criteria):
        w = next(it)
        if isinstance(w, Exception):
            raise w
        return Verdict(winner=w, rationale="because")

    monkeypatch.setattr(debate, "_judge_sample", fake_sample)


async def test_majority_wins(monkeypatch):
    _stub_samples(monkeypatch, ["Sweden", "Norway", "Sweden"])
    v = await judge_verdict(object(), STATE)
    assert v.winner == "Sweden"


async def test_judge_sample_normalizes_winner_and_restores_names(monkeypatch):
    # The model answers with markdown fluff and a pseudonym; the sample
    # must come back with the clean option label and the real name.
    class FakeAgent:
        def __init__(self, *a, **kw):
            pass

        async def run(self, text):
            class R:
                output = Verdict(winner="**Sweden**",
                                 rationale="Panelist 1 argued best")
            return R()

    monkeypatch.setattr(debate, "Agent", FakeAgent)
    monkeypatch.setattr(debate.random, "sample", lambda seq, k: list(seq)[:k])
    state = {"question": "Norway or Sweden?",
             "options": ["Norway", "Sweden"],
             "rounds": [{"index": 0, "ballots": [
                 {"name": "Alpha", "vote": "Sweden", "reasoning": "r"}]}]}
    v = await debate._judge_sample(object(), state, criteria=None)
    assert v.winner == "Sweden"                 # "**Sweden**" normalized
    assert v.rationale == "Alpha argued best"   # pseudonym mapped back


async def test_failed_samples_ignored_unless_all_fail(monkeypatch):
    _stub_samples(monkeypatch, [RuntimeError("boom"), "Norway", "Norway"])
    v = await judge_verdict(object(), STATE)
    assert v.winner == "Norway"

    _stub_samples(monkeypatch, [RuntimeError("a"), RuntimeError("b"),
                                RuntimeError("c")])
    with pytest.raises(RuntimeError):
        await judge_verdict(object(), STATE)
