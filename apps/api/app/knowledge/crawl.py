"""Crawls a website connector's starting URL, same-host only, into a list of pages worth indexing.

Every fetch — the start URL, every link followed, and every redirect hop in between — goes through
`assert_safe_base_url` (see app/core/ssrf.py): a connector's URL is supplied by a workspace member,
and a crawler that will fetch whatever it's pointed at is exactly the request-forgery primitive
that check exists to close off. Redirects are followed manually (`follow_redirects=False`) rather
than left to httpx precisely so each hop gets that same check — httpx's own redirect handling would
otherwise happily follow a same-host page straight to an internal address.
"""

import asyncio
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from app.core.ssrf import UnsafeBaseUrlError, assert_safe_base_url

USER_AGENT = "ReCore-Indexer/1.0 (+https://github.com/Amboyandrey/ReCore)"
_TIMEOUT = 15.0
_MAX_BODY_BYTES = 2 * 1024 * 1024
_MAX_REDIRECTS = 5
# Bounds total requests (pages, off-topic links, redirect hops) independent of how many actually
# turn into indexed pages — a site heavy on non-HTML links or dead ends shouldn't be free to make
# the crawl run far longer than max_pages alone would suggest.
_MAX_VISITS_MULTIPLIER = 5

# Overridable only from tests, to exercise this module's request/response handling against a fake
# transport instead of the real network — the same seam every provider adapter's own `_client()`
# method, and app/tools/web_search.py, already give themselves.
_transport: httpx.AsyncBaseTransport | None = None


@dataclass(frozen=True)
class Page:
    url: str
    title: str
    text: str


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_TIMEOUT, transport=_transport, headers={"User-Agent": USER_AGENT})


def _normalize(url: str) -> str:
    """Drop the fragment — #section-two on an otherwise-identical URL is the same page."""
    return urldefrag(url)[0]


def _same_host(a: str, b: str) -> bool:
    pa, pb = urlparse(a), urlparse(b)
    return (pa.scheme, pa.hostname) == (pb.scheme, pb.hostname)


def _extract(html: str) -> tuple[str, str]:
    """Title and body text, with the boilerplate that would otherwise pollute every single page
    of a site (nav, footer, script/style payloads) dropped before the text is even read out."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    text = soup.get_text("\n")
    lines = [line.strip() for line in text.splitlines()]
    collapsed = "\n".join(line for line in lines if line)
    return title, collapsed


async def _fetch_robots(client: httpx.AsyncClient, start_url: str) -> RobotFileParser:
    """Best-effort: a robots.txt that's missing, unreachable, or malformed is treated as
    allow-everything rather than blocking the crawl — the same posture a normal browser takes."""
    parsed = urlparse(start_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    parser = RobotFileParser()
    try:
        response = await client.get(robots_url)
        if response.status_code < 400:
            parser.parse(response.text.splitlines())
        else:
            parser.parse([])
    except httpx.HTTPError:
        parser.parse([])
    return parser


async def _fetch_page(client: httpx.AsyncClient, url: str) -> tuple[str, str] | None:
    """Fetch one URL, following same-host redirects up to `_MAX_REDIRECTS` hops (each re-checked
    for safety), and return `(final_url, html)` — or `None` if it isn't worth indexing (not HTML,
    too large, or the fetch failed outright)."""
    hop_url = url
    for _ in range(_MAX_REDIRECTS):
        await asyncio.to_thread(assert_safe_base_url, hop_url)
        try:
            async with client.stream("GET", hop_url) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return None
                    next_url = _normalize(urljoin(hop_url, location))
                    if not _same_host(url, next_url):
                        return None
                    hop_url = next_url
                    continue
                if response.status_code >= 400:
                    return None
                content_type = response.headers.get("content-type", "")
                if "text/html" not in content_type:
                    return None
                body = bytearray()
                async for piece in response.aiter_bytes():
                    body.extend(piece)
                    if len(body) > _MAX_BODY_BYTES:
                        break
                return hop_url, bytes(body).decode(response.encoding or "utf-8", errors="replace")
        except httpx.HTTPError:
            return None
    return None  # too many redirect hops


async def crawl(start_url: str, *, max_pages: int = 30, max_depth: int = 2) -> list[Page]:
    """BFS from `start_url`, staying on the same scheme+host, honoring robots.txt, up to
    `max_pages` HTML pages and `max_depth` link hops. Never raises for an individual page's own
    failure — a broken link or a page that fails to fetch is just skipped, since a single bad page
    on an otherwise-fine site shouldn't fail the whole connector (see the worker's own top-level
    catch-all for what happens if the crawl fails entirely, e.g. the start URL itself is down).
    """
    await asyncio.to_thread(assert_safe_base_url, start_url)

    pages: list[Page] = []
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(_normalize(start_url), 0)]
    max_visits = max(max_pages * _MAX_VISITS_MULTIPLIER, max_pages)

    async with _client() as client:
        robots = await _fetch_robots(client, start_url)

        while queue and len(pages) < max_pages and len(visited) < max_visits:
            url, depth = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            if not robots.can_fetch(USER_AGENT, url):
                continue

            try:
                fetched = await _fetch_page(client, url)
            except UnsafeBaseUrlError:
                continue
            if fetched is None:
                continue
            final_url, html = fetched

            title, text = _extract(html)
            if text.strip():
                pages.append(Page(url=final_url, title=title, text=text))

            if depth >= max_depth:
                continue
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                link = _normalize(urljoin(final_url, a["href"]))
                if _same_host(start_url, link) and link not in visited:
                    queue.append((link, depth + 1))

    return pages
