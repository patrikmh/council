"""The newsroom: cluster the day's headlines, debate each story, judge.

A news edition runs in three stages:

  1. Desk editor — one cheap framer call turns ~90 raw headline items
     (from app.news.fetch_headlines) into top stories (each reported by
     ≥2 outlets) plus blindspots (notable single-source items, listed
     without debate). The model proposes clusters by item number;
     `_resolve` disposes — the rules are enforced in code.
  2. Council — per story, every panelist reads all outlets' coverage,
     browses the free full articles, fact-checks the load-bearing claims
     and rates each outlet's framing lean (round 0); then sees the other
     assessments under pseudonyms and gets one rebuttal round. One
     rebuttal is the sweet spot — accuracy gains flatten after it while
     cost keeps scaling (Du et al. multi-agent debate line of work).
  3. Judge — writes the published story report in Swedish: neutral
     consensus account, per-outlet framing notes, disagreement callouts
     and the consolidated fact-check list. The *measured* lean per outlet
     is the median of panelist ratings, computed in code, not by the
     judge.
"""

import asyncio
import datetime as _dt
import logging
import os
import statistics
import time
from typing import AsyncIterator, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings

from .config import Panelist
from .debate import JUDGE_SETTINGS, _map_names, _panelist_settings
from .memory import RunMemory
from .news import SOURCES
from .panel import _preamble
from .tools import make_tools, OnToolCall

log = logging.getLogger("rabble")

MAX_STORIES = int(os.getenv("NEWS_MAX_STORIES", "5"))
MAX_BLINDSPOTS = int(os.getenv("NEWS_MAX_BLINDSPOTS", "5"))

# Two editions a day, morning and evening, on Stockholm time. There is no
# scheduler: the first News-tab visitor after a boundary generates the
# edition and everyone else reads the cached result.
STOCKHOLM = ZoneInfo("Europe/Stockholm")
MORNING_HOUR = 9
EVENING_HOUR = 18


def current_slot(now: _dt.datetime | None = None) -> str:
    """The edition slot the clock is currently in, e.g. '2026-07-07-morning'.
    Before 09:00 the current edition is still yesterday's evening one."""
    now = now or _dt.datetime.now(STOCKHOLM)
    if now.hour >= EVENING_HOUR:
        return f"{now.date().isoformat()}-evening"
    if now.hour >= MORNING_HOUR:
        return f"{now.date().isoformat()}-morning"
    return f"{(now.date() - _dt.timedelta(days=1)).isoformat()}-evening"

DESK_SETTINGS = ModelSettings(thinking="low")

_SOURCE_NAMES = {s.id: s.name for s in SOURCES}


class Story(BaseModel):
    title: str = Field(
        description=(
            "Neutral working title for the story, in Swedish. Describe the "
            "event itself — no outlet's spin, no clickbait, no question marks."
        )
    )
    item_numbers: list[int] = Field(
        description=(
            "Numbers of ALL headline items that report this same underlying "
            "event. Must include items from at least two different outlets."
        ),
        min_length=2,
    )


class Desk(BaseModel):
    stories: list[Story] = Field(
        description=(
            f"The top news stories right now, ranked by news value, "
            f"best first. At most {MAX_STORIES}. Only stories that at least "
            "two outlets are reporting."
        ),
        max_length=MAX_STORIES + 3,  # model may overshoot; re-capped in code
    )
    blindspots: list[int] = Field(
        description=(
            "Numbers of genuinely significant items that only ONE outlet is "
            "reporting. Skip routine sports results, celebrity items and "
            f"service journalism. At most {MAX_BLINDSPOTS}."
        ),
        max_length=MAX_BLINDSPOTS + 3,
    )


_OUTLET_ROSTER = ", ".join(s.name for s in SOURCES)

DESK_PROMPT = (
    "You are the desk editor for a council of AI models that debates how "
    "Swedish outlets report the news. You receive a numbered list of "
    "current headline items from a mix of mainstream and declared-partisan "
    f"Swedish outlets: {_OUTLET_ROSTER}.\n\n"
    "Group items that report the SAME underlying news event into stories. "
    "Two outlets covering the same event with different angles is exactly "
    "what the council wants; two merely similar events is not a group. "
    "Rank stories by news value: hard news with public significance beats "
    "sports results and celebrity coverage. When candidates tie, prefer a "
    "story covered from both mainstream and partisan angles — the framing "
    "comparison is richest there.\n\n"
    "Items that are significant but covered by only one outlet go in "
    "blindspots — the council cannot debate them, but readers should see "
    "what the other outlets skipped. Do not pad either list: fewer good "
    "stories beats forced groupings."
)


def _numbered(items: list[dict]) -> str:
    lines = []
    for i, it in enumerate(items, start=1):
        src = _SOURCE_NAMES.get(it["source"], it["source"])
        line = f"{i}. [{src}] {it['title']}"
        if it["summary"]:
            line += f" — {it['summary'][:200]}"
        lines.append(line)
    return "\n".join(lines)


def _resolve(desk: Desk, items: list[dict]) -> dict:
    """Map the desk's item numbers back to items and enforce the rules:
    a story needs ≥2 distinct outlets, no item is used twice, caps apply."""
    used: set[int] = set()
    stories: list[dict] = []
    for story in desk.stories:
        picked = [
            (n, items[n - 1])
            for n in story.item_numbers
            if 1 <= n <= len(items) and n not in used
        ]
        if len({it["source"] for _, it in picked}) < 2:
            continue
        used.update(n for n, _ in picked)
        stories.append({"title": story.title, "items": [it for _, it in picked]})
        if len(stories) >= MAX_STORIES:
            break

    blindspots: list[dict] = []
    for n in desk.blindspots:
        if not (1 <= n <= len(items)) or n in used:
            continue
        used.add(n)
        blindspots.append(items[n - 1])
        if len(blindspots) >= MAX_BLINDSPOTS:
            break

    return {"stories": stories, "blindspots": blindspots}


async def cluster_stories(model, items: list[dict]) -> dict:
    """One desk-editor call: raw headline items → {stories, blindspots}."""
    agent = Agent(model, output_type=Desk, system_prompt=DESK_PROMPT,
                  model_settings=DESK_SETTINGS, retries=2)
    result = await agent.run(_numbered(items))
    return _resolve(result.output, items)


# ── Stage 2: the council debates one story ──────────────────────────────────

# Browsing-heavy work: reading 2-4 full articles plus fact-checking needs
# more headroom than a poll ballot (budget 2) but the debate cap (5) is
# about right; the timeout is longer because browse calls serialize.
NEWS_TOOL_BUDGET = int(os.getenv("NEWS_TOOL_BUDGET", "6"))
NEWS_PANELIST_TIMEOUT = float(os.getenv("NEWS_PANELIST_TIMEOUT_SEC", "240"))

# News council model settings: thinking pinned low and output capped.
# The cap matters on OpenRouter: without max_tokens it pre-authorizes the
# model's NATIVE output limit (65k on GPT 5.5) against the key's remaining
# monthly credit and returns 402 when that can't be covered, even though
# the actual response is a few thousand tokens.
NEWS_THINKING = os.getenv("NEWS_THINKING", "low")
NEWS_MAX_TOKENS = int(os.getenv("NEWS_MAX_TOKENS", "16384"))


def _news_settings(slug: str) -> ModelSettings:
    ms = dict(_panelist_settings(slug))
    ms["thinking"] = NEWS_THINKING
    ms.setdefault("max_tokens", NEWS_MAX_TOKENS)  # MODEL_MAX_TOKENS still wins
    return ModelSettings(**ms)

_SOURCE_STANCES = {s.id: s.stance for s in SOURCES}
_SOURCE_PAYWALLED = {s.id: s.paywalled for s in SOURCES}


class OutletRead(BaseModel):
    source: str = Field(
        description=(
            "Outlet key exactly as written in brackets in the coverage "
            "list, e.g. 'dn' or 'svt'."
        )
    )
    lean: int = Field(
        ge=-2, le=2,
        description=(
            "Measured economic left-right lean of THIS article's framing: "
            "-2 clearly left, -1 leans left, 0 neutral, +1 leans right, "
            "+2 clearly right. Rate the text in front of you — word choice, "
            "emphasis, what it omits — never the outlet's reputation or "
            "declared stance."
        ),
    )
    social: int = Field(
        ge=-2, le=2,
        description=(
            "Measured social-axis position of THIS article's framing "
            "(GAL-TAN): -2 clearly liberal/progressive, 0 neutral, "
            "+2 clearly conservative/traditionalist. Same rule: rate the "
            "text, not the brand."
        ),
    )
    note: str = Field(
        description=(
            "One sentence on this outlet's framing: emphasis, loaded words, "
            "or what it leaves out that other outlets include."
        )
    )


class FactCheck(BaseModel):
    claim: str = Field(description="The specific factual claim, quoted or tightly paraphrased.")
    source: str = Field(
        default="",
        description=(
            "Outlet key ('dn', 'svt', …) whose article carries this claim. "
            "Empty string only if the claim appears across several outlets."
        ),
    )
    verdict: Literal["verified", "unverified", "contradicted"]
    evidence: str = Field(
        description=(
            "One sentence of evidence with the raw URL inline. 'unverified' "
            "means you looked and could not confirm — say where you looked."
        )
    )


class Assessment(BaseModel):
    account: str = Field(
        description=(
            "Your account of what actually happened, three to six sentences, "
            "neutral wire-service tone, citing raw URLs inline."
        )
    )
    outlet_reads: list[OutletRead] = Field(
        description="One entry per outlet in the coverage list."
    )
    fact_checks: list[FactCheck] = Field(
        max_length=3,
        description=(
            "The one to three most load-bearing factual claims in this "
            "story, checked against sources outside these outlets when "
            "possible."
        ),
    )
    confidence: int = Field(
        ge=0, le=100,
        description="Confidence that your account is accurate, 0-100.",
    )


class Rebuttal(Assessment):
    rebuttal: str = Field(
        description=(
            "Two to four sentences addressing specific panelists by their "
            "pseudonym: what they got wrong or missed, or which of their "
            "points changed your account."
        )
    )


ASSESS_PROMPT = (
    "You sit on the Rabble's news council. You get ONE news story as "
    "reported by several Swedish outlets — for each outlet: its declared "
    "editorial stance, whether it is paywalled, its headline and the RSS "
    "summary, and the article URL.\n\n"
    "Your job:\n"
    "1. Establish what actually happened. Use browse(url) to read the "
    "full articles from outlets NOT marked paywalled (paywalled ones give "
    "you only the snippet — treat their framing accordingly). Use "
    "web_search to fact-check the one to three most load-bearing claims, "
    "preferably against sources outside these outlets (TT, myndigheter, "
    "international wires).\n"
    "2. Rate each outlet's framing of THIS article on two -2..+2 axes: "
    "economic left-right, and social liberal-conservative (GAL-TAN). "
    "Rate the text, not the brand: a public-service piece can lean, a "
    "tabloid piece can be straight. Note emphasis and omissions.\n"
    "3. Write your account in neutral wire-service tone with URLs inline.\n\n"
    "Work in English; the judge publishes the final report in Swedish. "
    "Do not invent URLs — cite only pages you browsed or search results "
    "you received."
)

REBUT_PROMPT = (
    "You sit on the Rabble's news council, rebuttal round. You already "
    "assessed this story; now you see the other panelists' assessments "
    "under neutral pseudonyms (to you, they are 'Panelist N' — you are "
    "{alias}).\n\n"
    "Challenge what deserves challenging: a fact-check verdict you "
    "disbelieve, a lean rating that reads the framing wrong, an omission "
    "everyone repeated. Verify disputed claims with web_search/browse "
    "before ruling on them. Then submit your REVISED assessment — update "
    "your account, lean ratings and fact-checks where a peer's evidence "
    "was genuinely stronger, hold where it was not. Popularity is not "
    "evidence.\n\n"
    "Work in English. Address peers by pseudonym in your rebuttal."
)


def _coverage_text(story: dict) -> str:
    lines = [f"Story: {story['title']}", "", "Coverage (outlet key in brackets):"]
    for it in story["items"]:
        sid = it["source"]
        stance = _SOURCE_STANCES.get(sid, "unknown")
        pay = "PAYWALLED, snippet only" if _SOURCE_PAYWALLED.get(sid) else "free to browse"
        name = _SOURCE_NAMES.get(sid, sid)
        lines.append(f"- [{sid}] {name} (declared stance: {stance}; {pay})")
        lines.append(f"  Headline: {it['title']}")
        if it["summary"]:
            lines.append(f"  Summary: {it['summary']}")
        if it["link"]:
            lines.append(f"  URL: {it['link']}")
    return "\n".join(lines)


def _fan_out(panel: list[Panelist], one) -> AsyncIterator:
    async def gen():
        tasks = [asyncio.create_task(one(p)) for p in panel]
        for fut in asyncio.as_completed(tasks):
            yield await fut
    return gen()


def assess_story(
    panel: list[Panelist],
    story: dict,
    on_tool_call: OnToolCall | None = None,
    memory: RunMemory | None = None,
) -> AsyncIterator[tuple[Panelist, Assessment | Exception]]:
    """Round 0: every panelist independently assesses the story, in parallel.
    Yields (panelist, assessment_or_exception) as each one lands."""
    prompt = _coverage_text(story) + "\n\nAssess this story."

    async def one(p: Panelist) -> tuple[Panelist, Assessment | Exception]:
        t0 = time.monotonic()
        log.info("news_assess_start name=%r story=%r", p.name, story["title"][:60])
        try:
            agent = Agent(
                p.model,
                output_type=Assessment,
                system_prompt=_preamble() + ASSESS_PROMPT,
                tools=make_tools(p.name, on_tool_call, budget=NEWS_TOOL_BUDGET,
                                 memory=memory, round_index=0),
                model_settings=_news_settings(p.slug),
                retries=2,
            )
            result = await asyncio.wait_for(agent.run(prompt),
                                            timeout=NEWS_PANELIST_TIMEOUT)
            log.info("news_assess_done name=%r elapsed=%.1fs",
                     p.name, time.monotonic() - t0)
            return p, result.output
        except asyncio.TimeoutError:
            log.warning("news_assess_timeout name=%r", p.name)
            return p, TimeoutError(f"timed out after {NEWS_PANELIST_TIMEOUT:.0f}s")
        except Exception as exc:  # a dead panelist shouldn't kill the story
            log.exception("news_assess_error name=%r", p.name)
            return p, exc

    return _fan_out(panel, one)


def _render_assessment(a: dict, alias: str) -> str:
    leans = " ".join(
        f"{r['source']}:LR{r['lean']:+d}/LC{r.get('social', 0):+d}"
        for r in a["outlet_reads"])
    lines = [f"{alias} (confidence {a['confidence']}):",
             f"  Account: {a['account'][:700]}",
             f"  Lean ratings: {leans or '(none)'}"]
    for fc in a["fact_checks"]:
        lines.append(f"  Fact-check: \"{fc['claim'][:150]}\" → {fc['verdict']} "
                     f"({fc['evidence'][:200]})")
    return "\n".join(lines)


def _render_peers(prior: list[dict], self_name: str, aliases: dict[str, str]) -> str:
    lines = ["The other panelists' assessments:"]
    for a in prior:
        if a["name"] == self_name:
            continue
        alias = aliases.get(a["name"], a["name"])
        lines.append(_map_names(_render_assessment(a, alias), aliases))
    return "\n\n".join(lines)


def rebuttal_round(
    panel: list[Panelist],
    story: dict,
    prior: list[dict],
    on_tool_call: OnToolCall | None = None,
    memory: RunMemory | None = None,
    aliases: dict[str, str] | None = None,
) -> AsyncIterator[tuple[Panelist, Rebuttal | Exception]]:
    """The single rebuttal round: everyone sees everyone else's assessment
    under pseudonyms and submits a revised one plus a rebuttal."""
    aliases = aliases or {}
    real_names = {alias: name for name, alias in aliases.items()}
    coverage = _coverage_text(story)

    async def one(p: Panelist) -> tuple[Panelist, Rebuttal | Exception]:
        t0 = time.monotonic()
        log.info("news_rebut_start name=%r story=%r", p.name, story["title"][:60])
        try:
            mine = next((a for a in prior if a["name"] == p.name), None)
            parts = [coverage]
            if mine:
                parts.append("Your previous assessment:\n"
                             + _render_assessment(mine, "You"))
            parts.append(_render_peers(prior, p.name, aliases))
            parts.append("Now submit your revised assessment and rebuttal.")
            opener = REBUT_PROMPT.format(alias=aliases.get(p.name, "a panelist"))
            agent = Agent(
                p.model,
                output_type=Rebuttal,
                system_prompt=_preamble() + opener,
                tools=make_tools(p.name, on_tool_call, budget=NEWS_TOOL_BUDGET,
                                 memory=memory, round_index=1),
                model_settings=_news_settings(p.slug),
                retries=2,
            )
            result = await asyncio.wait_for(agent.run("\n\n".join(parts)),
                                            timeout=NEWS_PANELIST_TIMEOUT)
            out = result.output.model_copy(update={
                "account": _map_names(result.output.account, real_names),
                "rebuttal": _map_names(result.output.rebuttal, real_names),
            })
            log.info("news_rebut_done name=%r elapsed=%.1fs",
                     p.name, time.monotonic() - t0)
            return p, out
        except asyncio.TimeoutError:
            log.warning("news_rebut_timeout name=%r", p.name)
            return p, TimeoutError(f"timed out after {NEWS_PANELIST_TIMEOUT:.0f}s")
        except Exception as exc:
            log.exception("news_rebut_error name=%r", p.name)
            return p, exc

    return _fan_out(panel, one)


# ── Stage 3: the judge publishes the story report ────────────────────────────

class OutletFraming(BaseModel):
    source: str = Field(description="Outlet key, e.g. 'dn'.")
    framing: str = Field(
        description=(
            "One or two sentences in Swedish on how this outlet framed the "
            "story: vinkel, ordval, what it emphasized or omitted."
        )
    )


class StoryReport(BaseModel):
    consensus: str = Field(
        description=(
            "The published consensus account in Swedish: what can actually "
            "be said to have happened, four to eight sentences, neutral "
            "TT-telegram tone, raw source URLs inline. Only include claims "
            "the council's evidence supports."
        )
    )
    outlet_framings: list[OutletFraming] = Field(
        description="One entry per outlet that covered the story."
    )
    disagreements: list[str] = Field(
        description=(
            "In Swedish: each point where the outlets contradict each other "
            "or where the council could not reach consensus, one short "
            "sentence per point. Empty list if none."
        )
    )
    fact_checks: list[FactCheck] = Field(
        description=(
            "The consolidated fact-check list: merge the panelists' checks, "
            "drop duplicates, keep each verdict only if the cited evidence "
            "supports it. Preserve each claim's source outlet key. Claims "
            "and evidence in Swedish, URLs unchanged."
        )
    )


JUDGE_PROMPT_NEWS = (
    "You are the judge of the Rabble's news council. You receive one story "
    "as covered by several Swedish outlets, plus the council's final "
    "assessments (panelists appear under pseudonyms). Weigh evidence "
    "quality, not agreement counts: an account backed by a browsed source "
    "beats three unsourced ones.\n\n"
    "Publish the story report IN SWEDISH (URLs and outlet keys unchanged). "
    "Be concrete in framing notes — quote a loaded word rather than "
    "calling it loaded."
)


async def judge_story(model, story: dict, final: list[dict]) -> StoryReport:
    """One judge pass over the final assessments, pseudonymized so brand
    names can't tilt the ruling."""
    aliases = {a["name"]: f"Panelist {i + 1}" for i, a in enumerate(final)}
    parts = [_coverage_text(story), "The council's final assessments:"]
    for a in final:
        parts.append(_map_names(_render_assessment(a, aliases[a["name"]]), aliases))
        if a.get("rebuttal"):
            parts.append(_map_names(f"  Rebuttal: {a['rebuttal'][:500]}", aliases))
    agent = Agent(model, output_type=StoryReport,
                  system_prompt=JUDGE_PROMPT_NEWS,
                  model_settings=JUDGE_SETTINGS, retries=2)
    result = await agent.run("\n\n".join(parts))
    return result.output


# ── Outlet stats: how the papers score across editions ──────────────────────

async def outlet_stats(days: int | None = 30) -> list[dict]:
    """Aggregate the stored editions into a per-outlet scoreboard: average
    measured left-right and liberal-conservative lean (mean of per-story
    medians), and fact-check accuracy over claims attributed to the outlet.
    Accuracy = verified / (verified + contradicted); unverified claims are
    counted but don't move the score either way."""
    from . import store  # local import: store has no business importing us

    editions = await store.news_done_editions(days)
    acc: dict[str, dict] = {
        s.id: {"lr": [], "lc": [], "stories": 0,
               "verified": 0, "contradicted": 0, "unverified": 0}
        for s in SOURCES
    }
    for edition in editions:
        for story in edition.get("stories", []):
            if story.get("status") != "done":
                continue
            for sid, lean in (story.get("leans") or {}).items():
                if sid not in acc:
                    continue
                acc[sid]["stories"] += 1
                if isinstance(lean, dict):
                    acc[sid]["lr"].append(lean.get("lr", 0))
                    acc[sid]["lc"].append(lean.get("lc", 0))
                else:  # editions stored before the second axis existed
                    acc[sid]["lr"].append(lean)
            report = story.get("report") or {}
            for fc in report.get("fact_checks", []):
                sid = fc.get("source", "")
                if sid in acc and fc.get("verdict") in (
                        "verified", "contradicted", "unverified"):
                    acc[sid][fc["verdict"]] += 1

    def mean(values: list) -> float | None:
        return round(sum(values) / len(values), 2) if values else None

    rows = []
    for s in SOURCES:
        a = acc[s.id]
        decided = a["verified"] + a["contradicted"]
        rows.append({
            "id": s.id,
            "name": s.name,
            "stance": s.stance,
            "stories": a["stories"],
            "lean_lr": mean(a["lr"]),
            "lean_lc": mean(a["lc"]),
            "verified": a["verified"],
            "contradicted": a["contradicted"],
            "unverified": a["unverified"],
            "accuracy_pct": round(100 * a["verified"] / decided, 1) if decided else None,
        })
    rows.sort(key=lambda r: (-(r["stories"]), r["name"]))
    return rows


def merge_final(assessments: list[dict], rebuttals: list[dict]) -> list[dict]:
    """Each panelist's freshest assessment: the rebuttal-round revision
    when there is one, else round 0. Merged per field — a revision that
    comes back with empty outlet_reads or fact_checks keeps the round-0
    lists instead of erasing that panelist's ratings and checks from the
    judged set (the schema allows empty lists, and lazy rebuttals happen)."""
    latest = {a["name"]: a for a in assessments}
    for r in rebuttals:
        prior = latest.get(r["name"])
        merged = dict(r)
        if prior:
            for field in ("outlet_reads", "fact_checks"):
                if not merged.get(field):
                    merged[field] = prior[field]
        latest[r["name"]] = merged
    return list(latest.values())


# A "median" of one rating is just that panelist's opinion; below the
# quorum we publish no measured lean rather than a misleading one.
LEAN_QUORUM = int(os.getenv("NEWS_LEAN_QUORUM", "2"))


def measured_leans(final: list[dict]) -> dict[str, dict[str, float]]:
    """Median panelist rating per outlet on both axes — computed here,
    never by the judge, so one eloquent panelist can't drag the number.
    lr = economic left-right, lc = social liberal-conservative."""
    by_source: dict[str, dict[str, list[int]]] = {}
    for a in final:
        for r in a["outlet_reads"]:
            axes = by_source.setdefault(r["source"], {"lr": [], "lc": []})
            axes["lr"].append(r["lean"])
            axes["lc"].append(r.get("social", 0))
    return {
        sid: {"lr": round(statistics.median(axes["lr"]), 1),
              "lc": round(statistics.median(axes["lc"]), 1),
              "n": len(axes["lr"])}
        for sid, axes in by_source.items()
        if len(axes["lr"]) >= LEAN_QUORUM
    }
