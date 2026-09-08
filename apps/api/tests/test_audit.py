"""The audit trail: recording privileged actions, listing them back, and who's allowed to look."""

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, Workspace
from app.services.audit import list_audit_logs, record_audit

OWNER = {"email": "owner@example.com", "password": "correct horse battery staple"}
MEMBER = {"email": "member@example.com", "password": "correct horse battery staple"}


async def test_list_audit_logs_paginates_newest_first_by_cursor(db: AsyncSession) -> None:
    """A `before` cursor from one page's last row fetches exactly the page after it.

    Uses the committing `db` fixture, one commit per row: Postgres's `now()` is stable for the
    whole duration of a transaction, so five inserts in one uncommitted transaction (as
    `db_session` would give) would all land the same `created_at` and make DESC order a tie.
    """
    user = User(email="a@example.com", password_hash="hashed")
    db.add(user)
    await db.flush()
    workspace = Workspace(slug="acme", name="Acme", owner_id=user.id)
    db.add(workspace)
    await db.commit()
    for i in range(5):
        await record_audit(
            db,
            actor_id=user.id,
            workspace_id=workspace.id,
            action=f"test.action.{i}",
            target_type="thing",
            target_id=str(i),
            ip="127.0.0.1",
        )
        await db.commit()

    first_page = await list_audit_logs(db, workspace_id=workspace.id, limit=2, before=None)
    assert len(first_page) == 2
    assert [e.action for e in first_page] == ["test.action.4", "test.action.3"]

    second_page = await list_audit_logs(
        db, workspace_id=workspace.id, limit=2, before=first_page[-1].created_at.isoformat()
    )
    assert [e.action for e in second_page] == ["test.action.2", "test.action.1"]


async def test_list_audit_logs_never_leaks_another_workspace_s_rows(db_session: AsyncSession) -> None:
    """A workspace's audit view is a plain filter — another workspace's rows never appear in it."""
    user = User(email="a@example.com", password_hash="hashed")
    db_session.add(user)
    await db_session.flush()
    workspace_a = Workspace(slug="acme", name="Acme", owner_id=user.id)
    workspace_b = Workspace(slug="beta", name="Beta", owner_id=user.id)
    db_session.add_all([workspace_a, workspace_b])
    await db_session.flush()
    await record_audit(
        db_session,
        actor_id=user.id,
        workspace_id=workspace_a.id,
        action="workspace.created",
        target_type="workspace",
        target_id=str(workspace_a.id),
        ip="127.0.0.1",
    )

    logs = await list_audit_logs(db_session, workspace_id=workspace_b.id, limit=50, before=None)

    assert logs == []


async def test_creating_a_workspace_writes_an_audit_row(client: AsyncClient) -> None:
    """The very first privileged action a user takes — creating a workspace — is itself audited."""
    await client.post("/api/v1/auth/signup", json=OWNER)
    await client.post("/api/v1/auth/login", json=OWNER)

    workspace_id = (await client.post("/api/v1/workspaces", json={"name": "Acme"})).json()["id"]

    logs = (await client.get(f"/api/v1/workspaces/{workspace_id}/audit-logs")).json()
    assert any(entry["action"] == "workspace.created" for entry in logs)


async def test_changing_a_member_s_role_writes_an_audit_row(client: AsyncClient) -> None:
    """Promoting a member shows up in the trail with the role it was changed to."""
    await client.post("/api/v1/auth/signup", json=OWNER)
    await client.post("/api/v1/auth/login", json=OWNER)
    workspace_id = (await client.post("/api/v1/workspaces", json={"name": "Acme"})).json()["id"]
    invite = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": MEMBER["email"], "role": "member"},
    )
    token = invite.json()["token"]
    await client.post("/api/v1/auth/logout")
    await client.post("/api/v1/auth/signup", json=MEMBER)
    await client.post("/api/v1/auth/login", json=MEMBER)
    await client.post(f"/api/v1/invitations/{token}/accept")
    member_id = (await client.get("/api/v1/auth/me")).json()["id"]
    await client.post("/api/v1/auth/logout")
    await client.post("/api/v1/auth/login", json=OWNER)

    await client.patch(f"/api/v1/workspaces/{workspace_id}/members/{member_id}", json={"role": "admin"})

    logs = (await client.get(f"/api/v1/workspaces/{workspace_id}/audit-logs")).json()
    entry = next(e for e in logs if e["action"] == "member.role_changed")
    assert entry["target_id"] == member_id
    assert entry["event_metadata"] == {"new_role": "admin"}


async def test_a_failed_credential_validation_is_audited_even_though_the_request_fails(
    client: AsyncClient,
) -> None:
    """A rejected key still leaves a trail — 'every touch is audited' includes failures."""
    await client.post("/api/v1/auth/signup", json=OWNER)
    await client.post("/api/v1/auth/login", json=OWNER)
    workspace_id = (await client.post("/api/v1/workspaces", json={"name": "Acme"})).json()["id"]

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/credentials",
        json={"provider": "anthropic", "label": "Bad", "api_key": "definitely-not-a-real-key"},
    )
    assert response.status_code == 422

    logs = (await client.get(f"/api/v1/workspaces/{workspace_id}/audit-logs")).json()
    assert any(entry["action"] == "credential.validation_failed" for entry in logs)


async def test_non_owner_cannot_view_the_audit_log(client: AsyncClient) -> None:
    """The audit viewer is owner-only, per ARCHITECTURE.md's route table — an admin isn't enough."""
    await client.post("/api/v1/auth/signup", json=OWNER)
    await client.post("/api/v1/auth/login", json=OWNER)
    workspace_id = (await client.post("/api/v1/workspaces", json={"name": "Acme"})).json()["id"]
    invite = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations",
        json={"email": MEMBER["email"], "role": "admin"},
    )
    token = invite.json()["token"]
    await client.post("/api/v1/auth/logout")
    await client.post("/api/v1/auth/signup", json=MEMBER)
    await client.post("/api/v1/auth/login", json=MEMBER)
    await client.post(f"/api/v1/invitations/{token}/accept")

    response = await client.get(f"/api/v1/workspaces/{workspace_id}/audit-logs")

    assert response.status_code == 403


async def test_audit_log_ids_are_always_valid_uuids(db_session: AsyncSession) -> None:
    """Sanity check on record_audit's shape — target_id is a free-form string, not just a UUID."""
    user = User(email="a@example.com", password_hash="hashed")
    db_session.add(user)
    await db_session.flush()
    entry = await record_audit(
        db_session,
        actor_id=user.id,
        workspace_id=None,
        action="flag.created",
        target_type="flag",
        target_id="anthropic",  # a provider name, not a uuid — target_id is deliberately a string
        ip="127.0.0.1",
    )
    assert isinstance(entry.id, uuid.UUID)
