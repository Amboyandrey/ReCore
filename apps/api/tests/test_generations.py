"""The Redis primitives behind streaming: the event stream, the stop signal, and idempotency."""

import asyncio
import uuid

from redis.asyncio import Redis

from app.services.generations import (
    append_event,
    get_or_create_generation_id,
    read_events,
    request_stop,
)


async def test_read_events_replays_from_the_start(redis_client: Redis) -> None:
    """Everything appended before reading starts comes back, in order, then stops at 'done'."""
    generation_id = str(uuid.uuid4())
    await append_event(redis_client, generation_id, "delta", {"text": "Hel"})
    await append_event(redis_client, generation_id, "delta", {"text": "lo"})
    await append_event(redis_client, generation_id, "done", {"finish_reason": "stop"})

    events = [e async for e in read_events(redis_client, generation_id, block_ms=50)]

    assert [e.type for e in events] == ["delta", "delta", "done"]
    assert events[0].data == {"text": "Hel"}
    assert events[2].data == {"finish_reason": "stop"}


async def test_read_events_follows_new_entries_as_they_arrive(redis_client: Redis) -> None:
    """A reader started before anything exists still sees events appended after it starts."""
    generation_id = str(uuid.uuid4())

    async def producer() -> None:
        await asyncio.sleep(0.1)
        await append_event(redis_client, generation_id, "delta", {"text": "late"})
        await append_event(redis_client, generation_id, "done", {"finish_reason": "stop"})

    asyncio.create_task(producer())
    events = [e async for e in read_events(redis_client, generation_id, block_ms=50)]

    assert [e.type for e in events] == ["delta", "done"]


async def test_resuming_from_an_offset_skips_already_seen_events(redis_client: Redis) -> None:
    """Resuming with `after` set to the last-seen id only returns what came after it."""
    generation_id = str(uuid.uuid4())
    await append_event(redis_client, generation_id, "delta", {"text": "one"})
    # Read the raw stream directly (not via read_events, which only returns once it's seen a
    # 'done' — there isn't one yet) to find the id a disconnecting client would have last seen.
    raw_entries = await redis_client.xrange(f"gen:{generation_id}")
    last_seen_id = raw_entries[-1][0]
    await append_event(redis_client, generation_id, "delta", {"text": "two"})
    await append_event(redis_client, generation_id, "done", {"finish_reason": "stop"})

    resumed = [
        e async for e in read_events(redis_client, generation_id, after=last_seen_id, block_ms=50)
    ]

    assert [e.data for e in resumed] == [{"text": "two"}, {"finish_reason": "stop"}]


async def test_stop_signal_is_delivered_to_a_subscriber(redis_client: Redis) -> None:
    """request_stop publishes on the channel a running generation would be subscribed to."""
    generation_id = str(uuid.uuid4())
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(f"gen:{generation_id}:stop")
    await pubsub.get_message(timeout=1)  # the subscribe confirmation itself

    await request_stop(redis_client, generation_id)
    message = await pubsub.get_message(timeout=1)

    assert message is not None
    assert message["data"] == "stop"
    await pubsub.aclose()


async def test_no_idempotency_key_always_makes_a_new_generation(redis_client: Redis) -> None:
    """Without a key, every call gets its own fresh generation id."""
    first_id, first_created = await get_or_create_generation_id(redis_client, None)
    second_id, second_created = await get_or_create_generation_id(redis_client, None)

    assert first_id != second_id
    assert first_created is True
    assert second_created is True


async def test_the_same_idempotency_key_reuses_the_generation(redis_client: Redis) -> None:
    """A retried request with the same key attaches to the generation the first request made."""
    key = "retry-key-abc"

    first_id, first_created = await get_or_create_generation_id(redis_client, key)
    second_id, second_created = await get_or_create_generation_id(redis_client, key)

    assert first_id == second_id
    assert first_created is True
    assert second_created is False
