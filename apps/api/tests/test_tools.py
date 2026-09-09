"""Registering, listing, updating, and removing a workspace's tools — gated by the `tools` flag,
same shape test_attachments.py uses for `attachments` — plus the built-in web search and HTTP
tool executors, and the execute_tool() dispatcher every tool call runs through.
"""

import asyncio
import json
import uuid

import httpx
import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import EncryptedSecret, decrypt_secret, encrypt_secret
from app.core.errors import ToolNameAlreadyExists, ToolNotFound
from app.core.ssrf import UnsafeBaseUrlError
from app.models import (
    AuditLog,
    Conversation,
    FeatureFlag,
    FlagScope,
    LLMModel,
    Message,
    MessageRole,
    Provider,
    ProviderCredential,
    Tool,
    ToolInvocation,
    ToolInvocationStatus,
    ToolKind,
    User,
    Workspace,
)
from app.services.flags import set_override
from app.services.tools import (
    create_http_tool,
    delete_tool,
    enable_web_search,
    get_tool,
    list_enabled_tools,
    list_tools,
    to_tool_definition,
    update_tool,
)
from app.tools import http_tool, web_search
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

    response = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/tools/{tool_id}", json={"enabled": False}
    )

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    listed = await client.get(f"/api/v1/workspaces/{workspace_id}/tools")
    assert len(listed.json()) == 1
    assert listed.json()[0]["enabled"] is False


async def test_deleting_a_tool_removes_its_row(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    """Unlike disabling, DELETE is permanent — the row (and its secret) is actually gone."""
    workspace_id = await _owner_with_workspace(client)
    await _enable_tools_flag(db, redis_client, workspace_id=workspace_id)
    created = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tools/web-search", json={"api_key": "tvly-fake"}
    )
    tool_id = created.json()["id"]

    response = await client.delete(f"/api/v1/workspaces/{workspace_id}/tools/{tool_id}")

    assert response.status_code == 204
    listed = await client.get(f"/api/v1/workspaces/{workspace_id}/tools")
    assert listed.json() == []
    assert await db.get(Tool, uuid.UUID(tool_id)) is None


async def test_enabling_and_updating_a_tool_are_audited(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    workspace_id = await _owner_with_workspace(client)
    await _enable_tools_flag(db, redis_client, workspace_id=workspace_id)
    created = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tools/web-search", json={"api_key": "tvly-fake"}
    )
    tool_id = created.json()["id"]
    await client.patch(f"/api/v1/workspaces/{workspace_id}/tools/{tool_id}", json={"enabled": False})
    await client.delete(f"/api/v1/workspaces/{workspace_id}/tools/{tool_id}")

    actions = (
        await db.scalars(
            select(AuditLog.action).where(AuditLog.workspace_id == uuid.UUID(workspace_id))
        )
    ).all()
    assert "tool.enabled" in actions
    assert "tool.updated" in actions
    assert "tool.deleted" in actions


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

    await update_tool(db, workspace_id=workspace.id, tool_id=tool.id, changes={"enabled": False})

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


async def _user_with_workspace(db: AsyncSession, *, email: str, slug: str) -> tuple[User, Workspace]:
    user = User(email=email, password_hash="hashed")
    db.add(user)
    await db.flush()
    workspace = Workspace(slug=slug, name=slug, owner_id=user.id)
    db.add(workspace)
    await db.flush()
    return user, workspace


async def test_create_http_tool_registers_a_row_with_its_secret_encrypted(db: AsyncSession) -> None:
    user, workspace = await _user_with_workspace(db, email="http1@example.com", slug="http-ws1")

    tool = await create_http_tool(
        db,
        workspace_id=workspace.id,
        created_by=user,
        name="get_weather",
        description="Look up the weather.",
        parameters={"type": "object", "properties": {"city": {"type": "string"}}},
        method="GET",
        url="https://example.com/weather",
        secret_header="X-Api-Key",
        secret_value="super-secret",
    )

    assert tool.kind == ToolKind.HTTP
    assert tool.enabled is True
    assert tool.method == "GET"
    assert tool.url == "https://example.com/weather"
    assert tool.secret_header == "X-Api-Key"
    assert tool.ciphertext is not None
    assert tool.nonce is not None
    assert tool.wrapped_key is not None
    secret = EncryptedSecret(ciphertext=tool.ciphertext, nonce=tool.nonce, wrapped_key=tool.wrapped_key)
    assert decrypt_secret(secret) == "super-secret"


async def test_create_http_tool_rejects_a_private_url(db: AsyncSession) -> None:
    user, workspace = await _user_with_workspace(db, email="http2@example.com", slug="http-ws2")

    with pytest.raises(UnsafeBaseUrlError):
        await create_http_tool(
            db,
            workspace_id=workspace.id,
            created_by=user,
            name="internal_probe",
            description="",
            parameters={},
            method="GET",
            url="http://127.0.0.1:6379",
            secret_header=None,
            secret_value=None,
        )


async def test_create_http_tool_rejects_a_name_already_taken_in_the_workspace(db: AsyncSession) -> None:
    user, workspace = await _user_with_workspace(db, email="http3@example.com", slug="http-ws3")
    await create_http_tool(
        db,
        workspace_id=workspace.id,
        created_by=user,
        name="get_weather",
        description="",
        parameters={},
        method="GET",
        url="https://example.com/weather",
        secret_header=None,
        secret_value=None,
    )

    with pytest.raises(ToolNameAlreadyExists):
        await create_http_tool(
            db,
            workspace_id=workspace.id,
            created_by=user,
            name="get_weather",
            description="Different tool, same name.",
            parameters={},
            method="POST",
            url="https://example.com/other",
            secret_header=None,
            secret_value=None,
        )


async def test_update_tool_edits_fields_and_rechecks_a_new_url(db: AsyncSession) -> None:
    user, workspace = await _user_with_workspace(db, email="http4@example.com", slug="http-ws4")
    tool = await create_http_tool(
        db,
        workspace_id=workspace.id,
        created_by=user,
        name="get_weather",
        description="Look up the weather.",
        parameters={"type": "object"},
        method="GET",
        url="https://example.com/weather",
        secret_header=None,
        secret_value=None,
    )
    await db.commit()

    updated = await update_tool(
        db,
        workspace_id=workspace.id,
        tool_id=tool.id,
        changes={"description": "Updated description.", "url": "https://example.com/weather/v2"},
    )

    assert updated.description == "Updated description."
    assert updated.url == "https://example.com/weather/v2"

    with pytest.raises(UnsafeBaseUrlError):
        await update_tool(
            db, workspace_id=workspace.id, tool_id=tool.id, changes={"url": "http://127.0.0.1:6379"}
        )


async def test_update_tool_rotates_the_secret(db: AsyncSession) -> None:
    user, workspace = await _user_with_workspace(db, email="http5@example.com", slug="http-ws5")
    tool = await create_http_tool(
        db,
        workspace_id=workspace.id,
        created_by=user,
        name="get_weather",
        description="",
        parameters={},
        method="GET",
        url="https://example.com/weather",
        secret_header="X-Api-Key",
        secret_value="old-secret",
    )
    await db.commit()

    updated = await update_tool(
        db, workspace_id=workspace.id, tool_id=tool.id, changes={"secret_value": "new-secret"}
    )

    assert updated.ciphertext is not None
    assert updated.nonce is not None
    assert updated.wrapped_key is not None
    secret = EncryptedSecret(
        ciphertext=updated.ciphertext, nonce=updated.nonce, wrapped_key=updated.wrapped_key
    )
    assert decrypt_secret(secret) == "new-secret"


async def test_update_tool_rejects_renaming_to_a_name_already_taken(db: AsyncSession) -> None:
    user, workspace = await _user_with_workspace(db, email="http6@example.com", slug="http-ws6")
    first = await create_http_tool(
        db, workspace_id=workspace.id, created_by=user, name="tool_one", description="",
        parameters={}, method="GET", url="https://example.com/one", secret_header=None, secret_value=None,
    )
    second = await create_http_tool(
        db, workspace_id=workspace.id, created_by=user, name="tool_two", description="",
        parameters={}, method="GET", url="https://example.com/two", secret_header=None, secret_value=None,
    )
    await db.commit()
    del first

    with pytest.raises(ToolNameAlreadyExists):
        await update_tool(
            db, workspace_id=workspace.id, tool_id=second.id, changes={"name": "tool_one"}
        )


async def test_delete_tool_removes_the_row_and_nulls_its_invocations(db: AsyncSession) -> None:
    user, workspace = await _user_with_workspace(db, email="http7@example.com", slug="http-ws7")
    tool = await create_http_tool(
        db, workspace_id=workspace.id, created_by=user, name="get_weather", description="",
        parameters={}, method="GET", url="https://example.com/weather",
        secret_header=None, secret_value=None,
    )
    secret = encrypt_secret(VALID_KEY := "sk-test-0000000000000000000000000000")
    credential = ProviderCredential(
        workspace_id=workspace.id,
        provider=Provider.ANTHROPIC,
        label="Prod",
        ciphertext=secret.ciphertext,
        nonce=secret.nonce,
        wrapped_key=secret.wrapped_key,
        last4=VALID_KEY[-4:],
        created_by=user.id,
    )
    db.add(credential)
    await db.flush()
    model = LLMModel(
        workspace_id=workspace.id,
        credential_id=credential.id,
        provider_model_id="fake-small",
        display_name="Fake Small",
    )
    db.add(model)
    await db.flush()
    conversation = Conversation(
        workspace_id=workspace.id, user_id=user.id, model_id=model.id, title="Test"
    )
    db.add(conversation)
    await db.flush()
    message = Message(conversation_id=conversation.id, role=MessageRole.ASSISTANT, content="Done.")
    db.add(message)
    await db.flush()
    invocation = ToolInvocation(
        workspace_id=workspace.id,
        message_id=message.id,
        tool_id=tool.id,
        name=tool.name,
        arguments={"city": "Paris"},
        status=ToolInvocationStatus.SUCCESS,
        result="Sunny.",
    )
    db.add(invocation)
    await db.commit()

    await delete_tool(db, workspace_id=workspace.id, tool_id=tool.id)
    await db.commit()

    assert await db.get(Tool, tool.id) is None
    await db.refresh(invocation)
    assert invocation.tool_id is None
    assert invocation.name == "get_weather"  # denormalized name survives the tool's own deletion


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


async def test_execute_tool_dispatches_an_http_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="sunny")

    monkeypatch.setattr(http_tool, "_transport", httpx.MockTransport(handler))
    tool = Tool(
        workspace_id=uuid.uuid4(),
        name="get_weather",
        description="",
        parameters={},
        kind=ToolKind.HTTP,
        enabled=True,
        created_by=uuid.uuid4(),
        method="GET",
        url="https://example.com/weather",
    )

    result, _ = await execute_tool(tool, {"city": "Paris"})

    assert result.ok is True
    assert result.content == "sunny"


async def test_execute_tool_reports_an_http_tool_with_no_endpoint_configured() -> None:
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


# ---------- HTTP tool executor ----------


def _http_tool(**overrides: object) -> Tool:
    defaults: dict[str, object] = dict(
        workspace_id=uuid.uuid4(),
        name="get_weather",
        description="Look up the weather.",
        parameters={"type": "object"},
        kind=ToolKind.HTTP,
        enabled=True,
        created_by=uuid.uuid4(),
        method="GET",
        url="https://example.com/weather",
    )
    defaults.update(overrides)
    return Tool(**defaults)


async def test_http_tool_executes_a_get_request_with_the_arguments_as_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, text='{"forecast": "sunny"}')

    monkeypatch.setattr(http_tool, "_transport", httpx.MockTransport(handler))

    result = await http_tool.execute(_http_tool(), {"city": "Paris"})

    assert result.ok is True
    assert result.content == '{"forecast": "sunny"}'
    assert captured["request"].url.params["city"] == "Paris"


async def test_http_tool_sends_a_json_body_for_a_post_request(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(http_tool, "_transport", httpx.MockTransport(handler))

    result = await http_tool.execute(_http_tool(method="POST"), {"city": "Paris"})

    assert result.ok is True
    assert json.loads(captured["request"].content) == {"city": "Paris"}


async def test_http_tool_sends_the_secret_header_and_never_returns_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, httpx.Request] = {}
    secret = encrypt_secret("sk-tool-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(http_tool, "_transport", httpx.MockTransport(handler))

    result = await http_tool.execute(
        _http_tool(
            secret_header="X-Api-Key",
            ciphertext=secret.ciphertext,
            nonce=secret.nonce,
            wrapped_key=secret.wrapped_key,
        ),
        {"city": "Paris"},
    )

    assert captured["request"].headers["X-Api-Key"] == "sk-tool-secret"
    assert "sk-tool-secret" not in result.content


async def test_http_tool_reports_a_non_2xx_response_as_an_error_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    monkeypatch.setattr(http_tool, "_transport", httpx.MockTransport(handler))

    result = await http_tool.execute(_http_tool(), {"city": "Paris"})

    assert result.ok is False
    assert "500" in result.content


async def test_http_tool_truncates_a_response_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * (http_tool.MAX_RESPONSE_CHARS + 500))

    monkeypatch.setattr(http_tool, "_transport", httpx.MockTransport(handler))

    result = await http_tool.execute(_http_tool(), {})

    assert len(result.content) == http_tool.MAX_RESPONSE_CHARS


async def test_http_tool_rechecks_the_url_against_the_ssrf_guard_before_every_call() -> None:
    """A tool that was safe at registration time but now points somewhere disallowed (a changed
    DNS answer, or a row edited around the check) is refused at call time too."""
    result = await http_tool.execute(_http_tool(url="http://127.0.0.1:6379"), {})

    assert result.ok is False
    assert "allowed" in result.content.lower()


async def test_http_tool_reports_unreachable_endpoints_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(http_tool, "_transport", httpx.MockTransport(handler))

    result = await http_tool.execute(_http_tool(), {})

    assert result.ok is False
    assert "reach" in result.content.lower()


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
