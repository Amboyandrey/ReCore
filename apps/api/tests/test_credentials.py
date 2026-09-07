"""Credential registration, listing, and deletion — validated against a fake provider so no real
network call happens. What's under test is the routing, tenancy, encryption, and storage."""

import pytest
from httpx import AsyncClient

from app.providers.fake import VALID_KEY, FakeProvider

OWNER = {"email": "owner@example.com", "password": "correct horse battery staple"}
MEMBER = {"email": "member@example.com", "password": "correct horse battery staple"}


@pytest.fixture(autouse=True)
def _fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route every provider build through FakeProvider, regardless of which provider was asked for."""
    monkeypatch.setattr(
        "app.services.credentials.build_provider",
        lambda provider, *, api_key, base_url: FakeProvider(api_key=api_key, base_url=base_url),
    )


async def _owner_with_workspace(client: AsyncClient) -> str:
    """Sign up and log in as the owner, create a workspace, and return its id."""
    await client.post("/api/v1/auth/signup", json=OWNER)
    await client.post("/api/v1/auth/login", json=OWNER)
    created = await client.post("/api/v1/workspaces", json={"name": "Acme"})
    return str(created.json()["id"])


async def test_valid_key_is_registered_and_never_returned(client: AsyncClient) -> None:
    """A working key is stored and the response carries only its last4, never the key itself."""
    workspace_id = await _owner_with_workspace(client)

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/credentials",
        json={"provider": "anthropic", "label": "Prod", "api_key": VALID_KEY},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["last4"] == VALID_KEY[-4:]
    assert "api_key" not in body
    assert VALID_KEY not in response.text


async def test_invalid_key_is_rejected_and_never_stored(client: AsyncClient) -> None:
    """A key the provider rejects never becomes a row — nothing unvalidated is ever stored."""
    workspace_id = await _owner_with_workspace(client)

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/credentials",
        json={"provider": "anthropic", "label": "Prod", "api_key": "wrong-key"},
    )
    assert response.status_code == 422

    listed = await client.get(f"/api/v1/workspaces/{workspace_id}/credentials")
    assert listed.json() == []


async def test_openai_compatible_requires_a_base_url(client: AsyncClient) -> None:
    """An openai_compatible credential with nowhere to send requests is rejected up front."""
    workspace_id = await _owner_with_workspace(client)

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/credentials",
        json={"provider": "openai_compatible", "label": "Local", "api_key": VALID_KEY},
    )

    assert response.status_code == 422


async def test_unsafe_base_url_is_rejected_before_any_provider_call(client: AsyncClient) -> None:
    """A base URL pointing at an internal address is refused, whether or not the key is valid."""
    workspace_id = await _owner_with_workspace(client)

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/credentials",
        json={
            "provider": "openai_compatible",
            "label": "Local",
            "api_key": VALID_KEY,
            "base_url": "http://127.0.0.1:6379",
        },
    )

    assert response.status_code == 400


async def test_list_and_delete(client: AsyncClient) -> None:
    """A registered credential shows up in the list, and deleting it removes it for good."""
    workspace_id = await _owner_with_workspace(client)
    created = await client.post(
        f"/api/v1/workspaces/{workspace_id}/credentials",
        json={"provider": "anthropic", "label": "Prod", "api_key": VALID_KEY},
    )
    credential_id = created.json()["id"]

    listed = await client.get(f"/api/v1/workspaces/{workspace_id}/credentials")
    assert len(listed.json()) == 1

    deleted = await client.delete(f"/api/v1/workspaces/{workspace_id}/credentials/{credential_id}")
    assert deleted.status_code == 204

    listed_again = await client.get(f"/api/v1/workspaces/{workspace_id}/credentials")
    assert listed_again.json() == []


async def test_only_admins_can_register_credentials(client: AsyncClient) -> None:
    """A plain member can't add a billing-relevant API key to the workspace."""
    workspace_id = await _owner_with_workspace(client)
    invite = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": MEMBER["email"], "role": "member"},
    )
    token = invite.json()["token"]
    await client.post("/api/v1/auth/logout")
    await client.post("/api/v1/auth/signup", json=MEMBER)
    await client.post("/api/v1/auth/login", json=MEMBER)
    await client.post(f"/api/v1/invitations/{token}/accept")

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/credentials",
        json={"provider": "anthropic", "label": "Prod", "api_key": VALID_KEY},
    )

    assert response.status_code == 403
