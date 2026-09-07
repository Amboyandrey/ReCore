"""Invite, preview, and accept — including the invariant that only the invited email can accept."""

from httpx import AsyncClient

OWNER = {"email": "owner@example.com", "password": "correct horse battery staple"}
INVITEE = {"email": "invitee@example.com", "password": "correct horse battery staple"}
STRANGER = {"email": "stranger@example.com", "password": "correct horse battery staple"}


async def _owner_with_workspace(client: AsyncClient) -> str:
    """Sign up and log in as the owner, create a workspace, and return its id."""
    await client.post("/api/v1/auth/signup", json=OWNER)
    await client.post("/api/v1/auth/login", json=OWNER)
    created = await client.post("/api/v1/workspaces", json={"name": "Acme"})
    return str(created.json()["id"])


async def test_invite_returns_a_token_once(client: AsyncClient) -> None:
    """Creating an invite returns its plaintext token — the only time it's ever exposed."""
    workspace_id = await _owner_with_workspace(client)

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": INVITEE["email"], "role": "member"},
    )

    assert response.status_code == 201
    assert response.json()["token"]


async def test_preview_works_without_being_signed_in(client: AsyncClient) -> None:
    """Anyone with the link can see what they're being invited to, before creating an account."""
    workspace_id = await _owner_with_workspace(client)
    invite = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": INVITEE["email"], "role": "admin"},
    )
    token = invite.json()["token"]
    await client.post("/api/v1/auth/logout")

    response = await client.get(f"/api/v1/invitations/{token}")

    assert response.status_code == 200
    assert response.json()["role"] == "admin"
    assert response.json()["workspace_name"] == "Acme"


async def test_accepting_with_the_wrong_email_is_rejected(client: AsyncClient) -> None:
    """A stranger who obtains the token still can't accept an invite addressed to someone else."""
    workspace_id = await _owner_with_workspace(client)
    invite = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": INVITEE["email"], "role": "member"},
    )
    token = invite.json()["token"]
    await client.post("/api/v1/auth/logout")

    await client.post("/api/v1/auth/signup", json=STRANGER)
    await client.post("/api/v1/auth/login", json=STRANGER)
    response = await client.post(f"/api/v1/invitations/{token}/accept")

    assert response.status_code == 400


async def test_accepting_joins_the_workspace_at_the_invited_role(client: AsyncClient) -> None:
    """The invited email, once signed in, becomes a member at exactly the role it was offered."""
    workspace_id = await _owner_with_workspace(client)
    invite = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": INVITEE["email"], "role": "admin"},
    )
    token = invite.json()["token"]
    await client.post("/api/v1/auth/logout")

    await client.post("/api/v1/auth/signup", json=INVITEE)
    await client.post("/api/v1/auth/login", json=INVITEE)
    response = await client.post(f"/api/v1/invitations/{token}/accept")

    assert response.status_code == 200
    assert response.json()["role"] == "admin"
    assert response.json()["id"] == workspace_id


async def test_accepted_invite_cannot_be_reused(client: AsyncClient) -> None:
    """A token that's already been redeemed doesn't work a second time."""
    workspace_id = await _owner_with_workspace(client)
    invite = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": INVITEE["email"], "role": "member"},
    )
    token = invite.json()["token"]
    await client.post("/api/v1/auth/logout")

    await client.post("/api/v1/auth/signup", json=INVITEE)
    await client.post("/api/v1/auth/login", json=INVITEE)
    await client.post(f"/api/v1/invitations/{token}/accept")

    response = await client.post(f"/api/v1/invitations/{token}/accept")
    assert response.status_code == 400


async def test_only_admins_can_invite(client: AsyncClient) -> None:
    """A plain member can't invite others into the workspace."""
    workspace_id = await _owner_with_workspace(client)
    invite = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": INVITEE["email"], "role": "member"},
    )
    token = invite.json()["token"]
    await client.post("/api/v1/auth/logout")
    await client.post("/api/v1/auth/signup", json=INVITEE)
    await client.post("/api/v1/auth/login", json=INVITEE)
    await client.post(f"/api/v1/invitations/{token}/accept")

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": STRANGER["email"], "role": "member"},
    )

    assert response.status_code == 403


async def test_only_an_owner_can_invite_someone_as_owner(client: AsyncClient) -> None:
    """An admin can't hand out full control by inviting a colluding account in as owner."""
    workspace_id = await _owner_with_workspace(client)
    invite = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": INVITEE["email"], "role": "admin"},
    )
    token = invite.json()["token"]
    await client.post("/api/v1/auth/logout")
    await client.post("/api/v1/auth/signup", json=INVITEE)
    await client.post("/api/v1/auth/login", json=INVITEE)
    await client.post(f"/api/v1/invitations/{token}/accept")

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": STRANGER["email"], "role": "owner"},
    )

    assert response.status_code == 403
