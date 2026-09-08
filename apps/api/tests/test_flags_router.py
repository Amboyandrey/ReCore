"""The flag HTTP surface: evaluating a workspace's flags, and the superuser-only admin CRUD."""

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User

OWNER = {"email": "owner@example.com", "password": "correct horse battery staple"}


async def _signed_up_workspace(client: AsyncClient) -> str:
    """Sign up, log in, and create a workspace — the baseline every test here starts from."""
    await client.post("/api/v1/auth/signup", json=OWNER)
    await client.post("/api/v1/auth/login", json=OWNER)
    workspace = await client.post("/api/v1/workspaces", json={"name": "Acme"})
    return str(workspace.json()["id"])


async def _make_current_user_a_superuser(db: AsyncSession) -> None:
    """Flip the signed-up-and-logged-in caller's account to superuser, directly in the database
    — there's no signup flag for this; a superuser is provisioned out of band, not self-served."""
    user = await db.scalar(select(User).where(User.email == OWNER["email"]))
    assert user is not None
    user.is_superuser = True
    await db.commit()  # this test's `db` session must commit for the client's own connection to see it


async def test_evaluate_returns_the_seeded_flags(client: AsyncClient) -> None:
    """Any workspace member can resolve the flag map — no superuser access required."""
    workspace_id = await _signed_up_workspace(client)

    response = await client.get(f"/api/v1/workspaces/{workspace_id}/flags/evaluate")

    flags = response.status_code, response.json()
    assert flags[0] == 200
    assert flags[1]["provider.anthropic"] is True
    assert flags[1]["attachments"] is False


async def test_non_superuser_cannot_reach_admin_flag_routes(client: AsyncClient) -> None:
    """A regular member gets 403 on the admin surface, workspace membership notwithstanding."""
    await _signed_up_workspace(client)

    response = await client.get("/api/v1/admin/flags")

    assert response.status_code == 403


async def test_superuser_can_create_list_and_update_a_flag(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The full admin lifecycle: define a flag, see it listed, then edit only one field of it."""
    await _signed_up_workspace(client)
    await _make_current_user_a_superuser(db)
    key = f"test.{uuid.uuid4().hex}"

    created = await client.post(
        "/api/v1/admin/flags",
        json={"key": key, "description": "A test flag.", "default_value": False},
    )
    assert created.status_code == 201
    flag_id = created.json()["id"]

    listed = await client.get("/api/v1/admin/flags")
    assert key in {f["key"] for f in listed.json()}

    updated = await client.patch(f"/api/v1/admin/flags/{flag_id}", json={"default_value": True})
    assert updated.status_code == 200
    assert updated.json()["default_value"] is True
    assert updated.json()["description"] == "A test flag."  # untouched by the partial update


async def test_superuser_can_pin_and_then_remove_a_workspace_override(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Setting an override changes what `/flags/evaluate` returns for that workspace; removing
    it falls back to the default again."""
    workspace_id = await _signed_up_workspace(client)
    await _make_current_user_a_superuser(db)
    key = f"test.{uuid.uuid4().hex}"
    flag_id = (
        await client.post(
            "/api/v1/admin/flags",
            json={"key": key, "description": "A test flag.", "default_value": False},
        )
    ).json()["id"]

    override = await client.post(
        f"/api/v1/admin/flags/{flag_id}/overrides",
        json={"scope": "workspace", "scope_id": workspace_id, "value": True},
    )
    assert override.status_code == 201
    override_id = override.json()["id"]

    evaluated = await client.get(f"/api/v1/workspaces/{workspace_id}/flags/evaluate")
    assert evaluated.json()[key] is True

    removed = await client.delete(f"/api/v1/admin/flags/{flag_id}/overrides/{override_id}")
    assert removed.status_code == 204

    evaluated_again = await client.get(f"/api/v1/workspaces/{workspace_id}/flags/evaluate")
    assert evaluated_again.json()[key] is False


async def test_creating_a_flag_with_a_taken_key_is_rejected(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The admin route surfaces the same duplicate-key rule the service enforces."""
    await _signed_up_workspace(client)
    await _make_current_user_a_superuser(db)
    key = f"test.{uuid.uuid4().hex}"
    await client.post(
        "/api/v1/admin/flags", json={"key": key, "description": "d", "default_value": False}
    )

    conflict = await client.post(
        "/api/v1/admin/flags", json={"key": key, "description": "d2", "default_value": True}
    )

    assert conflict.status_code == 409
