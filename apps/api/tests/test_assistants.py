"""Creating, listing, updating, and removing a workspace's saved assistants — open to any member,
same floor conversations and tools already use, no feature flag involved.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AssistantNotFound, ModelNotFound, SelfDelegation, ToolNotFound
from app.models import (
    Assistant,
    AuditLog,
    Conversation,
    LLMModel,
    Provider,
    ProviderCredential,
    Tool,
    ToolKind,
    User,
    Workspace,
)
from app.services.assistants import (
    create_assistant,
    delete_assistant,
    get_assistant,
    list_assistant_delegate_ids,
    list_assistant_delegates,
    list_assistant_tool_ids,
    list_assistant_tools,
    list_assistants,
    update_assistant,
)

OWNER = {"email": "owner@example.com", "password": "correct horse battery staple"}


async def _owner_with_workspace(client: AsyncClient) -> str:
    """Sign up and log in as the owner, create a workspace, and return its id."""
    await client.post("/api/v1/auth/signup", json=OWNER)
    await client.post("/api/v1/auth/login", json=OWNER)
    created = await client.post("/api/v1/workspaces", json={"name": "Acme"})
    return str(created.json()["id"])


async def _user_with_workspace(db: AsyncSession, *, email: str, slug: str) -> tuple[User, Workspace]:
    user = User(email=email, password_hash="hashed")
    db.add(user)
    await db.flush()
    workspace = Workspace(slug=slug, name=slug, owner_id=user.id)
    db.add(workspace)
    await db.flush()
    return user, workspace


async def _model_in_workspace(
    db: AsyncSession, *, workspace_id: uuid.UUID, created_by: uuid.UUID
) -> LLMModel:
    credential = ProviderCredential(
        workspace_id=workspace_id,
        provider=Provider.ANTHROPIC,
        label="Prod",
        ciphertext=b"\x01",
        nonce=b"\x02" * 12,
        wrapped_key=b"\x03" * 44,
        last4="aa11",
        created_by=created_by,
    )
    db.add(credential)
    await db.flush()
    model = LLMModel(
        workspace_id=workspace_id,
        credential_id=credential.id,
        provider_model_id="fake-small",
        display_name="Fake Small",
    )
    db.add(model)
    await db.flush()
    return model


async def _tool_in_workspace(
    db: AsyncSession, *, workspace_id: uuid.UUID, created_by: uuid.UUID, name: str
) -> Tool:
    tool = Tool(
        workspace_id=workspace_id,
        name=name,
        description="A tool.",
        parameters={"type": "object"},
        kind=ToolKind.BUILTIN,
        enabled=True,
        created_by=created_by,
    )
    db.add(tool)
    await db.flush()
    return tool


# ---------- Router ----------


async def test_creating_an_assistant_requires_instructions(client: AsyncClient) -> None:
    workspace_id = await _owner_with_workspace(client)

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/assistants",
        json={"name": "Support bot", "instructions": ""},
    )

    assert response.status_code == 422


async def test_creating_an_assistant_needs_no_model_or_tools(client: AsyncClient) -> None:
    workspace_id = await _owner_with_workspace(client)

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/assistants",
        json={"name": "Support bot", "instructions": "Be helpful and concise."},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Support bot"
    assert body["instructions"] == "Be helpful and concise."
    assert body["model_id"] is None
    assert body["tool_ids"] == []
    assert body["delegate_ids"] == []


async def test_creating_an_assistant_with_a_model_and_tools(
    client: AsyncClient, db: AsyncSession
) -> None:
    workspace_id = await _owner_with_workspace(client)
    me = await db.scalar(select(User).where(User.email == OWNER["email"]))
    assert me is not None
    model = await _model_in_workspace(db, workspace_id=uuid.UUID(workspace_id), created_by=me.id)
    tool = await _tool_in_workspace(
        db, workspace_id=uuid.UUID(workspace_id), created_by=me.id, name="web_search"
    )
    await db.commit()

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/assistants",
        json={
            "name": "Research bot",
            "instructions": "Search the web before answering.",
            "model_id": str(model.id),
            "tool_ids": [str(tool.id)],
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["model_id"] == str(model.id)
    assert body["tool_ids"] == [str(tool.id)]


async def test_creating_an_assistant_rejects_a_model_from_another_workspace(
    client: AsyncClient, db: AsyncSession
) -> None:
    workspace_id = await _owner_with_workspace(client)
    other_user, other_workspace = await _user_with_workspace(db, email="other@example.com", slug="other-ws")
    other_model = await _model_in_workspace(db, workspace_id=other_workspace.id, created_by=other_user.id)
    await db.commit()

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/assistants",
        json={"name": "Bot", "instructions": "x", "model_id": str(other_model.id)},
    )

    assert response.status_code == 404


async def test_creating_an_assistant_that_delegates_to_another(
    client: AsyncClient, db: AsyncSession
) -> None:
    """delegate_ids round-trips through the API — the create body, and back out in AssistantOut."""
    workspace_id = await _owner_with_workspace(client)
    researcher = await client.post(
        f"/api/v1/workspaces/{workspace_id}/assistants",
        json={"name": "Researcher", "instructions": "Research things."},
    )
    assert researcher.status_code == 201
    researcher_id = researcher.json()["id"]

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/assistants",
        json={
            "name": "Writer",
            "instructions": "Write things.",
            "delegate_ids": [researcher_id],
        },
    )

    assert response.status_code == 201
    assert response.json()["delegate_ids"] == [researcher_id]


async def test_creating_an_assistant_rejects_a_tool_from_another_workspace(
    client: AsyncClient, db: AsyncSession
) -> None:
    workspace_id = await _owner_with_workspace(client)
    other_user, other_workspace = await _user_with_workspace(db, email="other2@example.com", slug="other-ws2")
    other_tool = await _tool_in_workspace(
        db, workspace_id=other_workspace.id, created_by=other_user.id, name="get_weather"
    )
    await db.commit()

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/assistants",
        json={"name": "Bot", "instructions": "x", "tool_ids": [str(other_tool.id)]},
    )

    assert response.status_code == 404


async def test_listing_assistants(client: AsyncClient) -> None:
    workspace_id = await _owner_with_workspace(client)
    await client.post(
        f"/api/v1/workspaces/{workspace_id}/assistants", json={"name": "A", "instructions": "x"}
    )
    await client.post(
        f"/api/v1/workspaces/{workspace_id}/assistants", json={"name": "B", "instructions": "y"}
    )

    response = await client.get(f"/api/v1/workspaces/{workspace_id}/assistants")

    assert response.status_code == 200
    assert [a["name"] for a in response.json()] == ["A", "B"]


async def test_updating_an_assistant_edits_only_the_fields_sent(client: AsyncClient) -> None:
    workspace_id = await _owner_with_workspace(client)
    created = await client.post(
        f"/api/v1/workspaces/{workspace_id}/assistants",
        json={"name": "Bot", "instructions": "Old instructions."},
    )
    assistant_id = created.json()["id"]

    response = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/assistants/{assistant_id}",
        json={"instructions": "New instructions."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Bot"  # untouched
    assert body["instructions"] == "New instructions."


async def test_updating_an_assistant_can_explicitly_clear_its_model_and_tools(
    client: AsyncClient, db: AsyncSession
) -> None:
    workspace_id = await _owner_with_workspace(client)
    me = await db.scalar(select(User).where(User.email == OWNER["email"]))
    assert me is not None
    model = await _model_in_workspace(db, workspace_id=uuid.UUID(workspace_id), created_by=me.id)
    tool = await _tool_in_workspace(
        db, workspace_id=uuid.UUID(workspace_id), created_by=me.id, name="web_search"
    )
    await db.commit()
    created = await client.post(
        f"/api/v1/workspaces/{workspace_id}/assistants",
        json={
            "name": "Bot",
            "instructions": "x",
            "model_id": str(model.id),
            "tool_ids": [str(tool.id)],
        },
    )
    assistant_id = created.json()["id"]

    response = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/assistants/{assistant_id}",
        json={"model_id": None, "tool_ids": []},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["model_id"] is None
    assert body["tool_ids"] == []


async def test_creating_updating_and_deleting_an_assistant_are_audited(
    client: AsyncClient, db: AsyncSession
) -> None:
    workspace_id = await _owner_with_workspace(client)
    created = await client.post(
        f"/api/v1/workspaces/{workspace_id}/assistants", json={"name": "Bot", "instructions": "x"}
    )
    assistant_id = created.json()["id"]
    await client.patch(
        f"/api/v1/workspaces/{workspace_id}/assistants/{assistant_id}", json={"name": "Renamed"}
    )
    await client.delete(f"/api/v1/workspaces/{workspace_id}/assistants/{assistant_id}")

    actions = (
        await db.scalars(
            select(AuditLog.action).where(AuditLog.workspace_id == uuid.UUID(workspace_id))
        )
    ).all()
    assert "assistant.created" in actions
    assert "assistant.updated" in actions
    assert "assistant.deleted" in actions


async def test_deleting_an_assistant_falls_a_conversation_back_to_plain_chat(
    client: AsyncClient, db: AsyncSession
) -> None:
    """A conversation that used a since-deleted assistant keeps working — its assistant_id just
    goes null (ON DELETE SET NULL), not a dangling reference."""
    workspace_id = await _owner_with_workspace(client)
    me = await db.scalar(select(User).where(User.email == OWNER["email"]))
    assert me is not None
    model = await _model_in_workspace(db, workspace_id=uuid.UUID(workspace_id), created_by=me.id)
    await db.commit()
    created = await client.post(
        f"/api/v1/workspaces/{workspace_id}/assistants", json={"name": "Bot", "instructions": "x"}
    )
    assistant_id = created.json()["id"]
    conversation_resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations",
        json={"model_id": str(model.id), "assistant_id": assistant_id},
    )
    conversation_id = conversation_resp.json()["id"]
    assert conversation_resp.json()["assistant_id"] == assistant_id

    delete_resp = await client.delete(f"/api/v1/workspaces/{workspace_id}/assistants/{assistant_id}")
    assert delete_resp.status_code == 204

    fetched = await client.get(f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}")
    assert fetched.status_code == 200
    assert fetched.json()["assistant_id"] is None


# ---------- Service ----------


async def test_get_assistant_rejects_one_from_another_workspace(db: AsyncSession) -> None:
    user, workspace = await _user_with_workspace(db, email="svc1@example.com", slug="svc-ws1")
    assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Bot", instructions="x",
        model_id=None, tool_ids=[],
    )
    await db.commit()
    other_workspace = Workspace(slug="svc-other1", name="Other", owner_id=user.id)
    db.add(other_workspace)
    await db.flush()

    with pytest.raises(AssistantNotFound):
        await get_assistant(db, workspace_id=other_workspace.id, assistant_id=assistant.id)


async def test_create_assistant_rejects_a_model_from_another_workspace(db: AsyncSession) -> None:
    user, workspace = await _user_with_workspace(db, email="svc2@example.com", slug="svc-ws2")
    other_user, other_workspace = await _user_with_workspace(db, email="svc2b@example.com", slug="svc-ws2b")
    other_model = await _model_in_workspace(db, workspace_id=other_workspace.id, created_by=other_user.id)
    await db.commit()

    with pytest.raises(ModelNotFound):
        await create_assistant(
            db, workspace_id=workspace.id, created_by=user, name="Bot", instructions="x",
            model_id=other_model.id, tool_ids=[],
        )


async def test_create_assistant_rejects_a_tool_from_another_workspace(db: AsyncSession) -> None:
    user, workspace = await _user_with_workspace(db, email="svc3@example.com", slug="svc-ws3")
    other_user, other_workspace = await _user_with_workspace(db, email="svc3b@example.com", slug="svc-ws3b")
    other_tool = await _tool_in_workspace(
        db, workspace_id=other_workspace.id, created_by=other_user.id, name="get_weather"
    )
    await db.commit()

    with pytest.raises(ToolNotFound):
        await create_assistant(
            db, workspace_id=workspace.id, created_by=user, name="Bot", instructions="x",
            model_id=None, tool_ids=[other_tool.id],
        )


async def test_update_assistant_replaces_the_whole_tool_assignment(db: AsyncSession) -> None:
    """Sending tool_ids always replaces the full set — not an incremental add."""
    user, workspace = await _user_with_workspace(db, email="svc4@example.com", slug="svc-ws4")
    tool_a = await _tool_in_workspace(db, workspace_id=workspace.id, created_by=user.id, name="tool_a")
    tool_b = await _tool_in_workspace(db, workspace_id=workspace.id, created_by=user.id, name="tool_b")
    assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Bot", instructions="x",
        model_id=None, tool_ids=[tool_a.id],
    )
    await db.commit()

    await update_assistant(
        db, workspace_id=workspace.id, assistant_id=assistant.id, changes={"tool_ids": [tool_b.id]}
    )

    tool_ids = await list_assistant_tool_ids(db, assistant_id=assistant.id)
    assert tool_ids == [tool_b.id]


async def test_list_assistant_tools_excludes_disabled_ones(db: AsyncSession) -> None:
    """A tool assigned to an assistant but since disabled drops out of what's actually offered,
    without needing its assignment cleaned up separately."""
    user, workspace = await _user_with_workspace(db, email="svc5@example.com", slug="svc-ws5")
    tool = await _tool_in_workspace(db, workspace_id=workspace.id, created_by=user.id, name="get_weather")
    assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Bot", instructions="x",
        model_id=None, tool_ids=[tool.id],
    )
    await db.commit()

    assert [t.id for t in await list_assistant_tools(db, assistant_id=assistant.id)] == [tool.id]

    tool.enabled = False
    await db.flush()

    assert await list_assistant_tools(db, assistant_id=assistant.id) == []
    # still assigned, just not currently offered — disabling isn't unassigning
    assert await list_assistant_tool_ids(db, assistant_id=assistant.id) == [tool.id]


async def test_delete_assistant_nulls_conversations_that_used_it(db: AsyncSession) -> None:
    user, workspace = await _user_with_workspace(db, email="svc6@example.com", slug="svc-ws6")
    model = await _model_in_workspace(db, workspace_id=workspace.id, created_by=user.id)
    assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Bot", instructions="x",
        model_id=None, tool_ids=[],
    )
    await db.flush()
    conversation = Conversation(
        workspace_id=workspace.id, user_id=user.id, model_id=model.id,
        assistant_id=assistant.id, title="Test",
    )
    db.add(conversation)
    await db.commit()

    await delete_assistant(db, workspace_id=workspace.id, assistant_id=assistant.id)
    await db.commit()

    assert await db.get(Assistant, assistant.id) is None
    await db.refresh(conversation)
    assert conversation.assistant_id is None


async def test_list_assistants_orders_oldest_first(db: AsyncSession) -> None:
    user, workspace = await _user_with_workspace(db, email="svc7@example.com", slug="svc-ws7")
    first = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="First", instructions="x",
        model_id=None, tool_ids=[],
    )
    second = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Second", instructions="y",
        model_id=None, tool_ids=[],
    )
    await db.commit()

    assistants = await list_assistants(db, workspace_id=workspace.id)

    assert [a.id for a in assistants] == [first.id, second.id]


async def test_update_assistant_rejects_delegating_to_itself(db: AsyncSession) -> None:
    """An assistant can't be told to ask itself for help. Only reachable via update, not create —
    an assistant's id doesn't exist yet for a caller to name at creation time."""
    user, workspace = await _user_with_workspace(db, email="svc8@example.com", slug="svc-ws8")
    assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Bot", instructions="x",
        model_id=None, tool_ids=[],
    )
    await db.commit()

    with pytest.raises(SelfDelegation):
        await update_assistant(
            db, workspace_id=workspace.id, assistant_id=assistant.id,
            changes={"delegate_ids": [assistant.id]},
        )


async def test_create_assistant_rejects_a_delegate_from_another_workspace(db: AsyncSession) -> None:
    user, workspace = await _user_with_workspace(db, email="svc9@example.com", slug="svc-ws9")
    other_user, other_workspace = await _user_with_workspace(db, email="svc9b@example.com", slug="svc-ws9b")
    other_assistant = await create_assistant(
        db, workspace_id=other_workspace.id, created_by=other_user, name="Other", instructions="x",
        model_id=None, tool_ids=[],
    )
    await db.commit()

    with pytest.raises(AssistantNotFound):
        await create_assistant(
            db, workspace_id=workspace.id, created_by=user, name="Bot", instructions="x",
            model_id=None, tool_ids=[], delegate_ids=[other_assistant.id],
        )


async def test_assistant_can_delegate_to_another_in_the_same_workspace(db: AsyncSession) -> None:
    user, workspace = await _user_with_workspace(db, email="svc10@example.com", slug="svc-ws10")
    researcher = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Researcher", instructions="Research things.",
        model_id=None, tool_ids=[],
    )
    orchestrator = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Writer", instructions="Write things.",
        model_id=None, tool_ids=[], delegate_ids=[researcher.id],
    )
    await db.commit()

    delegate_ids = await list_assistant_delegate_ids(db, assistant_id=orchestrator.id)
    delegates = await list_assistant_delegates(db, assistant_id=orchestrator.id)

    assert delegate_ids == [researcher.id]
    assert [d.id for d in delegates] == [researcher.id]


async def test_update_assistant_replaces_the_whole_delegate_assignment(db: AsyncSession) -> None:
    """Sending delegate_ids always replaces the full set — not an incremental add, same shape
    tool_ids already follows."""
    user, workspace = await _user_with_workspace(db, email="svc11@example.com", slug="svc-ws11")
    a = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="A", instructions="x",
        model_id=None, tool_ids=[],
    )
    b = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="B", instructions="x",
        model_id=None, tool_ids=[],
    )
    orchestrator = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Orchestrator", instructions="x",
        model_id=None, tool_ids=[], delegate_ids=[a.id],
    )
    await db.commit()

    await update_assistant(
        db, workspace_id=workspace.id, assistant_id=orchestrator.id, changes={"delegate_ids": [b.id]}
    )

    delegate_ids = await list_assistant_delegate_ids(db, assistant_id=orchestrator.id)
    assert delegate_ids == [b.id]

    await update_assistant(
        db, workspace_id=workspace.id, assistant_id=orchestrator.id, changes={"delegate_ids": []}
    )
    assert await list_assistant_delegate_ids(db, assistant_id=orchestrator.id) == []
