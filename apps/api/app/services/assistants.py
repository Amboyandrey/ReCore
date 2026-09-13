"""Creating, listing, updating, and removing a workspace's saved assistants.

An assistant is just a name, required instructions, an optional preferred model, and an optional
set of tools — everything a chat using one reads live at send time (see services/chat.py) rather
than has copied onto it, so editing an assistant reaches every conversation still using it.
"""

import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AssistantNotFound, ConnectorNotFound, ModelNotFound, SelfDelegation, ToolNotFound
from app.models import (
    Assistant,
    AssistantConnector,
    AssistantDelegate,
    AssistantTool,
    Connector,
    ConnectorStatus,
    LLMModel,
    Tool,
    User,
)


async def _assert_model_in_workspace(
    db: AsyncSession, *, workspace_id: uuid.UUID, model_id: uuid.UUID
) -> None:
    exists = await db.scalar(
        select(LLMModel.id).where(LLMModel.id == model_id, LLMModel.workspace_id == workspace_id)
    )
    if exists is None:
        raise ModelNotFound()


async def _assert_tools_in_workspace(
    db: AsyncSession, *, workspace_id: uuid.UUID, tool_ids: list[uuid.UUID]
) -> None:
    if not tool_ids:
        return
    stmt = select(Tool.id).where(Tool.id.in_(tool_ids), Tool.workspace_id == workspace_id)
    found = set((await db.scalars(stmt)).all())
    if set(tool_ids) - found:
        raise ToolNotFound()


async def _set_assistant_tools(
    db: AsyncSession, *, assistant_id: uuid.UUID, tool_ids: list[uuid.UUID]
) -> None:
    """Replace an assistant's entire tool assignment with `tool_ids` — always the full set, never
    an incremental add/remove, so a settings page's checkbox list can just send what's checked."""
    await db.execute(delete(AssistantTool).where(AssistantTool.assistant_id == assistant_id))
    for tool_id in tool_ids:
        db.add(AssistantTool(assistant_id=assistant_id, tool_id=tool_id))


async def _assert_delegates_in_workspace(
    db: AsyncSession, *, workspace_id: uuid.UUID, assistant_id: uuid.UUID, delegate_ids: list[uuid.UUID]
) -> None:
    """Every delegate id must be a real assistant in this workspace, and none may be
    `assistant_id` itself — an assistant can't be told to ask itself for help."""
    if not delegate_ids:
        return
    if assistant_id in delegate_ids:
        raise SelfDelegation()
    stmt = select(Assistant.id).where(Assistant.id.in_(delegate_ids), Assistant.workspace_id == workspace_id)
    found = set((await db.scalars(stmt)).all())
    if set(delegate_ids) - found:
        raise AssistantNotFound()


async def _set_assistant_delegates(
    db: AsyncSession, *, assistant_id: uuid.UUID, delegate_ids: list[uuid.UUID]
) -> None:
    """Replace an assistant's entire delegate assignment with `delegate_ids` — same full-set-
    replace shape as `_set_assistant_tools`."""
    await db.execute(delete(AssistantDelegate).where(AssistantDelegate.assistant_id == assistant_id))
    for delegate_id in delegate_ids:
        db.add(AssistantDelegate(assistant_id=assistant_id, delegate_id=delegate_id))


async def _assert_connectors_in_workspace(
    db: AsyncSession, *, workspace_id: uuid.UUID, connector_ids: list[uuid.UUID]
) -> None:
    if not connector_ids:
        return
    stmt = select(Connector.id).where(Connector.id.in_(connector_ids), Connector.workspace_id == workspace_id)
    found = set((await db.scalars(stmt)).all())
    if set(connector_ids) - found:
        raise ConnectorNotFound()


async def _set_assistant_connectors(
    db: AsyncSession, *, assistant_id: uuid.UUID, connector_ids: list[uuid.UUID]
) -> None:
    """Replace an assistant's entire connector assignment with `connector_ids` — same full-set-
    replace shape as `_set_assistant_tools`."""
    await db.execute(delete(AssistantConnector).where(AssistantConnector.assistant_id == assistant_id))
    for connector_id in connector_ids:
        db.add(AssistantConnector(assistant_id=assistant_id, connector_id=connector_id))


async def create_assistant(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    created_by: User,
    name: str,
    instructions: str,
    model_id: uuid.UUID | None,
    tool_ids: list[uuid.UUID],
    memory_enabled: bool = False,
    delegate_ids: list[uuid.UUID] | None = None,
    connector_ids: list[uuid.UUID] | None = None,
) -> Assistant:
    """Save a new assistant. `model_id` and every id in `tool_ids`/`delegate_ids`/`connector_ids`
    must already belong to this workspace — the same 404 a stale or cross-workspace id gets
    anywhere else in this codebase. `delegate_ids`/`connector_ids` default to none."""
    delegate_ids = delegate_ids or []
    connector_ids = connector_ids or []
    if model_id is not None:
        await _assert_model_in_workspace(db, workspace_id=workspace_id, model_id=model_id)
    await _assert_tools_in_workspace(db, workspace_id=workspace_id, tool_ids=tool_ids)
    await _assert_connectors_in_workspace(db, workspace_id=workspace_id, connector_ids=connector_ids)

    assistant = Assistant(
        workspace_id=workspace_id,
        name=name,
        instructions=instructions,
        model_id=model_id,
        created_by=created_by.id,
        memory_enabled=memory_enabled,
    )
    db.add(assistant)
    await db.flush()  # materializes assistant.id — needed below to reject self-delegation
    await _assert_delegates_in_workspace(
        db, workspace_id=workspace_id, assistant_id=assistant.id, delegate_ids=delegate_ids
    )
    await _set_assistant_tools(db, assistant_id=assistant.id, tool_ids=tool_ids)
    await _set_assistant_delegates(db, assistant_id=assistant.id, delegate_ids=delegate_ids)
    await _set_assistant_connectors(db, assistant_id=assistant.id, connector_ids=connector_ids)
    await db.flush()
    return assistant


async def get_assistant(db: AsyncSession, *, workspace_id: uuid.UUID, assistant_id: uuid.UUID) -> Assistant:
    """Load one assistant by id, scoped to its workspace, or raise if it isn't there."""
    assistant = await db.scalar(
        select(Assistant).where(Assistant.id == assistant_id, Assistant.workspace_id == workspace_id)
    )
    if assistant is None:
        raise AssistantNotFound()
    return assistant


async def list_assistants(db: AsyncSession, *, workspace_id: uuid.UUID) -> list[Assistant]:
    """List every assistant saved in the workspace, oldest first."""
    stmt = select(Assistant).where(Assistant.workspace_id == workspace_id).order_by(Assistant.created_at)
    return list((await db.scalars(stmt)).all())


async def list_assistant_tool_ids(db: AsyncSession, *, assistant_id: uuid.UUID) -> list[uuid.UUID]:
    """The tool ids assigned to one assistant — what AssistantOut reports back to a settings page
    so its checkboxes can show the current assignment."""
    stmt = select(AssistantTool.tool_id).where(AssistantTool.assistant_id == assistant_id)
    return list((await db.scalars(stmt)).all())


async def list_assistant_tools(db: AsyncSession, *, assistant_id: uuid.UUID) -> list[Tool]:
    """The assistant's assigned tools that are still enabled — what a chat using this assistant is
    actually offered, resolved fresh on every send rather than snapshotted onto the conversation.
    A tool disabled (or deleted, which unassigns it outright) after being assigned just drops out
    here, without needing its assignment cleaned up separately.
    """
    stmt = (
        select(Tool)
        .join(AssistantTool, AssistantTool.tool_id == Tool.id)
        .where(AssistantTool.assistant_id == assistant_id, Tool.enabled.is_(True))
    )
    return list((await db.scalars(stmt)).all())


async def list_assistant_delegate_ids(db: AsyncSession, *, assistant_id: uuid.UUID) -> list[uuid.UUID]:
    """The delegate ids assigned to one assistant — what AssistantOut reports back to a settings
    page so its checkboxes can show the current assignment."""
    stmt = select(AssistantDelegate.delegate_id).where(AssistantDelegate.assistant_id == assistant_id)
    return list((await db.scalars(stmt)).all())


async def list_assistant_delegates(db: AsyncSession, *, assistant_id: uuid.UUID) -> list[Assistant]:
    """The assistants this one may delegate a task to, in the order they were assigned — what
    chat.py offers as synthesized `ask_*` tools, resolved fresh on every send. Depth is always 1:
    a delegate's own delegates are never looked up here."""
    stmt = (
        select(Assistant)
        .join(AssistantDelegate, AssistantDelegate.delegate_id == Assistant.id)
        .where(AssistantDelegate.assistant_id == assistant_id)
        .order_by(AssistantDelegate.created_at)
    )
    return list((await db.scalars(stmt)).all())


async def list_assistant_connector_ids(db: AsyncSession, *, assistant_id: uuid.UUID) -> list[uuid.UUID]:
    """The connector ids assigned to one assistant — what AssistantOut reports back to a settings
    page so its checkboxes can show the current assignment."""
    stmt = select(AssistantConnector.connector_id).where(AssistantConnector.assistant_id == assistant_id)
    return list((await db.scalars(stmt)).all())


async def list_assistant_connectors(db: AsyncSession, *, assistant_id: uuid.UUID) -> list[Connector]:
    """The assistant's assigned connectors that are ready to search — what a chat using this
    assistant actually retrieves from, resolved fresh on every send. A connector still indexing,
    failed, or since deleted just drops out here, without its assignment needing cleanup."""
    stmt = (
        select(Connector)
        .join(AssistantConnector, AssistantConnector.connector_id == Connector.id)
        .where(AssistantConnector.assistant_id == assistant_id, Connector.status == ConnectorStatus.READY)
    )
    return list((await db.scalars(stmt)).all())


async def update_assistant(
    db: AsyncSession, *, workspace_id: uuid.UUID, assistant_id: uuid.UUID, changes: dict[str, Any]
) -> Assistant:
    """Apply only the fields present in `changes` (built with the request schema's
    `exclude_unset`) — so omitting a field leaves it untouched, but explicitly passing
    `model_id: null` or `tool_ids: []` clears it."""
    assistant = await get_assistant(db, workspace_id=workspace_id, assistant_id=assistant_id)
    if "name" in changes:
        assistant.name = changes["name"]
    if "instructions" in changes:
        assistant.instructions = changes["instructions"]
    if "model_id" in changes:
        if changes["model_id"] is not None:
            await _assert_model_in_workspace(db, workspace_id=workspace_id, model_id=changes["model_id"])
        assistant.model_id = changes["model_id"]
    if "tool_ids" in changes:
        tool_ids = changes["tool_ids"] or []
        await _assert_tools_in_workspace(db, workspace_id=workspace_id, tool_ids=tool_ids)
        await _set_assistant_tools(db, assistant_id=assistant.id, tool_ids=tool_ids)
    if "delegate_ids" in changes:
        delegate_ids = changes["delegate_ids"] or []
        await _assert_delegates_in_workspace(
            db, workspace_id=workspace_id, assistant_id=assistant.id, delegate_ids=delegate_ids
        )
        await _set_assistant_delegates(db, assistant_id=assistant.id, delegate_ids=delegate_ids)
    if "connector_ids" in changes:
        connector_ids = changes["connector_ids"] or []
        await _assert_connectors_in_workspace(db, workspace_id=workspace_id, connector_ids=connector_ids)
        await _set_assistant_connectors(db, assistant_id=assistant.id, connector_ids=connector_ids)
    if "memory_enabled" in changes:
        assistant.memory_enabled = bool(changes["memory_enabled"])
    await db.flush()
    await db.refresh(assistant)  # same onupdate=func.now() reload update_tool() needs
    return assistant


async def delete_assistant(db: AsyncSession, *, workspace_id: uuid.UUID, assistant_id: uuid.UUID) -> None:
    """Permanently remove an assistant. Conversations that used it keep going — `assistant_id`
    just goes null (ON DELETE SET NULL), the same fallback-to-plain-chat behavior an assistant
    that was merely unassigned from a conversation already has."""
    assistant = await get_assistant(db, workspace_id=workspace_id, assistant_id=assistant_id)
    await db.delete(assistant)
    await db.flush()
