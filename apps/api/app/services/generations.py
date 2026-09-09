"""Redis-backed generation streams — the mechanism behind resumable, stoppable chat streaming.

A generation's deltas are appended to a Redis Stream (`gen:{id}`) as they arrive from the
provider, independent of any one HTTP connection: the SSE response for the original send
request, and any later reconnect, both just tail this stream from wherever they left off — the
producer (the running generation) and the consumer (an SSE connection) are decoupled on purpose.
Stop is a pub/sub message the running generation checks for between provider chunks.
"""

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, cast

from redis.asyncio import Redis

GENERATION_TTL_SECONDS = 60 * 60  # a finished generation's stream lives an hour, then expires
IDEMPOTENCY_TTL_SECONDS = 300

# redis-py's stubs type XREAD's return generically enough to be unusable here; this is its actual
# shape with a decode_responses=True client: [(stream_name, [(entry_id, fields), ...])].
_XReadResponse = list[tuple[str, list[tuple[str, dict[str, str]]]]]


def _stream_key(generation_id: str) -> str:
    """Build the Redis key a generation's event stream is stored under."""
    return f"gen:{generation_id}"


def _stop_channel(generation_id: str) -> str:
    """Build the pub/sub channel a generation's stop signal is published on."""
    return f"gen:{generation_id}:stop"


def _active_key(conversation_id: uuid.UUID) -> str:
    """Build the Redis key tracking which generation (if any) is currently running for a
    conversation — so a freshly loaded page can discover a stream to resume after a reload,
    when no in-memory state (like a Last-Event-ID) survives to ask for it directly."""
    return f"conv:{conversation_id}:active_generation"


_EventType = Literal["delta", "tool_call", "tool_result", "done", "error"]
_TERMINAL_EVENT_TYPES = ("done", "error")


@dataclass(frozen=True)
class StreamEvent:
    """One entry read back from a generation's Redis stream."""

    id: str
    type: _EventType
    data: dict[str, object]


async def append_event(
    redis: Redis, generation_id: str, event_type: _EventType, data: dict[str, object]
) -> None:
    """Append one event to a generation's stream — the source of truth for what's happened so far."""
    await redis.xadd(_stream_key(generation_id), {"type": event_type, "data": json.dumps(data)})
    await redis.expire(_stream_key(generation_id), GENERATION_TTL_SECONDS)


async def read_events(
    redis: Redis, generation_id: str, *, after: str = "0", block_ms: int = 1000
) -> AsyncIterator[StreamEvent]:
    """Tail a generation's stream from just after `after`, following new entries as they arrive.

    Stops once a 'done' or 'error' event has been read — nothing more is coming after that.
    """
    last_id = after
    while True:
        raw = await redis.xread({_stream_key(generation_id): last_id}, block=block_ms, count=50)
        response = cast(_XReadResponse, raw)
        if not response:
            continue
        _, entries = response[0]
        for entry_id, fields in entries:
            last_id = entry_id
            raw_type = fields["type"]
            if raw_type not in ("delta", "tool_call", "tool_result", "done", "error"):
                continue  # ignore anything unexpected rather than crash a long-lived stream reader
            event_type = cast(_EventType, raw_type)
            event = StreamEvent(id=entry_id, type=event_type, data=json.loads(fields["data"]))
            yield event
            if event.type in _TERMINAL_EVENT_TYPES:
                return


async def request_stop(redis: Redis, generation_id: str) -> None:
    """Signal a running generation to stop — checked between provider chunks, not instantaneous."""
    await redis.publish(_stop_channel(generation_id), "stop")


async def get_or_create_generation_id(redis: Redis, idempotency_key: str | None) -> tuple[str, bool]:
    """Resolve an idempotency key to a generation id, returning (generation_id, was_created).

    A retried request carrying the same key attaches to the existing — possibly still-running —
    generation instead of starting, and billing for, a second one. `SET ... NX` makes the
    check-and-create atomic, so two concurrent retries can't each think they created it.
    """
    if idempotency_key is None:
        return str(uuid.uuid4()), True
    key = f"idem:{idempotency_key}"
    candidate_id = str(uuid.uuid4())
    created = await redis.set(key, candidate_id, nx=True, ex=IDEMPOTENCY_TTL_SECONDS)
    if created:
        return candidate_id, True
    existing_id = await redis.get(key)
    # decode_responses=True guarantees str here — redis-py's stubs just can't express that.
    return (str(existing_id) if existing_id else candidate_id), False


async def set_active_generation(redis: Redis, conversation_id: uuid.UUID, generation_id: str) -> None:
    """Record that a generation is now the one running for this conversation."""
    await redis.set(_active_key(conversation_id), generation_id, ex=GENERATION_TTL_SECONDS)


async def clear_active_generation(redis: Redis, conversation_id: uuid.UUID) -> None:
    """Clear a conversation's active generation once it finishes, by any means."""
    await redis.delete(_active_key(conversation_id))


async def get_active_generation(redis: Redis, conversation_id: uuid.UUID) -> str | None:
    """Return the generation currently running for a conversation, if any."""
    value = await redis.get(_active_key(conversation_id))
    return str(value) if value else None
