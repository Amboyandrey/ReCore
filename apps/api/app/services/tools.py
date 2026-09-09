"""Registering, listing, and enabling/disabling a workspace's tools.

The built-in web search is a row in the same `tools` table as everything else — enabling it the
first time creates that row; enabling it again (a key rotation) updates it in place, the same
"re-enable updates in place" shape `enable_model()` already uses for LLM models.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.core.errors import ToolNotFound
from app.models import Message, Tool, ToolInvocation, ToolKind, User
from app.providers.base import ToolDefinition
from app.tools import web_search


def to_tool_definition(tool: Tool) -> ToolDefinition:
    """What the agent loop offers the provider — a tool's own row already has this shape."""
    return ToolDefinition(name=tool.name, description=tool.description, parameters=tool.parameters)


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


async def get_tool(db: AsyncSession, *, workspace_id: uuid.UUID, tool_id: uuid.UUID) -> Tool:
    """Load one tool by id, scoped to its workspace, or raise if it isn't there."""
    tool = await db.scalar(
        select(Tool).where(Tool.id == tool_id, Tool.workspace_id == workspace_id)
    )
    if tool is None:
        raise ToolNotFound()
    return tool


async def disable_tool(db: AsyncSession, *, workspace_id: uuid.UUID, tool_id: uuid.UUID) -> None:
    """Turn a tool off — its row (and any secret it holds) is kept, same as disabling a model
    keeps its pricing, so re-enabling it later doesn't mean reconfiguring it from scratch."""
    tool = await get_tool(db, workspace_id=workspace_id, tool_id=tool_id)
    tool.enabled = False
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
