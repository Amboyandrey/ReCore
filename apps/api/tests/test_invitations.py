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


async def test_inviting_the_same_email_twice_re_sends_rather_than_duplicating(
    client: AsyncClient,
) -> None:
    """A second invite to an address that's already pending replaces it — the workspace ends up
    with one pending invitation for that email, not two, and the new token is the one that works."""
    workspace_id = await _owner_with_workspace(client)
    first = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": INVITEE["email"], "role": "member"},
    )
    first_token = first.json()["token"]

    second = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": INVITEE["email"], "role": "admin"},
    )
    second_token = second.json()["token"]

    assert first.json()["id"] == second.json()["id"]  # same row, re-sent, not a new one
    listed = await client.get(f"/api/v1/workspaces/{workspace_id}/invitations")
    assert len(listed.json()) == 1
    assert listed.json()[0]["role"] == "admin"

    stale = await client.get(f"/api/v1/invitations/{first_token}")
    assert stale.status_code == 400
    fresh = await client.get(f"/api/v1/invitations/{second_token}")
    assert fresh.status_code == 200


async def test_revoking_an_invitation_removes_it_and_invalidates_its_link(
    client: AsyncClient,
) -> None:
    """A revoked invite disappears from the pending list and its token stops working."""
    workspace_id = await _owner_with_workspace(client)
    invite = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": INVITEE["email"], "role": "member"},
    )
    invitation_id, token = invite.json()["id"], invite.json()["token"]

    revoked = await client.delete(f"/api/v1/workspaces/{workspace_id}/invitations/{invitation_id}")

    assert revoked.status_code == 204
    listed = await client.get(f"/api/v1/workspaces/{workspace_id}/invitations")
    assert listed.json() == []
    preview = await client.get(f"/api/v1/invitations/{token}")
    assert preview.status_code == 400


async def test_a_non_admin_cannot_revoke_an_invitation(client: AsyncClient) -> None:
    """Same guard as creating an invite: a plain member can't revoke one either."""
    workspace_id = await _owner_with_workspace(client)
    invite = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": INVITEE["email"], "role": "member"},
    )
    invitation_id, token = invite.json()["id"], invite.json()["token"]
    await client.post("/api/v1/auth/logout")
    await client.post("/api/v1/auth/signup", json=INVITEE)
    await client.post("/api/v1/auth/login", json=INVITEE)
    await client.post(f"/api/v1/invitations/{token}/accept")

    response = await client.delete(f"/api/v1/workspaces/{workspace_id}/invitations/{invitation_id}")

    assert response.status_code == 403


async def test_pending_invitations_are_visible_to_a_freshly_signed_up_account(
    client: AsyncClient,
) -> None:
    """An invite sent before an account ever existed still shows up once that email signs up —
    the "welcome, you've been invited" flow doesn't require ever following the original link."""
    workspace_id = await _owner_with_workspace(client)
    await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": INVITEE["email"], "role": "admin"},
    )
    await client.post("/api/v1/auth/logout")
    await client.post("/api/v1/auth/signup", json=INVITEE)
    await client.post("/api/v1/auth/login", json=INVITEE)

    response = await client.get("/api/v1/invitations/pending")

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["workspace_name"] == "Acme"
    assert response.json()[0]["role"] == "admin"


async def test_accepting_a_pending_invitation_by_id_needs_no_token(client: AsyncClient) -> None:
    """The welcome prompt's "accept" action works from just the invitation id and being signed in
    as the matching email — no token ever changes hands for this path."""
    workspace_id = await _owner_with_workspace(client)
    invite = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": INVITEE["email"], "role": "member"},
    )
    invitation_id = invite.json()["id"]
    await client.post("/api/v1/auth/logout")
    await client.post("/api/v1/auth/signup", json=INVITEE)
    await client.post("/api/v1/auth/login", json=INVITEE)

    response = await client.post(f"/api/v1/invitations/pending/{invitation_id}/accept")

    assert response.status_code == 200
    assert response.json()["id"] == workspace_id
    assert response.json()["role"] == "member"


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
