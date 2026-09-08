"""A load test against concurrent streaming generations — the "load test on concurrent streams"
hardening-phase deliverable.

Exercises the real send -> background generation -> Redis stream -> SSE pipeline end to end,
through the actual HTTP layer (ASGI in-process, no real network hop), with FakeProvider standing
in for the LLM call — this measures the platform's own concurrency overhead (session handling,
Redis streams, SSE framing, cost computation) rather than a provider's latency, and needs no real
API key to run.

Forces the same `_test`-suffixed database and dedicated Redis db index tests/conftest.py uses, for
the same reason: this creates and tears down a lot of throwaway data, and must never be able to
touch a `docker compose` stack's live, interactively-used database.

A finding from actually running this, not a guess: a streaming SSE response holds its request's
DB connection open for the whole reply (FastAPI only tears down a `yield` dependency once the
response body is fully sent), not just for the brief query that starts it. Past roughly the
connection pool's size (db_pool_size + db_max_overflow in core/config.py, 40 by default),
requests start queuing for a free connection rather than failing outright — the default
concurrency below stays safely under that ceiling; pass a much higher --conversations to watch
it happen, which is the entire point of tuning the pool sizes rather than trusting the defaults.

Usage: uv run python -m app.scripts.load_test [--conversations 30] [--words 20]
"""

import argparse
import asyncio
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

API_ROOT = Path(__file__).resolve().parent.parent.parent


def _force_test_env() -> None:
    """Same override as tests/conftest.py — see its docstring for why this is non-negotiable."""
    base_db = os.environ.get("DATABASE_URL", "postgresql+asyncpg://recore:recore@localhost:5432/recore")
    scheme, netloc, path, query, fragment = urlsplit(base_db)
    db_name = path.lstrip("/")
    if not db_name.endswith("_test"):
        os.environ["DATABASE_URL"] = urlunsplit((scheme, netloc, f"/{db_name}_test", query, fragment))

    base_redis = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    scheme, netloc, path, query, fragment = urlsplit(base_redis)
    if path.lstrip("/") != "15":
        os.environ["REDIS_URL"] = urlunsplit((scheme, netloc, "/15", query, fragment))


_force_test_env()

# Imports below deliberately follow the env override above — app.core.config's Settings is
# lru_cache'd on first call, so anything importing it earlier would bake in the wrong URLs.
from collections.abc import AsyncIterator, Sequence  # noqa: E402

from httpx import ASGITransport, AsyncClient, Limits  # noqa: E402

import app.services.chat as chat_service  # noqa: E402
import app.services.credentials as credentials_service  # noqa: E402
from app.core.db import engine  # noqa: E402
from app.core.redis import _pool as redis_pool  # noqa: E402
from app.main import app  # noqa: E402
from app.providers.base import ChatMessage, Chunk, Done, TextDelta, Usage  # noqa: E402
from app.providers.fake import VALID_KEY, FakeProvider  # noqa: E402

OWNER = {"email": "load-test@example.com", "password": "correct horse battery staple"}


class PacedFakeProvider(FakeProvider):
    """FakeProvider, but streaming a configurable number of words at a realistic pace — a fixed
    one-word reply wouldn't exercise concurrent *streaming*, just concurrent request handling."""

    def __init__(self, api_key: str, base_url: str | None = None, *, words: int = 20) -> None:
        super().__init__(api_key, base_url)
        self._words = words

    async def stream(
        self, *, model: str, messages: Sequence[ChatMessage], max_tokens: int
    ) -> AsyncIterator[Chunk]:
        del model, max_tokens
        for i in range(self._words):
            await asyncio.sleep(0.02)
            yield TextDelta(text=f"word{i} ")
        yield Usage(input_tokens=5, output_tokens=self._words)
        yield Done(finish_reason="stop")


def _install_fake_provider(words: int) -> None:
    """Same technique as the test suite's monkeypatch fixtures — swap the module-level binding
    both services import their own copy of, since patching one alone leaves the other real."""

    def build(provider: object, *, api_key: str, base_url: str | None) -> PacedFakeProvider:
        del provider
        return PacedFakeProvider(api_key, base_url, words=words)

    chat_service.build_provider = build  # type: ignore[attr-defined]
    credentials_service.build_provider = build  # type: ignore[attr-defined]


async def _consume_sse(client: AsyncClient, url: str, **kwargs: object) -> None:
    async with client.stream("POST", url, **kwargs) as response:  # type: ignore[arg-type]
        response.raise_for_status()
        async for _ in response.aiter_lines():
            pass


async def _one_send(client: AsyncClient, workspace_id: str, conversation_id: str) -> float:
    """Send one message, consume its full reply, and return how long that took."""
    started = time.monotonic()
    await _consume_sse(
        client,
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
        json={"content": "Load test message"},
    )
    return time.monotonic() - started


async def run(conversations: int, words: int) -> None:
    """The whole run, in one event loop start to finish — the Redis pool's connections are tied
    to whichever loop created them, so disconnecting them from a second, later `asyncio.run()`
    (as this used to) fails with "Event loop is closed"; cleanup has to happen right here."""
    _install_fake_provider(words)
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=API_ROOT, check=True)

    try:
        await _run_against(conversations, words)
    finally:
        await engine.dispose()
        await redis_pool.disconnect()


async def _run_against(conversations: int, words: int) -> None:
    """The actual load run, split out so `run()` can guarantee engine cleanup around it."""
    transport = ASGITransport(app=app)
    # httpx's own client-side connection cap defaults to ~100 concurrent, well below what this
    # script asks for by default — a streaming SSE response holds its "connection" for the
    # whole reply, same shape as the DB pool sizing note in core/config.py, and just as easy to
    # mistake for a server-side bottleneck until you actually look at where the error comes from.
    limits = Limits(max_connections=max(conversations * 2, 100), max_keepalive_connections=0)
    async with AsyncClient(transport=transport, base_url="http://loadtest", limits=limits) as client:
        await client.post("/api/v1/auth/signup", json=OWNER)
        await client.post("/api/v1/auth/login", json=OWNER)
        workspace = (await client.post("/api/v1/workspaces", json={"name": "Load Test"})).json()
        credential = (
            await client.post(
                f"/api/v1/workspaces/{workspace['id']}/credentials",
                json={"provider": "anthropic", "label": "Load", "api_key": VALID_KEY},
            )
        ).json()
        model = (
            await client.post(
                f"/api/v1/workspaces/{workspace['id']}/models",
                json={
                    "credential_id": credential["id"],
                    "provider_model_id": "fake-small",
                    "display_name": "Fake Small",
                },
            )
        ).json()

        conversation_ids = []
        for _ in range(conversations):
            conv = (
                await client.post(
                    f"/api/v1/workspaces/{workspace['id']}/conversations",
                    json={"model_id": model["id"]},
                )
            ).json()
            conversation_ids.append(conv["id"])

        print(f"Sending {conversations} messages concurrently, {words} words each...")
        started = time.monotonic()
        results = await asyncio.gather(
            *(_one_send(client, workspace["id"], cid) for cid in conversation_ids),
            return_exceptions=True,
        )
        total_time = time.monotonic() - started

    latencies = [r for r in results if isinstance(r, float)]
    errors = [r for r in results if not isinstance(r, float)]

    print(f"\n{len(latencies)} succeeded, {len(errors)} failed, in {total_time:.2f}s wall clock")
    if latencies:
        latencies.sort()
        p50 = statistics.median(latencies)
        p95 = latencies[int(len(latencies) * 0.95) - 1]
        print(f"latency: p50={p50:.3f}s  p95={p95:.3f}s  max={max(latencies):.3f}s")
        print(f"throughput: {len(latencies) / total_time:.1f} completed generations/sec")
    for err in errors[:5]:
        print(f"error: {err!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conversations", type=int, default=30)
    parser.add_argument("--words", type=int, default=20)
    args = parser.parse_args()
    asyncio.run(run(args.conversations, args.words))


if __name__ == "__main__":
    main()
