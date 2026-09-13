"""The website crawler, against a fake transport — same MockTransport reasoning every other
adapter/tool test in this suite follows. `example.com` is used as the crawled host (as
test_tools.py's own SSRF tests already do): a real, publicly-resolvable domain, so
assert_safe_base_url's DNS check passes, while every actual HTTP response is faked."""

from collections.abc import Callable

import httpx
import pytest

from app.core.ssrf import UnsafeBaseUrlError
from app.knowledge import crawl as crawl_module
from app.knowledge.crawl import crawl

START = "https://example.com/"


def _handler(
    pages: dict[str, httpx.Response], robots_txt: str = ""
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text=robots_txt)
        if path in pages:
            return pages[path]
        return httpx.Response(404)

    return handler


def _html(body: str, title: str = "Page") -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/html"},
        text=f"<html><head><title>{title}</title></head><body>{body}</body></html>",
    )


@pytest.fixture(autouse=True)
def _reset_transport() -> None:
    """Every test sets its own transport; make sure none leaks into the next."""
    yield
    crawl_module._transport = None


async def test_crawl_rejects_an_unsafe_start_url() -> None:
    with pytest.raises(UnsafeBaseUrlError):
        await crawl("http://127.0.0.1/", max_pages=5)


async def test_crawl_follows_same_host_links_and_skips_off_host() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if request.url.path == "/":
            return _html(
                '<a href="/about">About</a><a href="https://other-host.example/">Other</a>',
                title="Home",
            )
        if request.url.path == "/about":
            return _html("About us.", title="About")
        return httpx.Response(404)

    crawl_module._transport = httpx.MockTransport(handler)
    pages = await crawl(START, max_pages=10)

    assert {p.url for p in pages} == {"https://example.com/", "https://example.com/about"}
    # The off-host link was never even requested — filtered out before enqueueing, not after.
    assert seen_paths.count("/") + seen_paths.count("/about") + seen_paths.count("/robots.txt") == len(
        seen_paths
    )


async def test_crawl_honors_robots_disallow() -> None:
    robots_txt = "User-agent: *\nDisallow: /secret\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots_txt)
        if request.url.path == "/":
            return _html('<a href="/secret">Secret</a>')
        if request.url.path == "/secret":
            return _html("Should never be fetched.")
        return httpx.Response(404)

    crawl_module._transport = httpx.MockTransport(handler)
    pages = await crawl(START, max_pages=10)

    assert {p.url for p in pages} == {"https://example.com/"}


async def test_crawl_stops_at_max_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        n = 0 if request.url.path == "/" else int(request.url.path.strip("/").removeprefix("p"))
        next_link = f'<a href="/p{n + 1}">next</a>'
        return _html(f"Page {n}. {next_link}")

    crawl_module._transport = httpx.MockTransport(handler)
    pages = await crawl(START, max_pages=3, max_depth=10)

    assert len(pages) == 3


async def test_crawl_respects_max_depth() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if request.url.path == "/":
            return _html('<a href="/depth1">next</a>')
        if request.url.path == "/depth1":
            return _html('<a href="/depth2">next</a>')
        if request.url.path == "/depth2":
            return _html("Should never be reached with max_depth=1.")
        return httpx.Response(404)

    crawl_module._transport = httpx.MockTransport(handler)
    pages = await crawl(START, max_pages=10, max_depth=1)

    assert {p.url for p in pages} == {"https://example.com/", "https://example.com/depth1"}


async def test_crawl_skips_non_html_responses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if request.url.path == "/":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"ok": True})
        return httpx.Response(404)

    crawl_module._transport = httpx.MockTransport(handler)
    pages = await crawl(START, max_pages=10)

    assert pages == []


async def test_crawl_does_not_follow_a_redirect_to_a_different_host() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": "https://other-host.example/"})
        return httpx.Response(404)

    crawl_module._transport = httpx.MockTransport(handler)
    pages = await crawl(START, max_pages=10)

    assert pages == []


async def test_crawl_skips_a_page_that_yields_no_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html><body></body></html>")

    crawl_module._transport = httpx.MockTransport(handler)
    pages = await crawl(START, max_pages=10)

    assert pages == []
