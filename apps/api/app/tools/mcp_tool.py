"""Talks to a remote MCP server over Streamable HTTP — listing the tools it offers (for
services/mcp_servers.py to sync into `tools` rows) and executing one of them.

Only remote servers are supported: a stdio server means running a user-supplied command on the
API host, which a multi-tenant app can't allow. A fresh session is opened per call, so nothing
here holds state between requests — the same stateless shape as the HTTP tool executor.
"""

import json
from dataclasses import dataclass
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, EmbeddedResource, ResourceLink, TextContent, TextResourceContents

from app.core.crypto import EncryptedSecret, decrypt_secret
from app.core.ssrf import UnsafeBaseUrlError, assert_safe_base_url
from app.models import Tool
from app.tools.base import ToolExecutionResult

_TIMEOUT = 15.0
# A server offering more than this is almost certainly not something a model should see in full.
MAX_TOOLS_PER_SERVER = 100

# Overridable only from tests, to run against an in-process server instead of the real network —
# the same seam http_tool.py's `_transport` gives itself.
_server_override: MCPServer | None = None


@dataclass(frozen=True)
class RemoteTool:
    """One tool as a server's `tools/list` describes it."""

    name: str
    description: str
    parameters: dict[str, Any]


def _client(url: str, headers: dict[str, str]) -> Client:
    """Build a client for one server, carrying its auth header on every request."""
    if _server_override is not None:
        return Client(_server_override, cache=None)
    http_client = httpx2.AsyncClient(headers=headers, timeout=_TIMEOUT)
    return Client(streamable_http_client(url, http_client=http_client), cache=None)


def _root_cause(exc: BaseException) -> BaseException:
    """Unwrap the task-group exception groups the SDK raises down to the error that caused them."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


async def list_remote_tools(url: str, headers: dict[str, str]) -> list[RemoteTool]:
    """Fetch every tool the server offers, following pagination up to MAX_TOOLS_PER_SERVER.

    Raises whatever the connection raised, unwrapped to its root cause — the caller turns that
    into a clean error for the person registering or syncing the server.
    """
    tools: list[RemoteTool] = []
    try:
        async with _client(url, headers) as client:
            cursor: str | None = None
            while len(tools) < MAX_TOOLS_PER_SERVER:
                page = await client.list_tools(cursor=cursor)
                tools.extend(
                    RemoteTool(name=t.name, description=t.description or "", parameters=t.input_schema)
                    for t in page.tools
                )
                cursor = page.next_cursor
                if cursor is None:
                    break
    except Exception as exc:
        raise _root_cause(exc) from exc
    return tools[:MAX_TOOLS_PER_SERVER]


def auth_headers(
    header: str | None, *, ciphertext: bytes | None, nonce: bytes | None, wrapped_key: bytes | None
) -> dict[str, str]:
    """Decrypt a stored auth secret into the header dict sent to the server, or {} if none."""
    if not header or ciphertext is None or nonce is None or wrapped_key is None:
        return {}
    secret = decrypt_secret(EncryptedSecret(ciphertext=ciphertext, nonce=nonce, wrapped_key=wrapped_key))
    return {header: secret}


def _flatten(result: CallToolResult) -> str:
    """Turn a result's content blocks into the plain text every provider adapter accepts."""
    parts: list[str] = []
    for block in result.content:
        if isinstance(block, TextContent):
            parts.append(block.text)
        elif isinstance(block, EmbeddedResource) and isinstance(block.resource, TextResourceContents):
            parts.append(block.resource.text)
        elif isinstance(block, ResourceLink):
            parts.append(f"[resource: {block.uri}]")
        else:
            parts.append(f"[{block.type} content omitted]")
    if not parts and result.structured_content is not None:
        parts.append(json.dumps(result.structured_content))
    return "\n".join(parts)


async def execute(tool: Tool, arguments: dict[str, object]) -> ToolExecutionResult:
    """Call the tool on its server by its remote name, and return the flattened result."""
    if not tool.url or not tool.remote_name:
        return ToolExecutionResult(ok=False, content="This tool has no MCP server configured.")

    # Re-checked at call time, not just at registration — see http_tool.py for why.
    try:
        assert_safe_base_url(tool.url)
    except UnsafeBaseUrlError as exc:
        return ToolExecutionResult(ok=False, content=f"This tool's server is no longer allowed: {exc}")

    headers = auth_headers(
        tool.secret_header, ciphertext=tool.ciphertext, nonce=tool.nonce, wrapped_key=tool.wrapped_key
    )
    try:
        async with _client(tool.url, headers) as client:
            result = await client.call_tool(tool.remote_name, dict(arguments))
    except Exception as exc:  # noqa: BLE001 — an unreachable server is a tool error, not a crash
        return ToolExecutionResult(ok=False, content=f"Could not reach the MCP server: {_root_cause(exc)}")

    content = _flatten(result)
    if result.is_error:
        return ToolExecutionResult(ok=False, content=f"The tool returned an error: {content}")
    return ToolExecutionResult(ok=True, content=content)
