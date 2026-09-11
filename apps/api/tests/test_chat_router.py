"""The chat HTTP surface: conversations, sending a message, and consuming its SSE reply.

Routes provider calls through FakeProvider via monkeypatch — no real network call happens.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FeatureFlag, FlagScope, Tool, ToolKind, User
from app.providers.base import ChatMessage, Chunk, Done, TextDelta, ToolCall, ToolDefinition, Usage
from app.providers.fake import VALID_KEY, FakeProvider
from app.services.flags import set_override

OWNER = {"email": "owner@example.com", "password": "correct horse battery staple"}
MEMBER = {"email": "member@example.com", "password": "correct horse battery staple"}


class SlowFakeProvider(FakeProvider):
    """Paced like test_chat_service.py's version — gives a test a reliable window mid-stream."""

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        max_tokens: int,
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[Chunk]:
        del tools
        for word in ["one ", "two ", "three ", "four "]:
            await asyncio.sleep(0.05)
            yield TextDelta(text=word)
        yield Usage(input_tokens=1, output_tokens=4)
        yield Done(finish_reason="stop")


class ToolCallingFakeProvider(FakeProvider):
    """Asks for one tool on its first stream() call, then replies normally once the loop feeds
    the result back — see FakeProvider.SCRIPTED_TOOL_CALL."""

    SCRIPTED_TOOL_CALL = ToolCall(id="call_1", name="get_weather", arguments={"city": "Paris"})


def _fake_build_provider(provider, *, api_key, base_url) -> FakeProvider:
    return FakeProvider(api_key=api_key, base_url=base_url)


def _slow_build_provider(provider, *, api_key, base_url) -> SlowFakeProvider:
    return SlowFakeProvider(api_key=api_key, base_url=base_url)


def _tool_calling_build_provider(provider, *, api_key, base_url) -> ToolCallingFakeProvider:
    return ToolCallingFakeProvider(api_key=api_key, base_url=base_url)


@pytest.fixture(autouse=True)
def _fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route every provider build through FakeProvider, in both services that call it."""
    monkeypatch.setattr("app.services.credentials.build_provider", _fake_build_provider)
    monkeypatch.setattr("app.services.chat.build_provider", _fake_build_provider)


async def _workspace_with_model(client: AsyncClient) -> tuple[str, str]:
    """Sign up as owner, create a workspace, register a credential, enable a model."""
    await client.post("/api/v1/auth/signup", json=OWNER)
    await client.post("/api/v1/auth/login", json=OWNER)
    workspace_id = str((await client.post("/api/v1/workspaces", json={"name": "Acme"})).json()["id"])
    credential = await client.post(
        f"/api/v1/workspaces/{workspace_id}/credentials",
        json={"provider": "anthropic", "label": "Prod", "api_key": VALID_KEY},
    )
    credential_id = credential.json()["id"]
    model = await client.post(
        f"/api/v1/workspaces/{workspace_id}/models",
        json={
            "credential_id": credential_id,
            "provider_model_id": "fake-small",
            "display_name": "Fake Small",
            "cost_per_mtok_in": 1.0,
            "cost_per_mtok_out": 2.0,
        },
    )
    return workspace_id, model.json()["id"]


async def _consume_sse(client: AsyncClient, method: str, url: str, **kwargs) -> list[tuple[str, str]]:
    """Collect every (event, data) pair from an SSE response."""
    events = []
    async with client.stream(method, url, **kwargs) as response:
        event_type = None
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event_type = line.removeprefix("event:").strip()
            elif line.startswith("data:") and event_type is not None:
                events.append((event_type, line.removeprefix("data:").strip()))
                event_type = None
    return events


async def test_send_message_streams_deltas_and_persists_both_turns(client: AsyncClient) -> None:
    """Sending a message streams the reply as SSE, and both turns land in the message history."""
    workspace_id, model_id = await _workspace_with_model(client)
    conversation = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id}
    )
    conversation_id = conversation.json()["id"]

    events = await _consume_sse(
        client,
        "POST",
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
        json={"content": "Hi there"},
    )

    assert events[-1][0] == "done"
    assert any(e == "delta" for e, _ in events)

    messages = await client.get(f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages")
    roles = [m["role"] for m in messages.json()]
    assert roles == ["user", "assistant"]


async def test_conversation_title_is_set_from_the_first_message(client: AsyncClient) -> None:
    """The conversation's title changes from the placeholder once the first message is sent."""
    workspace_id, model_id = await _workspace_with_model(client)
    conversation = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id}
    )
    conversation_id = conversation.json()["id"]
    assert conversation.json()["title"] == "New conversation"

    await _consume_sse(
        client,
        "POST",
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
        json={"content": "What is the capital of France?"},
    )

    updated = await client.get(f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}")
    assert updated.json()["title"] == "What is the capital of France?"


async def test_updating_a_conversation_s_model_switches_which_model_it_uses(
    client: AsyncClient,
) -> None:
    """PATCHing a conversation's model_id changes it, and only to a model the workspace itself
    has enabled — mirrors the chat page's "change model mid-session" picker."""
    workspace_id, model_id = await _workspace_with_model(client)
    credential_id = (
        await client.get(f"/api/v1/workspaces/{workspace_id}/credentials")
    ).json()[0]["id"]
    other_model_id = (
        await client.post(
            f"/api/v1/workspaces/{workspace_id}/models",
            json={
                "credential_id": credential_id,
                "provider_model_id": "fake-large",
                "display_name": "Fake Large",
            },
        )
    ).json()["id"]
    conversation_id = (
        await client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id}
        )
    ).json()["id"]

    updated = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}",
        json={"model_id": other_model_id},
    )

    assert updated.status_code == 200
    assert updated.json()["model_id"] == other_model_id


async def test_deleting_a_conversation_removes_it_from_the_list(client: AsyncClient) -> None:
    """A deleted conversation 404s directly and disappears from the workspace's list."""
    workspace_id, model_id = await _workspace_with_model(client)
    conversation_id = (
        await client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id}
        )
    ).json()["id"]

    deleted = await client.delete(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}"
    )

    assert deleted.status_code == 204
    fetched = await client.get(f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}")
    assert fetched.status_code == 404
    listed = await client.get(f"/api/v1/workspaces/{workspace_id}/conversations")
    assert conversation_id not in {c["id"] for c in listed.json()}


async def test_listing_conversations_honors_limit_and_before(client: AsyncClient) -> None:
    """A thin smoke test over the HTTP wiring — list_conversations()'s own pagination logic is
    exercised thoroughly in test_chat_service.py; this just confirms the query params reach it."""
    workspace_id, model_id = await _workspace_with_model(client)
    for _ in range(3):
        await client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id}
        )

    first_page = await client.get(
        f"/api/v1/workspaces/{workspace_id}/conversations", params={"limit": 2}
    )
    assert first_page.status_code == 200
    first_items = first_page.json()
    assert len(first_items) == 2

    second_page = await client.get(
        f"/api/v1/workspaces/{workspace_id}/conversations",
        params={"limit": 2, "before": first_items[-1]["updated_at"]},
    )
    assert second_page.status_code == 200
    second_items = second_page.json()
    assert {c["id"] for c in first_items}.isdisjoint({c["id"] for c in second_items})


async def test_idempotency_key_prevents_a_duplicate_send(client: AsyncClient) -> None:
    """Two sends with the same Idempotency-Key produce only one user/assistant pair."""
    workspace_id, model_id = await _workspace_with_model(client)
    conversation = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id}
    )
    conversation_id = conversation.json()["id"]
    headers = {"Idempotency-Key": "retry-xyz"}

    await _consume_sse(
        client,
        "POST",
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
        json={"content": "Hi"},
        headers=headers,
    )
    await _consume_sse(
        client,
        "POST",
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
        json={"content": "Hi"},
        headers=headers,
    )

    messages = await client.get(f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages")
    assert [m["role"] for m in messages.json()] == ["user", "assistant"]


async def test_active_generation_is_reported_while_streaming_then_clears(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent request sees the generation as active mid-stream, then not, once it's done."""
    monkeypatch.setattr("app.services.chat.build_provider", _slow_build_provider)
    workspace_id, model_id = await _workspace_with_model(client)
    conversation = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id}
    )
    conversation_id = conversation.json()["id"]

    async def send() -> None:
        await _consume_sse(
            client,
            "POST",
            f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
            json={"content": "Hi"},
        )

    task = asyncio.create_task(send())
    await asyncio.sleep(0.08)  # let the slow provider start streaming
    mid_stream = await client.get(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/active-generation"
    )
    assert mid_stream.json()["generation_id"] is not None

    await task
    after = await client.get(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/active-generation"
    )
    assert after.json()["generation_id"] is None


async def test_resume_after_completion_replays_the_whole_reply(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh connection to the resume endpoint (as a page reload would open) replays everything —
    the frontend has no per-tab Last-Event-ID surviving a reload, so it asks for the whole stream
    from the start instead. Uses the slow provider so there's a reliable window to capture the
    generation id (via the active-generation endpoint) before it finishes and gets cleared.
    """
    monkeypatch.setattr("app.services.chat.build_provider", _slow_build_provider)
    workspace_id, model_id = await _workspace_with_model(client)
    conversation = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id}
    )
    conversation_id = conversation.json()["id"]

    async def send() -> list[tuple[str, str]]:
        return await _consume_sse(
            client,
            "POST",
            f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
            json={"content": "Hi"},
        )

    task = asyncio.create_task(send())
    await asyncio.sleep(0.08)
    active = await client.get(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/active-generation"
    )
    generation_id = active.json()["generation_id"]
    assert generation_id is not None

    original_events = await task  # let it finish naturally, uninterrupted

    resumed_events = await _consume_sse(
        client,
        "GET",
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}"
        f"/generations/{generation_id}",
    )

    assert resumed_events == original_events


async def test_stopping_mid_stream_truncates_the_reply(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stopping partway through leaves fewer deltas than a full reply would have."""
    monkeypatch.setattr("app.services.chat.build_provider", _slow_build_provider)
    workspace_id, model_id = await _workspace_with_model(client)
    conversation = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id}
    )
    conversation_id = conversation.json()["id"]

    events: list[tuple[str, str]] = []

    async def send() -> None:
        events.extend(
            await _consume_sse(
                client,
                "POST",
                f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
                json={"content": "Hi"},
            )
        )

    task = asyncio.create_task(send())
    await asyncio.sleep(0.08)
    active = await client.get(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/active-generation"
    )
    generation_id = active.json()["generation_id"]
    await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}"
        f"/generations/{generation_id}/stop"
    )
    await task

    delta_count = len([e for e in events if e[0] == "delta"])
    assert 0 < delta_count < 4
    assert events[-1] == ("done", '{"finish_reason": "stopped"}')


async def test_non_member_cannot_send_messages(client: AsyncClient) -> None:
    """A conversation in a workspace the caller doesn't belong to is a plain 404."""
    workspace_id, model_id = await _workspace_with_model(client)
    conversation = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id}
    )
    conversation_id = conversation.json()["id"]
    await client.post("/api/v1/auth/logout")

    await client.post("/api/v1/auth/signup", json=MEMBER)
    await client.post("/api/v1/auth/login", json=MEMBER)
    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
        json={"content": "Hi"},
    )

    assert response.status_code == 404


async def test_a_private_conversation_is_hidden_from_teammates_until_shared(
    client: AsyncClient,
) -> None:
    """Full round trip: a fellow workspace member can't see or list a conversation they weren't
    invited into, but can once its owner flips the sharing flag on."""
    workspace_id, model_id = await _workspace_with_model(client)
    conversation_id = (
        await client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id}
        )
    ).json()["id"]
    invite = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": MEMBER["email"], "role": "member"},
    )
    token = invite.json()["token"]
    await client.post("/api/v1/auth/logout")
    await client.post("/api/v1/auth/signup", json=MEMBER)
    await client.post("/api/v1/auth/login", json=MEMBER)
    await client.post(f"/api/v1/invitations/{token}/accept")

    hidden = await client.get(f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}")
    listed = await client.get(f"/api/v1/workspaces/{workspace_id}/conversations")
    assert hidden.status_code == 404
    assert listed.json() == []

    await client.post("/api/v1/auth/logout")
    await client.post("/api/v1/auth/login", json=OWNER)
    shared = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}", json={"shared": True}
    )
    assert shared.status_code == 200
    assert shared.json()["shared"] is True

    await client.post("/api/v1/auth/logout")
    await client.post("/api/v1/auth/login", json=MEMBER)
    now_visible = await client.get(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}"
    )
    listed_again = await client.get(f"/api/v1/workspaces/{workspace_id}/conversations")
    assert now_visible.status_code == 200
    assert conversation_id in {c["id"] for c in listed_again.json()}


async def test_a_private_conversation_cannot_be_shared_by_a_non_owner(client: AsyncClient) -> None:
    """A teammate can't even attempt to share a conversation that's still private to someone
    else — there's nothing to be refused permission on, so it 404s, not 403s."""
    workspace_id, model_id = await _workspace_with_model(client)
    conversation_id = (
        await client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id}
        )
    ).json()["id"]
    invite = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": MEMBER["email"], "role": "admin"},
    )
    token = invite.json()["token"]
    await client.post("/api/v1/auth/logout")
    await client.post("/api/v1/auth/signup", json=MEMBER)
    await client.post("/api/v1/auth/login", json=MEMBER)
    await client.post(f"/api/v1/invitations/{token}/accept")

    response = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}", json={"shared": True}
    )

    assert response.status_code == 404


async def test_a_non_owner_cannot_unshare_a_shared_conversation(client: AsyncClient) -> None:
    """Once shared, a teammate can see and use the conversation — but can't revoke that access
    for everyone else by unsharing it. Only its owner controls that flag."""
    workspace_id, model_id = await _workspace_with_model(client)
    conversation_id = (
        await client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id}
        )
    ).json()["id"]
    await client.patch(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}", json={"shared": True}
    )
    invite = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": MEMBER["email"], "role": "member"},
    )
    token = invite.json()["token"]
    await client.post("/api/v1/auth/logout")
    await client.post("/api/v1/auth/signup", json=MEMBER)
    await client.post("/api/v1/auth/login", json=MEMBER)
    await client.post(f"/api/v1/invitations/{token}/accept")

    response = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}", json={"shared": False}
    )

    assert response.status_code == 403


async def test_sending_is_blocked_while_the_model_s_provider_is_disabled(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """The killswitch demo: flip `provider.anthropic` off for the workspace, new sends 403 —
    but a conversation already read (see test_non_member_cannot_send_messages) stays readable."""
    workspace_id, model_id = await _workspace_with_model(client)
    conversation = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id}
    )
    conversation_id = conversation.json()["id"]
    flag = await db.scalar(select(FeatureFlag).where(FeatureFlag.key == "provider.anthropic"))
    assert flag is not None
    await set_override(
        db,
        redis_client,
        flag_id=flag.id,
        scope=FlagScope.WORKSPACE,
        scope_id=uuid.UUID(workspace_id),
        value=False,
    )
    await db.commit()  # this test's `db` session must commit for the client's own connection to see it

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
        json={"content": "Hi"},
    )

    assert response.status_code == 403
    still_readable = await client.get(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages"
    )
    assert still_readable.status_code == 200


async def test_a_tool_call_streams_live_and_is_listed_after_the_fact(
    db: AsyncSession, redis_client: Redis, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full round trip: a tool call shows up as SSE events while it's happening, and as a row
    the tool-invocations endpoint returns once the reply is done — a reload shouldn't lose it."""
    workspace_id, model_id = await _workspace_with_model(client)
    flag = await db.scalar(select(FeatureFlag).where(FeatureFlag.key == "tools"))
    assert flag is not None
    await set_override(
        db,
        redis_client,
        flag_id=flag.id,
        scope=FlagScope.WORKSPACE,
        scope_id=uuid.UUID(workspace_id),
        value=True,
    )
    owner = await db.scalar(select(User).where(User.email == OWNER["email"]))
    assert owner is not None
    tool = Tool(
        workspace_id=uuid.UUID(workspace_id),
        name="get_weather",
        description="Get the current weather for a city.",
        parameters={"type": "object", "properties": {"city": {"type": "string"}}},
        kind=ToolKind.BUILTIN,
        enabled=True,
        created_by=owner.id,
    )
    db.add(tool)
    await db.commit()
    monkeypatch.setattr("app.services.chat.build_provider", _tool_calling_build_provider)
    conversation = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id}
    )
    conversation_id = conversation.json()["id"]

    events = await _consume_sse(
        client,
        "POST",
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
        json={"content": "What's the weather in Paris?"},
    )

    event_types = [e for e, _ in events]
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert event_types[-1] == "done"

    invocations = await client.get(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/tool-invocations"
    )
    assert invocations.status_code == 200
    body = invocations.json()
    assert len(body) == 1
    assert body[0]["name"] == "get_weather"
    assert body[0]["tool_id"] == str(tool.id)
