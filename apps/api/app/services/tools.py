"""Registering, listing, updating, and removing a workspace's tools.

The built-in web search is a row in the same `tools` table as everything else — enabling it the
first time creates that row; enabling it again (a key rotation) updates it in place, the same
"re-enable updates in place" shape `enable_model()` already uses for LLM models. A third-party
HTTP tool is a row too, just one an admin (any member, actually — see the router) fills in
themselves rather than one this app ships with a fixed definition for.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.core.errors import ToolNameAlreadyExists, ToolNotFound
from app.core.ssrf import assert_safe_base_url
from app.models import Message, Tool, ToolInvocation, ToolKind, User
from app.providers.base import ToolDefinition
from app.tools import web_search


def to_tool_definition(tool: Tool) -> ToolDefinition:
    """What the agent loop offers the provider — a tool's own row already has this shape."""
    return ToolDefinition(name=tool.name, description=tool.description, parameters=tool.parameters)


async def _assert_name_available(
    db: AsyncSession, *, workspace_id: uuid.UUID, name: str, excluding_tool_id: uuid.UUID | None = None
) -> None:
    """Raise if another tool in this workspace already uses `name` — the model sees this name as
    the function it's calling, so two tools can't share one. Checked here (rather than left to
    the table's own unique constraint) so a name conflict is a clean 409, not a raw DB error."""
    stmt = select(Tool.id).where(Tool.workspace_id == workspace_id, Tool.name == name)
    if excluding_tool_id is not None:
        stmt = stmt.where(Tool.id != excluding_tool_id)
    if await db.scalar(stmt) is not None:
        raise ToolNameAlreadyExists()


async def enable_web_search(
    db: AsyncSession, *, workspace_id: uuid.UUID, created_by: User, api_key: str
) -> Tool:
    """Turn on the built-in web search tool, creating it the first time or rotating its stored
    Tavily key on a later call."""
    secret = encrypt_secret(api_key)
    existing = await db.scalar(
        select(Tool).where(Tool.workspace_id == workspace_id, Tool.name == web_search.NAME)
    )
    if existing is not None:
        existing.enabled = True
        existing.ciphertext = secret.ciphertext
        existing.nonce = secret.nonce
        existing.wrapped_key = secret.wrapped_key
        await db.flush()
        return existing
    tool = Tool(
        workspace_id=workspace_id,
        name=web_search.NAME,
        description=web_search.DESCRIPTION,
        parameters=web_search.PARAMETERS,
        kind=ToolKind.BUILTIN,
        enabled=True,
        created_by=created_by.id,
        ciphertext=secret.ciphertext,
        nonce=secret.nonce,
        wrapped_key=secret.wrapped_key,
    )
    db.add(tool)
    await db.flush()
    return tool


async def create_http_tool(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    created_by: User,
    name: str,
    description: str,
    parameters: dict[str, Any],
    method: str,
    url: str,
    secret_header: str | None,
    secret_value: str | None,
) -> Tool:
    """Register a third-party HTTP tool. The URL is checked against the SSRF guard now, at save
    time — and again immediately before every call (see app/tools/http_tool.py), since a DNS
    answer can change in between; see app/core/ssrf.py's own docstring for why that gap can't be
    closed here alone."""
    assert_safe_base_url(url)
    await _assert_name_available(db, workspace_id=workspace_id, name=name)

    ciphertext = nonce = wrapped_key = None
    if secret_value is not None:
        secret = encrypt_secret(secret_value)
        ciphertext, nonce, wrapped_key = secret.ciphertext, secret.nonce, secret.wrapped_key

    tool = Tool(
        workspace_id=workspace_id,
        name=name,
        description=description,
        parameters=parameters,
        kind=ToolKind.HTTP,
        enabled=True,
        created_by=created_by.id,
        method=method,
        url=url,
        secret_header=secret_header,
        ciphertext=ciphertext,
        nonce=nonce,
        wrapped_key=wrapped_key,
    )
    db.add(tool)
    await db.flush()
    return tool


async def get_tool(db: AsyncSession, *, workspace_id: uuid.UUID, tool_id: uuid.UUID) -> Tool:
    """Load one tool by id, scoped to its workspace, or raise if it isn't there."""
    tool = await db.scalar(
        select(Tool).where(Tool.id == tool_id, Tool.workspace_id == workspace_id)
    )
    if tool is None:
        raise ToolNotFound()
    return tool


async def update_tool(
    db: AsyncSession, *, workspace_id: uuid.UUID, tool_id: uuid.UUID, changes: dict[str, Any]
) -> Tool:
    """Apply only the fields present in `changes` (built with the request schema's
    `exclude_unset`) — so omitting a field leaves it untouched.

    `enabled` applies to any tool, built-in or HTTP — the lightweight on/off switch that keeps a
    tool's configuration (and secret) around for later, the same role `disable_model()` plays for
    LLM models. Every other field is HTTP-tool-only; a URL change is re-checked against the SSRF
    guard exactly like a brand-new one is.
    """
    tool = await get_tool(db, workspace_id=workspace_id, tool_id=tool_id)
    if "name" in changes and changes["name"] != tool.name:
        await _assert_name_available(
            db, workspace_id=workspace_id, name=changes["name"], excluding_tool_id=tool.id
        )
        tool.name = changes["name"]
    if "description" in changes:
        tool.description = changes["description"]
    if "parameters" in changes:
        tool.parameters = changes["parameters"]
    if "method" in changes:
        tool.method = changes["method"]
    if "url" in changes:
        assert_safe_base_url(changes["url"])
        tool.url = changes["url"]
    if "secret_header" in changes:
        tool.secret_header = changes["secret_header"]
    if "secret_value" in changes and changes["secret_value"] is not None:
        secret = encrypt_secret(changes["secret_value"])
        tool.ciphertext, tool.nonce, tool.wrapped_key = secret.ciphertext, secret.nonce, secret.wrapped_key
    if "enabled" in changes:
        tool.enabled = changes["enabled"]
    await db.flush()
    await db.refresh(tool)  # same onupdate=func.now() reload update_conversation() needs
    return tool


async def delete_tool(db: AsyncSession, *, workspace_id: uuid.UUID, tool_id: uuid.UUID) -> None:
    """Permanently remove a tool — unlike disabling (see update_tool), there's no way back short
    of registering it again from scratch. Past tool_invocations that named it keep their own
    denormalized `name`, so history stays legible; only their `tool_id` reference goes null (see
    that table's ON DELETE SET NULL foreign key)."""
    tool = await get_tool(db, workspace_id=workspace_id, tool_id=tool_id)
    await db.delete(tool)
    await db.flush()


async def list_tools(db: AsyncSession, *, workspace_id: uuid.UUID) -> list[Tool]:
    """List every tool registered in the workspace, oldest first."""
    stmt = select(Tool).where(Tool.workspace_id == workspace_id).order_by(Tool.created_at)
    return list((await db.scalars(stmt)).all())


async def list_enabled_tools(db: AsyncSession, *, workspace_id: uuid.UUID) -> list[Tool]:
    """List the tools a chat in this workspace is actually offered — what the agent loop calls."""
    stmt = select(Tool).where(Tool.workspace_id == workspace_id, Tool.enabled.is_(True))
    return list((await db.scalars(stmt)).all())


async def list_tool_invocations(db: AsyncSession, *, conversation_id: uuid.UUID) -> list[ToolInvocation]:
    """List every tool call made in a conversation, oldest first.

    Joined through messages since a ToolInvocation only carries the message_id it was made
    for, not a conversation_id of its own — same reach-through-the-parent shape messages' own
    row-level-security policy already uses to get from a message to its workspace.
    """
    stmt = (
        select(ToolInvocation)
        .join(Message, Message.id == ToolInvocation.message_id)
        .where(Message.conversation_id == conversation_id)
        .order_by(ToolInvocation.created_at)
    )
    return list((await db.scalars(stmt)).all())
