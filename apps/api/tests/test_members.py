"""Listing, promoting/demoting, and removing members — and the last-owner invariant around it."""

from httpx import AsyncClient

OWNER = {"email": "owner@example.com", "password": "correct horse battery staple"}
MEMBER = {"email": "member@example.com", "password": "correct horse battery staple"}


async def _workspace_with_member(client: AsyncClient) -> tuple[str, str]:
    """Owner creates a workspace and invites MEMBER in, who accepts. Returns (workspace_id, member_id)."""
    await client.post("/api/v1/auth/signup", json=OWNER)
    await client.post("/api/v1/auth/login", json=OWNER)
    workspace_id = str((await client.post("/api/v1/workspaces", json={"name": "Acme"})).json()["id"])
    invite = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": MEMBER["email"], "role": "member"},
    )
    token = invite.json()["token"]
    await client.post("/api/v1/auth/logout")

    await client.post("/api/v1/auth/signup", json=MEMBER)
    await client.post("/api/v1/auth/login", json=MEMBER)
    await client.post(f"/api/v1/invitations/{token}/accept")
    me = await client.get("/api/v1/auth/me")
    member_user_id = me.json()["id"]

    await client.post("/api/v1/auth/logout")
    await client.post("/api/v1/auth/login", json=OWNER)
    return workspace_id, member_user_id


async def test_list_members_includes_both(client: AsyncClient) -> None:
    """Both the owner and the accepted member show up in the roster."""
    workspace_id, _ = await _workspace_with_member(client)

    response = await client.get(f"/api/v1/workspaces/{workspace_id}/members")

    emails = {m["email"] for m in response.json()}
    assert emails == {OWNER["email"], MEMBER["email"]}


async def test_admin_can_promote_a_member(client: AsyncClient) -> None:
    """An owner can raise a member up to admin."""
    workspace_id, member_id = await _workspace_with_member(client)

    response = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/members/{member_id}", json={"role": "admin"}
    )

    assert response.status_code == 200
    assert response.json()["role"] == "admin"


async def test_cannot_remove_the_last_owner(client: AsyncClient) -> None:
    """A workspace's sole owner can't be removed, even by themselves."""
    workspace_id, _ = await _workspace_with_member(client)
    me = await client.get("/api/v1/auth/me")
    owner_id = me.json()["id"]

    response = await client.delete(f"/api/v1/workspaces/{workspace_id}/members/{owner_id}")

    assert response.status_code == 409


async def test_admin_cannot_touch_the_owner_role(client: AsyncClient) -> None:
    """An admin (not an owner) can't demote or remove an owner."""
    workspace_id, member_id = await _workspace_with_member(client)
    await client.patch(f"/api/v1/workspaces/{workspace_id}/members/{member_id}", json={"role": "admin"})
    me = await client.get("/api/v1/auth/me")
    owner_id = me.json()["id"]

    await client.post("/api/v1/auth/logout")
    await client.post("/api/v1/auth/login", json=MEMBER)

    response = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/members/{owner_id}", json={"role": "member"}
    )

    assert response.status_code == 403


async def test_member_cannot_change_roles(client: AsyncClient) -> None:
    """A plain member (below admin) can't change anyone's role, including their own."""
    workspace_id, member_id = await _workspace_with_member(client)
    await client.post("/api/v1/auth/logout")
    await client.post("/api/v1/auth/login", json=MEMBER)

    response = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/members/{member_id}", json={"role": "admin"}
    )

    assert response.status_code == 403


async def test_removing_a_member_drops_them_from_the_list(client: AsyncClient) -> None:
    """A removed member no longer appears in the roster."""
    workspace_id, member_id = await _workspace_with_member(client)

    remove = await client.delete(f"/api/v1/workspaces/{workspace_id}/members/{member_id}")
    assert remove.status_code == 204

    members = await client.get(f"/api/v1/workspaces/{workspace_id}/members")
    assert member_id not in [m["user_id"] for m in members.json()]
