"""Headless Playwright browser, shared across the app.

One Chromium instance is launched at FastAPI startup and reused for every
tool call. Each fetch gets its own context so cookies/history don't leak
between panelists. Returns extracted text (not HTML) — panelists only need
the readable content, and it keeps token cost bounded.
"""

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import quote_plus, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Browser, Playwright

_MAX_TEXT_CHARS = 4000
_FETCH_TIMEOUT_MS = 8000
_UA = "Mozilla/5.0 (Rabble/1.0; +https://rabble.example) Chrome/121"


@dataclass
class Page:
    title: str
    text: str
    final_url: str


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str


class BrowserPool:
    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._browser is not None:
                return
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )

    async def stop(self) -> None:
        async with self._lock:
            if self._browser:
                await self._browser.close()
                self._browser = None
            if self._pw:
                await self._pw.stop()
                self._pw = None

    async def _browser_or_start(self) -> Browser:
        if self._browser is None:
            await self.start()
        assert self._browser is not None
        return self._browser


pool = BrowserPool()


def _is_private_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return True
    if parsed.scheme not in ("http", "https"):
        return True
    host = parsed.hostname
    if not host:
        return True
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
        for _, _, _, _, sockaddr in infos:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return True
    except OSError:
        return True
    return False


def _extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_MAX_TEXT_CHARS]


async def fetch_page(url: str) -> Page:
    if _is_private_url(url):
        return Page(title="", text=f"Refused: {url} is not a public http(s) URL.", final_url=url)

    browser = await pool._browser_or_start()
    context = await browser.new_context(user_agent=_UA)
    try:
        page = await context.new_page()
        try:
            response = await page.goto(url, timeout=_FETCH_TIMEOUT_MS, wait_until="domcontentloaded")
        except Exception as exc:
            return Page(title="", text=f"Fetch failed: {type(exc).__name__}: {str(exc)[:200]}", final_url=url)
        title = await page.title()
        try:
            html = await page.content()
        except Exception:
            html = ""
        text = _extract_text(html) if html else ""
        final = page.url if response is not None else url
        return Page(title=title or "", text=text, final_url=final)
    finally:
        await context.close()


async def ddg_search(query: str, limit: int = 5) -> list[SearchHit]:
    q = quote_plus(query)
    url = f"https://duckduckgo.com/html/?q={q}"
    browser = await pool._browser_or_start()
    context = await browser.new_context(user_agent=_UA)
    try:
        page = await context.new_page()
        try:
            await page.goto(url, timeout=_FETCH_TIMEOUT_MS, wait_until="domcontentloaded")
        except Exception as exc:
            return []
        html = await page.content()
    finally:
        await context.close()

    soup = BeautifulSoup(html, "html.parser")
    hits: list[SearchHit] = []
    for result in soup.select("div.result")[:limit]:
        a = result.select_one("a.result__a")
        snippet = result.select_one(".result__snippet")
        if not a:
            continue
        hits.append(SearchHit(
            title=a.get_text(strip=True),
            url=a.get("href", ""),
            snippet=(snippet.get_text(" ", strip=True) if snippet else "")[:300],
        ))
    return hits
