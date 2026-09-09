"""Registering, listing, and disabling a workspace's tools — gated by the `tools` flag, same
shape test_attachments.py uses for `attachments` — plus the built-in web search executor and the
execute_tool() dispatcher every tool call runs through.
"""

import asyncio
import uuid

import httpx
import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import EncryptedSecret, decrypt_secret, encrypt_secret
from app.core.errors import ToolNotFound
from app.models import AuditLog, FeatureFlag, FlagScope, Tool, ToolKind, User, Workspace
from app.services.flags import set_override
from app.services.tools import (
    disable_tool,
    enable_web_search,
    get_tool,
    list_enabled_tools,
    list_tools,
    to_tool_definition,
)
from app.tools import web_search
from app.tools.base import ToolExecutionResult
from app.tools.execute import MAX_RESULT_CHARS, execute_tool

OWNER = {"email": "owner@example.com", "password": "correct horse battery staple"}


async def _owner_with_workspace(client: AsyncClient) -> str:
    """Sign up and log in as the owner, create a workspace, and return its id."""
    await client.post("/api/v1/auth/signup", json=OWNER)
    await client.post("/api/v1/auth/login", json=OWNER)
    created = await client.post("/api/v1/workspaces", json={"name": "Acme"})
    return str(created.json()["id"])


async def _enable_tools_flag(db: AsyncSession, redis: Redis, *, workspace_id: str) -> None:
    """Flip the `tools` flag on for one workspace — the same lever the tools settings page pulls."""
    flag = await db.scalar(select(FeatureFlag).where(FeatureFlag.key == "tools"))
    assert flag is not None
    await set_override(
        db, redis, flag_id=flag.id, scope=FlagScope.WORKSPACE, scope_id=uuid.UUID(workspace_id), value=True
    )
    await db.commit()  # this test's `db` session must commit for the client's own connection to see it


# ---------- Router: gating, enabling, listing, disabling ----------


async def test_tools_routes_404_while_the_flag_is_off(client: AsyncClient) -> None:
    """`tools` defaults to off — every route 404s entirely, same as a route that doesn't exist."""
    workspace_id = await _owner_with_workspace(client)

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tools/web-search", json={"api_key": "tvly-fake"}
    )

    assert response.status_code == 404


async def test_enabling_web_search_creates_a_tool_without_exposing_the_key(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    workspace_id = await _owner_with_workspace(client)
    await _enable_tools_flag(db, redis_client, workspace_id=workspace_id)

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tools/web-search", json={"api_key": "tvly-secret-key"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "web_search"
    assert body["kind"] == "builtin"
    assert body["enabled"] is True
    assert "api_key" not in body
    assert "ciphertext" not in body


async def test_enabling_web_search_again_rotates_the_key_in_place(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """A second enable doesn't create a second tool — it's the same row, with a new key."""
    workspace_id = await _owner_with_workspace(client)
    await _enable_tools_flag(db, redis_client, workspace_id=workspace_id)
    first = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tools/web-search", json={"api_key": "tvly-old-key"}
    )

    second = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tools/web-search", json={"api_key": "tvly-new-key"}
    )

    assert first.json()["id"] == second.json()["id"]
    listed = await client.get(f"/api/v1/workspaces/{workspace_id}/tools")
    assert len(listed.json()) == 1

    tool = await db.get(Tool, uuid.UUID(second.json()["id"]))
    assert tool is not None
    assert tool.ciphertext is not None
    assert tool.nonce is not None
    assert tool.wrapped_key is not None
    secret = EncryptedSecret(ciphertext=tool.ciphertext, nonce=tool.nonce, wrapped_key=tool.wrapped_key)
    assert decrypt_secret(secret) == "tvly-new-key"


async def test_disabling_a_tool_keeps_its_row(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """Disabling isn't deleting — the row (and any secret it holds) stays, so re-enabling later
    doesn't mean reconfiguring from scratch, same as disable_model()."""
    workspace_id = await _owner_with_workspace(client)
    await _enable_tools_flag(db, redis_client, workspace_id=workspace_id)
    created = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tools/web-search", json={"api_key": "tvly-fake"}
    )
    tool_id = created.json()["id"]

    response = await client.delete(f"/api/v1/workspaces/{workspace_id}/tools/{tool_id}")

    assert response.status_code == 204
    listed = await client.get(f"/api/v1/workspaces/{workspace_id}/tools")
    assert len(listed.json()) == 1
    assert listed.json()[0]["enabled"] is False


async def test_enabling_and_disabling_a_tool_are_audited(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    workspace_id = await _owner_with_workspace(client)
    await _enable_tools_flag(db, redis_client, workspace_id=workspace_id)
    created = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tools/web-search", json={"api_key": "tvly-fake"}
    )
    tool_id = created.json()["id"]
    await client.delete(f"/api/v1/workspaces/{workspace_id}/tools/{tool_id}")

    actions = (
        await db.scalars(
            select(AuditLog.action).where(AuditLog.workspace_id == uuid.UUID(workspace_id))
        )
    ).all()
    assert "tool.enabled" in actions
    assert "tool.disabled" in actions


# ---------- Service ----------


async def test_get_tool_rejects_one_from_another_workspace(db: AsyncSession) -> None:
    user = User(email="svc@example.com", password_hash="hashed")
    db.add(user)
    await db.flush()
    workspace = Workspace(slug="svc-ws", name="Svc", owner_id=user.id)
    db.add(workspace)
    await db.flush()
    tool = await enable_web_search(db, workspace_id=workspace.id, created_by=user, api_key="tvly-x")
    await db.commit()
    other_workspace = Workspace(slug="svc-other", name="Other", owner_id=user.id)
    db.add(other_workspace)
    await db.flush()

    with pytest.raises(ToolNotFound):
        await get_tool(db, workspace_id=other_workspace.id, tool_id=tool.id)


async def test_list_enabled_tools_excludes_disabled_ones(db: AsyncSession) -> None:
    user = User(email="svc2@example.com", password_hash="hashed")
    db.add(user)
    await db.flush()
    workspace = Workspace(slug="svc-ws2", name="Svc2", owner_id=user.id)
    db.add(workspace)
    await db.flush()
    tool = await enable_web_search(db, workspace_id=workspace.id, created_by=user, api_key="tvly-x")
    await db.commit()

    await disable_tool(db, workspace_id=workspace.id, tool_id=tool.id)

    assert await list_enabled_tools(db, workspace_id=workspace.id) == []
    assert [t.id for t in await list_tools(db, workspace_id=workspace.id)] == [tool.id]


def test_to_tool_definition_matches_the_tool_s_own_fields() -> None:
    tool = Tool(
        workspace_id=uuid.uuid4(),
        name="web_search",
        description="Search the web.",
        parameters={"type": "object"},
        kind=ToolKind.BUILTIN,
        enabled=True,
        created_by=uuid.uuid4(),
    )

    definition = to_tool_definition(tool)

    assert definition.name == "web_search"
    assert definition.description == "Search the web."
    assert definition.parameters == {"type": "object"}


# ---------- Web search executor ----------


def _tool_with_key(api_key: str) -> Tool:
    secret = encrypt_secret(api_key)
    return Tool(
        workspace_id=uuid.uuid4(),
        name=web_search.NAME,
        description=web_search.DESCRIPTION,
        parameters=web_search.PARAMETERS,
        kind=ToolKind.BUILTIN,
        enabled=True,
        created_by=uuid.uuid4(),
        ciphertext=secret.ciphertext,
        nonce=secret.nonce,
        wrapped_key=secret.wrapped_key,
    )


async def test_web_search_flattens_results_into_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Paris weather", "url": "https://example.com/paris", "content": "Sunny, 22C"}
                ]
            },
        )

    monkeypatch.setattr(web_search, "_transport", httpx.MockTransport(handler))

    result = await web_search.execute(_tool_with_key("tvly-x"), {"query": "weather in paris"})

    assert result.ok is True
    assert "Paris weather" in result.content
    assert "https://example.com/paris" in result.content
    assert "Sunny, 22C" in result.content


async def test_web_search_with_no_results(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    monkeypatch.setattr(web_search, "_transport", httpx.MockTransport(handler))

    result = await web_search.execute(_tool_with_key("tvly-x"), {"query": "something obscure"})

    assert result.ok is True
    assert result.content == "No results found."


async def test_web_search_requires_a_query() -> None:
    result = await web_search.execute(_tool_with_key("tvly-x"), {})

    assert result.ok is False
    assert "query" in result.content.lower()


async def test_web_search_without_a_configured_key() -> None:
    tool = Tool(
        workspace_id=uuid.uuid4(),
        name=web_search.NAME,
        description=web_search.DESCRIPTION,
        parameters=web_search.PARAMETERS,
        kind=ToolKind.BUILTIN,
        enabled=True,
        created_by=uuid.uuid4(),
    )

    result = await web_search.execute(tool, {"query": "anything"})

    assert result.ok is False
    assert "key" in result.content.lower()


async def test_web_search_reports_a_rejected_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid api key"})

    monkeypatch.setattr(web_search, "_transport", httpx.MockTransport(handler))

    result = await web_search.execute(_tool_with_key("tvly-bad"), {"query": "anything"})

    assert result.ok is False
    assert "401" in result.content


# ---------- execute_tool() dispatcher ----------


async def test_execute_tool_dispatches_to_the_matching_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    monkeypatch.setattr(web_search, "_transport", httpx.MockTransport(handler))

    result, latency_ms = await execute_tool(_tool_with_key("tvly-x"), {"query": "x"})

    assert result.ok is True
    assert latency_ms >= 0


async def test_execute_tool_reports_an_unknown_builtin_name() -> None:
    tool = Tool(
        workspace_id=uuid.uuid4(),
        name="not_a_real_tool",
        description="",
        parameters={},
        kind=ToolKind.BUILTIN,
        enabled=True,
        created_by=uuid.uuid4(),
    )

    result, _ = await execute_tool(tool, {})

    assert result.ok is False
    assert "not_a_real_tool" in result.content


async def test_execute_tool_reports_http_tools_as_not_yet_supported() -> None:
    tool = Tool(
        workspace_id=uuid.uuid4(),
        name="some_http_tool",
        description="",
        parameters={},
        kind=ToolKind.HTTP,
        enabled=True,
        created_by=uuid.uuid4(),
    )

    result, _ = await execute_tool(tool, {})

    assert result.ok is False


async def test_execute_tool_truncates_an_oversized_result(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _huge_executor(tool: Tool, arguments: dict[str, object]) -> ToolExecutionResult:
        del tool, arguments
        return ToolExecutionResult(ok=True, content="x" * (MAX_RESULT_CHARS + 500))

    from app.tools.registry import BUILTIN_TOOLS, BuiltinToolSpec

    monkeypatch.setitem(
        BUILTIN_TOOLS,
        "huge_tool",
        BuiltinToolSpec(name="huge_tool", description="", parameters={}, execute=_huge_executor),
    )
    tool = Tool(
        workspace_id=uuid.uuid4(),
        name="huge_tool",
        description="",
        parameters={},
        kind=ToolKind.BUILTIN,
        enabled=True,
        created_by=uuid.uuid4(),
    )

    result, _ = await execute_tool(tool, {})

    assert len(result.content) <= MAX_RESULT_CHARS + len("\n\n[...truncated]")
    assert result.content.endswith("[...truncated]")


async def test_execute_tool_times_out_a_hanging_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _hangs_forever(tool: Tool, arguments: dict[str, object]) -> ToolExecutionResult:
        del tool, arguments
        await asyncio.sleep(999)
        return ToolExecutionResult(ok=True, content="never")

    from app.tools.registry import BUILTIN_TOOLS, BuiltinToolSpec

    monkeypatch.setitem(
        BUILTIN_TOOLS,
        "slow_tool",
        BuiltinToolSpec(name="slow_tool", description="", parameters={}, execute=_hangs_forever),
    )
    monkeypatch.setattr("app.tools.execute.EXECUTION_TIMEOUT_SECONDS", 0.05)
    tool = Tool(
        workspace_id=uuid.uuid4(),
        name="slow_tool",
        description="",
        parameters={},
        kind=ToolKind.BUILTIN,
        enabled=True,
        created_by=uuid.uuid4(),
    )

    result, _ = await execute_tool(tool, {})

    assert result.ok is False
    assert "timed out" in result.content
