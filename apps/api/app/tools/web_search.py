"""The built-in web search tool, backed by Tavily's search API.

Tavily was picked over a scraped, no-key search endpoint specifically because it returns text
already shaped for an LLM to read (a title, a URL, and a content snippet per result) rather than
raw HTML a second parsing step would have to clean up.
"""

import httpx

from app.core.crypto import EncryptedSecret, decrypt_secret
from app.models import Tool
from app.tools.base import ToolExecutionResult

TAVILY_URL = "https://api.tavily.com/search"
MAX_RESULTS = 5
_TIMEOUT = 15.0

# What create_tool() stores for the seeded web_search row, and what the agent loop offers the
# model as this tool's definition.
NAME = "web_search"
DESCRIPTION = (
    "Search the web for current or unfamiliar information — anything that might have changed "
    "since training, or that you aren't confident about from memory alone."
)
PARAMETERS: dict[str, object] = {
    "type": "object",
    "properties": {"query": {"type": "string", "description": "The search query."}},
    "required": ["query"],
}

# Overridable only from tests, to exercise this module's request/response handling against a
# fake transport instead of the real network — the same seam every provider adapter's own
# `_client()` method already gives itself.
_transport: httpx.AsyncBaseTransport | None = None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_TIMEOUT, transport=_transport)


async def execute(tool: Tool, arguments: dict[str, object]) -> ToolExecutionResult:
    """Run one search and flatten Tavily's results into text the model can read directly."""
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        return ToolExecutionResult(ok=False, content="No search query was provided.")
    if tool.ciphertext is None or tool.nonce is None or tool.wrapped_key is None:
        return ToolExecutionResult(ok=False, content="Web search has no API key configured.")

    api_key = decrypt_secret(
        EncryptedSecret(ciphertext=tool.ciphertext, nonce=tool.nonce, wrapped_key=tool.wrapped_key)
    )
    try:
        async with _client() as client:
            response = await client.post(
                TAVILY_URL,
                json={"api_key": api_key, "query": query, "max_results": MAX_RESULTS},
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return ToolExecutionResult(
            ok=False, content=f"Search failed ({exc.response.status_code})."
        )
    except httpx.HTTPError as exc:
        return ToolExecutionResult(ok=False, content=f"Could not reach the search provider: {exc}")

    results = response.json().get("results", [])
    if not results:
        return ToolExecutionResult(ok=True, content="No results found.")
    lines = [
        f"- {r.get('title', '(untitled)')} ({r.get('url', '')}): {r.get('content', '')}"
        for r in results[:MAX_RESULTS]
    ]
    return ToolExecutionResult(ok=True, content="\n".join(lines))
