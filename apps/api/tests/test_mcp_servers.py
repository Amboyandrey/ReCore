"""Connecting, syncing, updating, and removing a workspace's MCP servers — gated by the `tools`
flag, same as test_tools.py — with the remote tool list faked at the service boundary, plus one
end-to-end route test against an in-process MCP server."""

import uuid

import pytest
from httpx import AsyncClient
from mcp.server.mcpserver import MCPServer
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import EncryptedSecret, decrypt_secret
from app.core.errors import (
    McpServerNameAlreadyExists,
    McpServerUnreachable,
    McpToolReadOnly,
)
from app.core.ssrf import UnsafeBaseUrlError
from app.models import AuditLog, FeatureFlag, FlagScope, McpServer, Tool, ToolKind, User, Workspace
from app.services import mcp_servers
from app.services.flags import set_override
from app.services.mcp_servers import (
    create_mcp_server,
    delete_mcp_server,
    sync_mcp_server,
    tool_name_for,
    update_mcp_server,
)
from app.services.tools import create_http_tool, update_tool
from app.tools import mcp_tool
from app.tools.mcp_tool import RemoteTool

URL = "https://example.com/mcp"
OWNER = {"email": "owner@example.com", "password": "correct horse battery staple"}


def _remote(*names: str) -> list[RemoteTool]:
    return [RemoteTool(name=n, description=f"Does {n}.", parameters={"type": "object"}) for n in names]


def _serve(monkeypatch: pytest.MonkeyPatch, tools: list[RemoteTool]) -> None:
    """Make every sync see exactly `tools` as the server's current list."""

    async def fake(url: str, headers: dict[str, str]) -> list[RemoteTool]:
        return tools

    monkeypatch.setattr(mcp_servers, "list_remote_tools", fake)


async def _user_with_workspace(db: AsyncSession, *, email: str, slug: str) -> tuple[User, Workspace]:
    user = User(email=email, password_hash="hashed")
    db.add(user)
    await db.flush()
    workspace = Workspace(slug=slug, name=slug, owner_id=user.id)
    db.add(workspace)
    await db.flush()
    return user, workspace


async def _connect(
    db: AsyncSession, user: User, workspace: Workspace, *, name: str = "docs", auth_value: str | None = None
) -> McpServer:
    """Connect a server with the given name, and an `Authorization` header when a value is given."""
    return await create_mcp_server(
        db,
        workspace_id=workspace.id,
        created_by=user,
        name=name,
        url=URL,
        auth_header="Authorization" if auth_value else None,
        auth_value=auth_value,
    )


async def _server_tools(db: AsyncSession, server: McpServer) -> dict[str, Tool]:
    rows = (await db.scalars(select(Tool).where(Tool.mcp_server_id == server.id))).all()
    return {t.remote_name or "": t for t in rows}


def test_tool_name_for_prefixes_cleans_and_caps() -> None:
    assert tool_name_for("gh", "create_issue") == "gh__create_issue"
    assert tool_name_for("gh", "repo.search files") == "gh__repo_search_files"
    assert len(tool_name_for("gh", "x" * 100)) == 64


async def test_create_imports_the_server_s_tools_disabled_with_its_secret(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _remote("search", "fetch"))
    user, workspace = await _user_with_workspace(db, email="mcp1@example.com", slug="mcp-ws1")

    server = await create_mcp_server(
        db,
        workspace_id=workspace.id,
        created_by=user,
        name="docs",
        url=URL,
        auth_header="Authorization",
        auth_value="Bearer abc",
    )

    tools = await _server_tools(db, server)
    assert set(tools) == {"search", "fetch"}
    search = tools["search"]
    assert search.name == "docs__search"
    assert search.kind == ToolKind.MCP
    assert search.enabled is False
    assert search.url == URL
    assert search.secret_header == "Authorization"
    assert search.ciphertext is not None and search.nonce is not None and search.wrapped_key is not None
    secret = EncryptedSecret(ciphertext=search.ciphertext, nonce=search.nonce, wrapped_key=search.wrapped_key)
    assert decrypt_secret(secret) == "Bearer abc"
    assert server.last_synced_at is not None


async def test_create_rejects_a_private_url(db: AsyncSession) -> None:
    user, workspace = await _user_with_workspace(db, email="mcp2@example.com", slug="mcp-ws2")

    with pytest.raises(UnsafeBaseUrlError):
        await create_mcp_server(
            db,
            workspace_id=workspace.id,
            created_by=user,
            name="internal",
            url="http://127.0.0.1:8000/mcp",
            auth_header=None,
            auth_value=None,
        )


async def test_create_rejects_a_name_already_taken(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, [])
    user, workspace = await _user_with_workspace(db, email="mcp3@example.com", slug="mcp-ws3")
    await _connect(db, user, workspace)

    with pytest.raises(McpServerNameAlreadyExists):
        await _connect(db, user, workspace)


async def test_create_reports_an_unreachable_server(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(url: str, headers: dict[str, str]) -> list[RemoteTool]:
        raise ConnectionError("connection refused")

    monkeypatch.setattr(mcp_servers, "list_remote_tools", refuse)
    user, workspace = await _user_with_workspace(db, email="mcp4@example.com", slug="mcp-ws4")

    with pytest.raises(McpServerUnreachable, match="connection refused"):
        await _connect(db, user, workspace, name="down")


async def test_sync_refreshes_in_place_adds_new_and_disables_removed(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _remote("search", "fetch"))
    user, workspace = await _user_with_workspace(db, email="mcp5@example.com", slug="mcp-ws5")
    server = await _connect(db, user, workspace)
    before = await _server_tools(db, server)
    before["search"].enabled = True
    before["fetch"].enabled = True
    await db.flush()

    refreshed = RemoteTool(name="search", description="Better search.", parameters={})
    _serve(monkeypatch, [refreshed, *_remote("list")])
    await sync_mcp_server(db, server=server)

    after = await _server_tools(db, server)
    assert after["search"].id == before["search"].id
    assert after["search"].enabled is True
    assert after["search"].description == "Better search."
    assert after["fetch"].enabled is False
    assert after["list"].enabled is False


async def test_sync_skips_a_tool_whose_name_another_tool_already_has(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, workspace = await _user_with_workspace(db, email="mcp6@example.com", slug="mcp-ws6")
    await create_http_tool(
        db,
        workspace_id=workspace.id,
        created_by=user,
        name="docs__search",
        description="",
        parameters={},
        method="GET",
        url="https://example.com/search",
        secret_header=None,
        secret_value=None,
    )
    _serve(monkeypatch, _remote("search", "fetch"))

    server = await _connect(db, user, workspace)

    assert set(await _server_tools(db, server)) == {"fetch"}


async def test_update_rotates_the_secret_onto_every_tool(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _remote("search"))
    user, workspace = await _user_with_workspace(db, email="mcp7@example.com", slug="mcp-ws7")
    server = await _connect(db, user, workspace, auth_value="old")

    await update_mcp_server(
        db, workspace_id=workspace.id, server_id=server.id, changes={"auth_value": "new"}
    )

    tool = (await _server_tools(db, server))["search"]
    assert tool.ciphertext is not None and tool.nonce is not None and tool.wrapped_key is not None
    secret = EncryptedSecret(ciphertext=tool.ciphertext, nonce=tool.nonce, wrapped_key=tool.wrapped_key)
    assert decrypt_secret(secret) == "new"


async def test_an_mcp_tool_can_only_be_enabled_or_disabled(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _remote("search"))
    user, workspace = await _user_with_workspace(db, email="mcp8@example.com", slug="mcp-ws8")
    server = await _connect(db, user, workspace)
    tool = (await _server_tools(db, server))["search"]

    updated = await update_tool(db, workspace_id=workspace.id, tool_id=tool.id, changes={"enabled": True})
    assert updated.enabled is True
    with pytest.raises(McpToolReadOnly):
        await update_tool(db, workspace_id=workspace.id, tool_id=tool.id, changes={"url": "https://example.org"})


async def test_delete_removes_the_server_and_its_tools(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _remote("search"))
    user, workspace = await _user_with_workspace(db, email="mcp9@example.com", slug="mcp-ws9")
    server = await _connect(db, user, workspace)
    server_id = server.id

    await delete_mcp_server(db, workspace_id=workspace.id, server_id=server_id)
    db.expunge_all()

    assert await db.scalar(select(Tool).where(Tool.mcp_server_id == server_id)) is None


# ---------- Router ----------


async def _owner_with_workspace(client: AsyncClient) -> str:
    await client.post("/api/v1/auth/signup", json=OWNER)
    await client.post("/api/v1/auth/login", json=OWNER)
    created = await client.post("/api/v1/workspaces", json={"name": "Acme"})
    return str(created.json()["id"])


async def _enable_tools_flag(db: AsyncSession, redis: Redis, *, workspace_id: str) -> None:
    flag = await db.scalar(select(FeatureFlag).where(FeatureFlag.key == "tools"))
    assert flag is not None
    await set_override(
        db, redis, flag_id=flag.id, scope=FlagScope.WORKSPACE, scope_id=uuid.UUID(workspace_id), value=True
    )
    await db.commit()


async def test_mcp_server_routes_404_while_the_flag_is_off(client: AsyncClient) -> None:
    workspace_id = await _owner_with_workspace(client)

    response = await client.get(f"/api/v1/workspaces/{workspace_id}/mcp-servers")

    assert response.status_code == 404


async def test_create_requires_an_auth_header_and_value_together(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    workspace_id = await _owner_with_workspace(client)
    await _enable_tools_flag(db, redis_client, workspace_id=workspace_id)

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/mcp-servers",
        json={"name": "docs", "url": URL, "auth_header": "Authorization"},
    )

    assert response.status_code == 422


async def test_connecting_a_server_end_to_end(
    client: AsyncClient, db: AsyncSession, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Against a real (in-process) MCP server: connect, see its tools, enable one, sync, delete —
    with the secret never returned and every step audited."""
    server = MCPServer("e2e")

    @server.tool()
    def echo(text: str) -> str:
        """Echo the text back."""
        return text

    monkeypatch.setattr(mcp_tool, "_server_override", server)
    workspace_id = await _owner_with_workspace(client)
    await _enable_tools_flag(db, redis_client, workspace_id=workspace_id)
    base = f"/api/v1/workspaces/{workspace_id}"

    created = await client.post(
        f"{base}/mcp-servers",
        json={"name": "e2e", "url": URL, "auth_header": "Authorization", "auth_value": "Bearer s3cret"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["has_secret"] is True
    assert "s3cret" not in created.text
    server_id = body["id"]

    tools = (await client.get(f"{base}/tools")).json()
    assert [(t["name"], t["kind"], t["enabled"], t["mcp_server_id"]) for t in tools] == [
        ("e2e__echo", "mcp", False, server_id)
    ]
    enabled = await client.patch(f"{base}/tools/{tools[0]['id']}", json={"enabled": True})
    assert enabled.json()["enabled"] is True
    assert (await client.patch(f"{base}/tools/{tools[0]['id']}", json={"name": "renamed"})).status_code == 409

    assert (await client.post(f"{base}/mcp-servers/{server_id}/sync")).status_code == 200
    assert (await client.delete(f"{base}/mcp-servers/{server_id}")).status_code == 204
    assert (await client.get(f"{base}/tools")).json() == []

    actions = (await db.scalars(select(AuditLog.action).where(AuditLog.target_type == "mcp_server"))).all()
    assert sorted(actions) == ["mcp_server.created", "mcp_server.deleted", "mcp_server.synced"]
