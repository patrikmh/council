"""OpenRouter model catalog — used to validate slugs in ROUNDTABLE_PANEL.

Panelists whose slug is missing from OpenRouter's catalog would fail every
run with a 400. Checking the catalog up front lets the frontend grey them
out in the picker and skip them in ``_select_panel`` so runs aren't
littered with the same abstain-noise every time.

We fetch once per process, cache in-memory for an hour, and fail open:
if OpenRouter is unreachable we assume every slug is fine rather than
locking the user out of a working panel.
"""

import asyncio
import logging
import time
from typing import Iterable

from urllib.request import Request, urlopen
import json

log = logging.getLogger("rabble")

_CACHE_TTL_SEC = 3600
_cache: dict[str, object] = {"slugs": None, "expires_at": 0.0}
_lock = asyncio.Lock()


def _fetch_sync() -> set[str]:
    req = Request(
        "https://openrouter.ai/api/v1/models",
        headers={"User-Agent": "Rabble/1.0"},
    )
    with urlopen(req, timeout=8) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = payload.get("data") or []
    return {m["id"] for m in data if isinstance(m.get("id"), str)}


async def available_slugs() -> set[str] | None:
    """Return the set of valid OpenRouter slugs, or None if we couldn't
    fetch the catalog (fail open — treat all slugs as valid)."""
    now = time.time()
    cached = _cache["slugs"]
    if isinstance(cached, set) and now < _cache["expires_at"]:
        return cached  # type: ignore[return-value]

    async with _lock:
        # Re-check inside the lock
        cached = _cache["slugs"]
        if isinstance(cached, set) and now < _cache["expires_at"]:
            return cached  # type: ignore[return-value]
        try:
            slugs = await asyncio.to_thread(_fetch_sync)
        except Exception as exc:
            log.warning("openrouter_catalog_failed %s: %s",
                        type(exc).__name__, str(exc)[:200])
            _cache["slugs"] = None
            _cache["expires_at"] = now + 60  # short retry window
            return None
        log.info("openrouter_catalog_ok count=%d", len(slugs))
        _cache["slugs"] = slugs
        _cache["expires_at"] = now + _CACHE_TTL_SEC
        return slugs


async def filter_available(slugs: Iterable[str]) -> tuple[set[str], set[str]]:
    """Split *slugs* into (available, unavailable) using the catalog.

    If the catalog is unreachable, every slug is treated as available.
    """
    catalog = await available_slugs()
    if catalog is None:
        return set(slugs), set()
    slug_list = list(slugs)
    good = {s for s in slug_list if s in catalog}
    bad = set(slug_list) - good
    return good, bad
