"""The usage dashboard HTTP surface: real chat usage, aggregated and role-gated."""

import pytest
from httpx import AsyncClient

from app.providers.fake import VALID_KEY, FakeProvider

OWNER = {"email": "owner@example.com", "password": "correct horse battery staple"}
MEMBER = {"email": "member@example.com", "password": "correct horse battery staple"}


def _fake_build_provider(provider, *, api_key, base_url):
    return FakeProvider(api_key=api_key, base_url=base_url)


@pytest.fixture(autouse=True)
def _fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
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


async def _consume_sse(client: AsyncClient, url: str, **kwargs) -> None:
    async with client.stream("POST", url, **kwargs) as response:
        async for _ in response.aiter_lines():
            pass


async def test_usage_reflects_a_real_send(client: AsyncClient) -> None:
    """After one chat exchange, the dashboard shows it under the right model."""
    workspace_id, model_id = await _workspace_with_model(client)
    conversation_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/conversations", json={"model_id": model_id})
    ).json()["id"]

    await _consume_sse(
        client,
        f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
        json={"content": "Hi there"},
    )

    usage = (await client.get(f"/api/v1/workspaces/{workspace_id}/usage")).json()
    assert len(usage["by_model"]) == 1
    assert usage["by_model"][0]["display_name"] == "Fake Small"
    assert usage["by_model"][0]["message_count"] == 1
    assert len(usage["by_member"]) == 1
    assert usage["by_member"][0]["email"] == OWNER["email"]
    assert len(usage["by_day"]) == 1


async def test_a_plain_member_cannot_view_usage(client: AsyncClient) -> None:
    """Usage is admin-and-up, per ARCHITECTURE.md's route table — a viewer/member isn't enough."""
    workspace_id, _model_id = await _workspace_with_model(client)
    invite = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": MEMBER["email"], "role": "member"},
    )
    token = invite.json()["token"]
    await client.post("/api/v1/auth/logout")
    await client.post("/api/v1/auth/signup", json=MEMBER)
    await client.post("/api/v1/auth/login", json=MEMBER)
    await client.post(f"/api/v1/invitations/{token}/accept")

    response = await client.get(f"/api/v1/workspaces/{workspace_id}/usage")

    assert response.status_code == 403
