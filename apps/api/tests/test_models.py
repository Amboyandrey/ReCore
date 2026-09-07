"""Discovering, enabling, listing, and disabling models — validated against a fake provider."""

import pytest
from httpx import AsyncClient

from app.providers.fake import VALID_KEY, FakeProvider

OWNER = {"email": "owner@example.com", "password": "correct horse battery staple"}


def _fake_build_provider(provider, *, api_key, base_url):
    """Stand in for the real registry — used by both credentials and models below."""
    return FakeProvider(api_key=api_key, base_url=base_url)


@pytest.fixture(autouse=True)
def _fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route every provider build through FakeProvider, in both services that call it."""
    monkeypatch.setattr("app.services.credentials.build_provider", _fake_build_provider)
    monkeypatch.setattr("app.services.models.build_provider", _fake_build_provider)


async def _workspace_with_credential(client: AsyncClient) -> tuple[str, str]:
    """Sign up, log in, create a workspace, and register a valid credential in it."""
    await client.post("/api/v1/auth/signup", json=OWNER)
    await client.post("/api/v1/auth/login", json=OWNER)
    workspace_id = str((await client.post("/api/v1/workspaces", json={"name": "Acme"})).json()["id"])
    credential = await client.post(
        f"/api/v1/workspaces/{workspace_id}/credentials",
        json={"provider": "anthropic", "label": "Prod", "api_key": VALID_KEY},
    )
    return workspace_id, str(credential.json()["id"])


async def test_available_models_come_from_the_provider(client: AsyncClient) -> None:
    """The fake provider's two fixed models show up as available, unenabled."""
    workspace_id, credential_id = await _workspace_with_credential(client)

    response = await client.get(
        f"/api/v1/workspaces/{workspace_id}/credentials/{credential_id}/available-models"
    )

    ids = {m["id"] for m in response.json()}
    assert ids == {"fake-small", "fake-large"}


async def test_enabling_a_model_makes_it_listed_with_its_pricing(client: AsyncClient) -> None:
    """An enabled model appears in the workspace's model list with the pricing it was given."""
    workspace_id, credential_id = await _workspace_with_credential(client)

    enabled = await client.post(
        f"/api/v1/workspaces/{workspace_id}/models",
        json={
            "credential_id": credential_id,
            "provider_model_id": "fake-large",
            "display_name": "Fake Large",
            "context_window": 200_000,
            "cost_per_mtok_in": 3.0,
            "cost_per_mtok_out": 15.0,
        },
    )
    assert enabled.status_code == 201

    listed = await client.get(f"/api/v1/workspaces/{workspace_id}/models")
    models = listed.json()
    assert len(models) == 1
    assert models[0]["provider_model_id"] == "fake-large"
    assert models[0]["cost_per_mtok_out"] == 15.0


async def test_disabling_a_model_removes_it_from_the_list(client: AsyncClient) -> None:
    """A disabled model no longer shows up in the enabled list."""
    workspace_id, credential_id = await _workspace_with_credential(client)
    enabled = await client.post(
        f"/api/v1/workspaces/{workspace_id}/models",
        json={
            "credential_id": credential_id,
            "provider_model_id": "fake-small",
            "display_name": "Fake Small",
        },
    )
    model_id = enabled.json()["id"]

    disabled = await client.delete(f"/api/v1/workspaces/{workspace_id}/models/{model_id}")
    assert disabled.status_code == 204

    listed = await client.get(f"/api/v1/workspaces/{workspace_id}/models")
    assert listed.json() == []


async def test_re_enabling_a_disabled_model_updates_its_pricing(client: AsyncClient) -> None:
    """Enabling the same (credential, model) pair again updates it in place rather than erroring."""
    workspace_id, credential_id = await _workspace_with_credential(client)
    first = await client.post(
        f"/api/v1/workspaces/{workspace_id}/models",
        json={
            "credential_id": credential_id,
            "provider_model_id": "fake-small",
            "display_name": "Fake Small",
            "cost_per_mtok_in": 1.0,
        },
    )
    await client.delete(f"/api/v1/workspaces/{workspace_id}/models/{first.json()['id']}")

    second = await client.post(
        f"/api/v1/workspaces/{workspace_id}/models",
        json={
            "credential_id": credential_id,
            "provider_model_id": "fake-small",
            "display_name": "Fake Small",
            "cost_per_mtok_in": 2.0,
        },
    )

    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["cost_per_mtok_in"] == 2.0

    listed = await client.get(f"/api/v1/workspaces/{workspace_id}/models")
    assert len(listed.json()) == 1
