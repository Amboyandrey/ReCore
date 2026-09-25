"""Registering, syncing, updating, and removing a workspace's remote MCP servers.

A server's tools become ordinary `tools` rows of kind MCP, named `<server>__<tool>` so they can't
collide with each other or with a workspace's own tools. They're created disabled: one server can
offer dozens of tools, and a plain chat (no assistant) is offered every enabled tool in the
workspace, so which of them the model actually sees is an explicit choice, not a side effect of
connecting. Each row copies its server's URL and auth secret, which every sync re-applies.
"""

import asyncio
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.core.errors import McpServerNameAlreadyExists, McpServerNotFound, McpServerUnreachable
from app.core.ssrf import assert_safe_base_url
from app.models import McpServer, Tool, ToolKind, User
from app.tools.mcp_tool import auth_headers, list_remote_tools

_SYNC_TIMEOUT_SECONDS = 15.0
_MAX_TOOL_NAME = 64


def tool_name_for(server_name: str, remote_name: str) -> str:
    """The name the model sees for a server's tool — prefixed, cleaned to the tool-name rule, and
    capped at 64 characters."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", remote_name)
    return f"{server_name}__{cleaned}"[:_MAX_TOOL_NAME]


async def get_mcp_server(db: AsyncSession, *, workspace_id: uuid.UUID, server_id: uuid.UUID) -> McpServer:
    """Load one server by id, scoped to its workspace, or raise if it isn't there."""
    server = await db.scalar(
        select(McpServer).where(McpServer.id == server_id, McpServer.workspace_id == workspace_id)
    )
    if server is None:
        raise McpServerNotFound()
    return server


async def list_mcp_servers(db: AsyncSession, *, workspace_id: uuid.UUID) -> list[McpServer]:
    """List every server connected to the workspace, oldest first."""
    stmt = select(McpServer).where(McpServer.workspace_id == workspace_id).order_by(McpServer.created_at)
    return list((await db.scalars(stmt)).all())


async def create_mcp_server(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    created_by: User,
    name: str,
    url: str,
    auth_header: str | None,
    auth_value: str | None,
) -> McpServer:
    """Connect a server and import its tools. A server that can't be reached raises before
    anything is committed, so a broken registration never leaves a half-saved row behind."""
    assert_safe_base_url(url)
    taken = await db.scalar(
        select(McpServer.id).where(McpServer.workspace_id == workspace_id, McpServer.name == name)
    )
    if taken is not None:
        raise McpServerNameAlreadyExists()

    server = McpServer(
        workspace_id=workspace_id, name=name, url=url, auth_header=auth_header, created_by=created_by.id
    )
    _set_secret(server, auth_value)
    db.add(server)
    await db.flush()
    await sync_mcp_server(db, server=server)
    return server


async def update_mcp_server(
    db: AsyncSession, *, workspace_id: uuid.UUID, server_id: uuid.UUID, changes: dict[str, Any]
) -> McpServer:
    """Change a server's URL or auth (only the fields present in `changes`), then re-sync so its
    tools pick up the new connection details."""
    server = await get_mcp_server(db, workspace_id=workspace_id, server_id=server_id)
    if "url" in changes:
        assert_safe_base_url(changes["url"])
        server.url = changes["url"]
    if "auth_header" in changes:
        server.auth_header = changes["auth_header"]
    if changes.get("auth_value") is not None:
        _set_secret(server, changes["auth_value"])
    await sync_mcp_server(db, server=server)
    return server


async def delete_mcp_server(db: AsyncSession, *, workspace_id: uuid.UUID, server_id: uuid.UUID) -> None:
    """Remove a server, and with it (by cascade) every tool it contributed."""
    server = await get_mcp_server(db, workspace_id=workspace_id, server_id=server_id)
    await db.delete(server)
    await db.flush()


async def sync_mcp_server(db: AsyncSession, *, server: McpServer) -> McpServer:
    """Re-read the server's tool list: add new tools (disabled), refresh existing ones in place so
    assistant assignments survive, and disable any the server no longer offers.

    A new tool whose name is already taken by a different tool in the workspace is skipped rather
    than failing the whole sync — one clash shouldn't cost every other tool the server offers.
    """
    headers = auth_headers(
        server.auth_header, ciphertext=server.ciphertext, nonce=server.nonce, wrapped_key=server.wrapped_key
    )
    try:
        async with asyncio.timeout(_SYNC_TIMEOUT_SECONDS):
            remote_tools = await list_remote_tools(server.url, headers)
    except Exception as exc:
        raise McpServerUnreachable(f"Couldn't reach the MCP server: {exc}") from exc

    existing = {
        t.remote_name: t
        for t in (await db.scalars(select(Tool).where(Tool.mcp_server_id == server.id))).all()
    }
    taken = set(
        (
            await db.scalars(
                select(Tool.name).where(
                    Tool.workspace_id == server.workspace_id, Tool.mcp_server_id.is_distinct_from(server.id)
                )
            )
        ).all()
    )

    seen: set[str] = set()
    for remote in remote_tools:
        name = tool_name_for(server.name, remote.name)
        if remote.name in seen or name in taken:
            continue
        seen.add(remote.name)
        taken.add(name)
        tool = existing.get(remote.name)
        if tool is None:
            tool = Tool(
                workspace_id=server.workspace_id,
                kind=ToolKind.MCP,
                enabled=False,
                created_by=server.created_by,
                mcp_server_id=server.id,
                remote_name=remote.name,
            )
            db.add(tool)
        tool.name = name
        tool.description = remote.description
        tool.parameters = remote.parameters
        tool.url = server.url
        tool.secret_header = server.auth_header
        tool.ciphertext, tool.nonce, tool.wrapped_key = server.ciphertext, server.nonce, server.wrapped_key

    for remote_name, tool in existing.items():
        if remote_name not in seen:
            tool.enabled = False

    server.last_synced_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(server)
    return server


def _set_secret(server: McpServer, value: str | None) -> None:
    """Encrypt and store the server's auth value, when one is given."""
    if value is None:
        return
    secret = encrypt_secret(value)
    server.ciphertext, server.nonce, server.wrapped_key = secret.ciphertext, secret.nonce, secret.wrapped_key
