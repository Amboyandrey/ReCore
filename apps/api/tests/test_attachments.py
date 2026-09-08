"""Uploading a file into a conversation — gated by the `attachments` flag — and its extraction.

Routes provider calls through FakeProvider via monkeypatch, same as test_chat_router.py.
"""

import uuid

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FeatureFlag, FlagScope
from app.providers.fake import VALID_KEY, FakeProvider
from app.services.flags import set_override

OWNER = {"email": "owner@example.com", "password": "correct horse battery staple"}


def _fake_build_provider(provider, *, api_key, base_url):
    return FakeProvider(api_key=api_key, base_url=base_url)


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
        },
    )
    return workspace_id, model.json()["id"]


async def _enable_attachments(db: AsyncSession, redis: Redis, *, workspace_id: str) -> None:
    """Flip the `attachments` flag on for one workspace — the same lever the admin UI pulls."""
    flag = await db.scalar(select(FeatureFlag).where(FeatureFlag.key == "attachments"))
    assert flag is not None
    await set_override(
        db, redis, flag_id=flag.id, scope=FlagScope.WORKSPACE, scope_id=uuid.UUID(workspace_id), value=True
    )
    await db.commit()  # this test's `db` session must commit for the client's own connection to see it


async def test_upload_is_blocked_while_the_flag_is_off(client: AsyncClient) -> None:
    """`attachments` defaults to off — uploading 404s entirely, same as a route that doesn't exist."""
    workspace_id, model_id = await _workspace_with_model(client)
    conversation_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id})
    ).json()["id"]

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 404


async def test_uploading_a_text_file_extracts_its_content(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """A text/plain upload is decoded and stored as `extracted_text`, marked `done`."""
    workspace_id, model_id = await _workspace_with_model(client)
    await _enable_attachments(db, redis_client, workspace_id=workspace_id)
    conversation_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id})
    ).json()["id"]

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments",
        files={"file": ("notes.txt", b"the quarterly numbers look good", "text/plain")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["extract_status"] == "done"
    assert body["extracted_text"] == "the quarterly numbers look good"
    assert body["message_id"] is None  # not attached to a message yet


async def test_uploading_a_binary_file_is_marked_unsupported(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """A mime type this phase doesn't know how to read is stored but flagged, not silently empty."""
    workspace_id, model_id = await _workspace_with_model(client)
    await _enable_attachments(db, redis_client, workspace_id=workspace_id)
    conversation_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id})
    ).json()["id"]

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments",
        files={"file": ("photo.png", b"\x89PNG\r\n\x1a\n\x00\x00", "image/png")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["extract_status"] == "unsupported"
    assert body["extracted_text"] is None


async def test_listing_returns_every_attachment_uploaded_into_the_conversation(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """Two uploads into the same conversation both come back, in upload order."""
    workspace_id, model_id = await _workspace_with_model(client)
    await _enable_attachments(db, redis_client, workspace_id=workspace_id)
    conversation_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id})
    ).json()["id"]
    base = f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments"
    await client.post(base, files={"file": ("a.txt", b"first", "text/plain")})
    await client.post(base, files={"file": ("b.txt", b"second", "text/plain")})

    listed = await client.get(base)

    assert [a["original_filename"] for a in listed.json()] == ["a.txt", "b.txt"]


async def test_sending_a_message_with_an_attachment_feeds_its_text_to_the_model(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """An attached file's extracted text reaches the provider alongside the user's own message —
    proven indirectly via FakeProvider's input-token count, which counts words it was sent."""
    workspace_id, model_id = await _workspace_with_model(client)
    await _enable_attachments(db, redis_client, workspace_id=workspace_id)
    conversation_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id})
    ).json()["id"]
    upload = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments",
        files={"file": ("notes.txt", b"one two three four five", "text/plain")},
    )
    attachment_id = upload.json()["id"]

    async with client.stream(
        "POST",
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
        json={"content": "Hi", "attachment_ids": [attachment_id]},
    ) as response:
        async for _ in response.aiter_lines():
            pass

    messages = (
        await client.get(f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages")
    ).json()
    assistant = messages[1]
    # "Hi" alone is one word; with the attachment's five words (plus the bracketed filename
    # marker) folded in, the fake provider — which counts words in what it was sent — sees more.
    assert assistant["tokens_in"] > 1

    attachments = (
        await client.get(f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/attachments")
    ).json()
    assert attachments[0]["message_id"] == messages[0]["id"]  # linked to the user's turn
