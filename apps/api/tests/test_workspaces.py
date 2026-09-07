"""Workspace CRUD and the tenancy dependency chain — 404 for non-members, never 403."""

from httpx import AsyncClient

CREDENTIALS_A = {"email": "founder@example.com", "password": "correct horse battery staple"}
CREDENTIALS_B = {"email": "outsider@example.com", "password": "correct horse battery staple"}


async def _signed_in_client(client: AsyncClient, credentials: dict[str, str]) -> AsyncClient:
    """Sign up and log in, leaving the session cookie on the shared client."""
    await client.post("/api/v1/auth/signup", json=credentials)
    await client.post("/api/v1/auth/login", json=credentials)
    return client


async def test_creating_a_workspace_makes_the_caller_owner(client: AsyncClient) -> None:
    """The creator is immediately a member of their own workspace, at the owner role."""
    await _signed_in_client(client, CREDENTIALS_A)

    response = await client.post("/api/v1/workspaces", json={"name": "Acme Inc"})

    assert response.status_code == 201
    body = response.json()
    assert body["slug"] == "acme-inc"
    assert body["role"] == "owner"


async def test_duplicate_names_get_disambiguated_slugs(client: AsyncClient) -> None:
    """A second workspace with the same name gets a distinct, still-readable slug."""
    await _signed_in_client(client, CREDENTIALS_A)

    first = await client.post("/api/v1/workspaces", json={"name": "Acme Inc"})
    second = await client.post("/api/v1/workspaces", json={"name": "Acme Inc"})

    assert first.json()["slug"] != second.json()["slug"]
    assert second.json()["slug"] == "acme-inc-2"


async def test_list_only_returns_the_callers_own_workspaces(client: AsyncClient) -> None:
    """Workspace B never appears in A's list, and vice versa."""
    await _signed_in_client(client, CREDENTIALS_A)
    await client.post("/api/v1/workspaces", json={"name": "Acme"})
    await client.post("/api/v1/auth/logout")

    await _signed_in_client(client, CREDENTIALS_B)
    await client.post("/api/v1/workspaces", json={"name": "Widgets Co"})

    response = await client.get("/api/v1/workspaces")

    names = [w["name"] for w in response.json()]
    assert names == ["Widgets Co"]


async def test_non_member_gets_404_not_403(client: AsyncClient) -> None:
    """A workspace that exists but isn't the caller's own looks exactly like it doesn't exist."""
    await _signed_in_client(client, CREDENTIALS_A)
    created = await client.post("/api/v1/workspaces", json={"name": "Acme"})
    workspace_id = created.json()["id"]
    await client.post("/api/v1/auth/logout")

    await _signed_in_client(client, CREDENTIALS_B)
    response = await client.get(f"/api/v1/workspaces/{workspace_id}")

    assert response.status_code == 404


async def test_unknown_workspace_id_also_gets_404(client: AsyncClient) -> None:
    """A syntactically valid but nonexistent workspace id is a plain 404."""
    await _signed_in_client(client, CREDENTIALS_A)

    response = await client.get("/api/v1/workspaces/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404


async def test_workspace_routes_require_authentication(client: AsyncClient) -> None:
    """No session at all is a 401, before tenancy is even considered."""
    response = await client.get("/api/v1/workspaces")

    assert response.status_code == 401
