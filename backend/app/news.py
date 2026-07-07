"""Morning/evening news editions: raw material for the council.

Six major Swedish outlets, each fetched exactly once per edition. Every
source carries its *declared* editorial stance (Swedish papers print
theirs on the ledarsida); the council later rates the *measured* lean of
each article, and the gap between the two is part of the report.

No LLM calls in this module — it only produces headline items per source.
"""

import asyncio
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx

FETCH_TIMEOUT = 15.0
MAX_ITEMS_PER_SOURCE = 15
USER_AGENT = "Mozilla/5.0 (compatible; Rabble/1.0)"


@dataclass(frozen=True)
class Source:
    id: str          # short key used in reports, e.g. "dn"
    name: str        # display name, e.g. "Dagens Nyheter"
    feed: str        # RSS URL
    stance: str      # declared editorial stance, as the outlet prints it
    paywalled: bool  # full articles behind a paywall?


SOURCES: list[Source] = [
    Source("dn", "Dagens Nyheter", "https://www.dn.se/rss/",
           "oberoende liberal", paywalled=True),
    Source("svd", "Svenska Dagbladet", "https://www.svd.se/feed/articles.rss",
           "obunden moderat", paywalled=True),
    Source("aftonbladet", "Aftonbladet",
           "https://rss.aftonbladet.se/rss2/small/pages/sections/senastenytt/",
           "oberoende socialdemokratisk", paywalled=False),
    Source("expressen", "Expressen", "https://feeds.expressen.se/nyheter/",
           "obundet liberal", paywalled=False),
    Source("svt", "SVT Nyheter", "https://www.svt.se/rss.xml",
           "public service (opartisk)", paywalled=False),
    Source("sr", "Sveriges Radio Ekot",
           "https://api.sr.se/api/rss/program/83?format=145",
           "public service (opartisk)", paywalled=False),
]

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return html.unescape(_TAG_RE.sub("", text)).strip()


_ATOM = "{http://www.w3.org/2005/Atom}"


def _parse_feed(xml_bytes: bytes, source: Source) -> list[dict]:
    """RSS 2.0 <item> or Atom <entry> → {source, title, summary, link, published}.

    Five of the six outlets serve RSS 2.0; Sveriges Radio serves Atom.
    """
    root = ET.fromstring(xml_bytes)
    items: list[dict] = []
    if root.tag == f"{_ATOM}feed":
        for entry in root.iter(f"{_ATOM}entry"):
            title = _clean(entry.findtext(f"{_ATOM}title"))
            if not title:
                continue
            link = entry.find(f"{_ATOM}link")
            items.append({
                "source": source.id,
                "title": title,
                "summary": _clean(entry.findtext(f"{_ATOM}summary"))[:400],
                "link": link.get("href", "") if link is not None else "",
                "published": (entry.findtext(f"{_ATOM}updated") or "").strip(),
            })
            if len(items) >= MAX_ITEMS_PER_SOURCE:
                break
        return items
    for item in root.iter("item"):
        title = _clean(item.findtext("title"))
        if not title:
            continue
        items.append({
            "source": source.id,
            "title": title,
            "summary": _clean(item.findtext("description"))[:400],
            "link": (item.findtext("link") or "").strip(),
            "published": (item.findtext("pubDate") or "").strip(),
        })
        if len(items) >= MAX_ITEMS_PER_SOURCE:
            break
    return items


async def _fetch_one(client: httpx.AsyncClient, source: Source) -> list[dict]:
    resp = await client.get(source.feed)
    resp.raise_for_status()
    return _parse_feed(resp.content, source)


async def fetch_headlines() -> dict:
    """Fetch all sources in parallel, exactly once each.

    Returns {"items": [...], "errors": {source_id: message}} — a dead feed
    degrades the edition instead of killing it, same spirit as a dead
    panelist in poll mode.
    """
    items: list[dict] = []
    errors: dict[str, str] = {}
    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        results = await asyncio.gather(
            *(_fetch_one(client, s) for s in SOURCES),
            return_exceptions=True,
        )
    for source, result in zip(SOURCES, results):
        if isinstance(result, Exception):
            errors[source.id] = str(result)
        else:
            items.extend(result)
    return {"items": items, "errors": errors}
