"""The MCP tool executor — listing a server's tools and calling one — against an in-process MCP
server, plus the execute_tool() dispatcher routing an MCP row to it."""

import asyncio
import uuid
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent

from app.core.crypto import encrypt_secret
from app.models import Tool, ToolKind
from app.tools import execute, mcp_tool
from app.tools.execute import execute_tool

_server = MCPServer("test")


@_server.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@_server.tool()
def explode() -> str:
    """Always fails."""
    raise ValueError("kaboom")


@_server.tool()
def snapshot() -> list[Any]:
    """Return an image the model can't be sent as text."""
    return [ImageContent(data="aGk=", mime_type="image/png")]


@pytest.fixture
def in_process_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route every MCP connection to the in-process test server."""
    monkeypatch.setattr(mcp_tool, "_server_override", _server)


def _mcp_tool(**overrides: object) -> Tool:
    defaults: dict[str, object] = dict(
        workspace_id=uuid.uuid4(),
        name="demo__add",
        description="Add two numbers.",
        parameters={"type": "object"},
        kind=ToolKind.MCP,
        enabled=True,
        created_by=uuid.uuid4(),
        url="https://example.com/mcp",
        remote_name="add",
    )
    defaults.update(overrides)
    return Tool(**defaults)


@pytest.mark.usefixtures("in_process_server")
async def test_list_remote_tools_returns_each_tool_s_schema() -> None:
    tools = await mcp_tool.list_remote_tools("https://example.com/mcp", {})

    by_name = {t.name: t for t in tools}
    assert set(by_name) == {"add", "explode", "snapshot"}
    assert by_name["add"].description == "Add two numbers."
    assert by_name["add"].parameters["required"] == ["a", "b"]


@pytest.mark.usefixtures("in_process_server")
async def test_list_remote_tools_stops_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_tool, "MAX_TOOLS_PER_SERVER", 1)

    assert len(await mcp_tool.list_remote_tools("https://example.com/mcp", {})) == 1


@pytest.mark.usefixtures("in_process_server")
async def test_execute_calls_the_tool_by_its_remote_name() -> None:
    result = await mcp_tool.execute(_mcp_tool(), {"a": 2, "b": 3})

    assert result.ok is True
    assert result.content == "5"


@pytest.mark.usefixtures("in_process_server")
async def test_execute_reports_a_tool_error_as_a_failed_result() -> None:
    result = await mcp_tool.execute(_mcp_tool(name="demo__explode", remote_name="explode"), {})

    assert result.ok is False
    assert "error" in result.content.lower()


@pytest.mark.usefixtures("in_process_server")
async def test_execute_replaces_non_text_content_with_a_placeholder() -> None:
    result = await mcp_tool.execute(_mcp_tool(name="demo__snapshot", remote_name="snapshot"), {})

    assert result.ok is True
    assert result.content == "[image content omitted]"


async def test_execute_rechecks_the_url_against_the_ssrf_guard() -> None:
    result = await mcp_tool.execute(_mcp_tool(url="http://127.0.0.1:8000/mcp"), {})

    assert result.ok is False
    assert "allowed" in result.content.lower()


async def test_execute_reports_an_unreachable_server_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Refusing:
        async def __aenter__(self) -> None:
            raise ExceptionGroup("task group", [ConnectionError("connection refused")])

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(mcp_tool, "_client", lambda url, headers: _Refusing())

    result = await mcp_tool.execute(_mcp_tool(), {})

    assert result.ok is False
    assert result.content == "Could not reach the MCP server: connection refused"


async def test_execute_sends_the_decrypted_auth_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, dict[str, str]] = {}
    real_client = mcp_tool._client

    def spy(url: str, headers: dict[str, str]) -> Any:
        captured["headers"] = headers
        return real_client(url, headers)

    monkeypatch.setattr(mcp_tool, "_server_override", _server)
    monkeypatch.setattr(mcp_tool, "_client", spy)
    secret = encrypt_secret("token-123")
    tool = _mcp_tool(
        secret_header="Authorization",
        ciphertext=secret.ciphertext,
        nonce=secret.nonce,
        wrapped_key=secret.wrapped_key,
    )

    result = await mcp_tool.execute(tool, {"a": 1, "b": 1})

    assert result.ok is True
    assert captured["headers"] == {"Authorization": "token-123"}


@pytest.mark.usefixtures("in_process_server")
async def test_execute_tool_dispatches_an_mcp_tool() -> None:
    result, _latency_ms = await execute_tool(_mcp_tool(), {"a": 4, "b": 5})

    assert result.ok is True
    assert result.content == "9"


async def test_execute_tool_gives_an_mcp_tool_the_longer_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A call slower than the default ceiling but within the MCP one still succeeds."""

    async def slow(tool: Tool, arguments: dict[str, object]) -> mcp_tool.ToolExecutionResult:
        await asyncio.sleep(0.1)
        return mcp_tool.ToolExecutionResult(ok=True, content="done")

    monkeypatch.setattr(mcp_tool, "execute", slow)
    monkeypatch.setattr(execute, "EXECUTION_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(execute, "MCP_EXECUTION_TIMEOUT_SECONDS", 1.0)

    result, _latency_ms = await execute_tool(_mcp_tool(), {})

    assert result.content == "done"
