"""Executes a third-party HTTP tool — the model's arguments become the request, and the response
(truncated, and never carrying the tool's own secret back out) becomes the result fed back to it.
"""

import httpx

from app.core.crypto import EncryptedSecret, decrypt_secret
from app.core.ssrf import UnsafeBaseUrlError, assert_safe_base_url
from app.models import Tool
from app.tools.base import ToolExecutionResult

_TIMEOUT = 10.0
MAX_RESPONSE_CHARS = 8_000

# Overridable only from tests, to exercise this module's request/response handling against a
# fake transport instead of the real network — the same seam web_search.py gives itself.
_transport: httpx.AsyncBaseTransport | None = None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_TIMEOUT, transport=_transport)


async def execute(tool: Tool, arguments: dict[str, object]) -> ToolExecutionResult:
    """Call the tool's configured endpoint with the model's arguments as the request body (for a
    body-carrying method) or query params (for GET/DELETE)."""
    if not tool.url or not tool.method:
        return ToolExecutionResult(ok=False, content="This tool has no endpoint configured.")

    # Re-checked here, not just at registration time — a DNS answer can change in between (see
    # app/core/ssrf.py's own docstring), and this is the moment a request actually goes out.
    try:
        assert_safe_base_url(tool.url)
    except UnsafeBaseUrlError as exc:
        return ToolExecutionResult(ok=False, content=f"This tool's endpoint is no longer allowed: {exc}")

    headers: dict[str, str] = {}
    if tool.secret_header and tool.ciphertext and tool.nonce and tool.wrapped_key:
        secret = decrypt_secret(
            EncryptedSecret(ciphertext=tool.ciphertext, nonce=tool.nonce, wrapped_key=tool.wrapped_key)
        )
        headers[tool.secret_header] = secret

    try:
        async with _client() as client:
            if tool.method in ("GET", "DELETE"):
                # A tool call's arguments are arbitrary JSON — httpx's own params type only
                # accepts flat scalars, which is all a GET/DELETE tool's arguments should
                # sensibly be anyway; a nested value here fails at the request itself, caught by
                # execute_tool()'s own catch-all rather than needing a check duplicated here.
                response = await client.request(
                    tool.method, tool.url, params=arguments, headers=headers  # type: ignore[arg-type]
                )
            else:
                response = await client.request(tool.method, tool.url, json=arguments, headers=headers)
    except httpx.HTTPError as exc:
        return ToolExecutionResult(ok=False, content=f"Could not reach the tool: {exc}")

    body = response.text[:MAX_RESPONSE_CHARS]
    if response.status_code >= 400:
        return ToolExecutionResult(
            ok=False, content=f"The tool returned an error ({response.status_code}): {body}"
        )
    return ToolExecutionResult(ok=True, content=body)
