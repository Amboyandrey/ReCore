"""Wire adapter for mem0's memory API (https://api.mem0.ai).

Every function returns a value that expresses failure — `None`, or `False` — rather than raising.
Memory is an enhancement to a chat, never a reason one should fail: the retrieval and write call
sites in services/memory.py and services/chat.py depend on that contract to degrade a mem0 outage
into "no memories this turn," not a broken generation.
"""

import httpx

BASE_URL = "https://api.mem0.ai"
# Generous on purpose: a search with `rerank=True` costs real extra latency on mem0's side, and a
# slow-but-successful search is worth far more to this feature than a fast, silent failure — a
# 5s timeout was found live to trip often enough that some replies proceeded with no memory at
# all, indistinguishable from the model simply not having anything relevant to say.
_TIMEOUT = 10.0

# Overridable only from tests, to exercise this module's request/response handling against a
# fake transport instead of the real network — the same seam every other executor in this
# codebase (web_search.py, http_tool.py) already gives itself.
_transport: httpx.AsyncBaseTransport | None = None


def _client(api_key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE_URL,
        timeout=_TIMEOUT,
        transport=_transport,
        headers={"Authorization": f"Token {api_key}"},
    )


async def add(
    *,
    api_key: str,
    messages: list[dict[str, str]],
    agent_id: str,
    user_id: str | None = None,
    infer: bool = True,
    immutable: bool = False,
    includes: str | None = None,
    agent_custom_instructions: str | None = None,
) -> bool:
    """Queue `messages` to be turned into memories under `agent_id` (and `user_id`, if given).

    mem0 processes this asynchronously — a `True` return means the request was accepted, not that
    extraction has finished (or that it produced anything: `infer=True` is a genuine LLM
    classifier on mem0's side, which can decide a turn has nothing memorable in it at all).
    `infer=False` stores the given text verbatim instead of having mem0 interpret it, which is
    what a deliberately curated fact wants; `immutable=True` excludes it from mem0's own later
    consolidation, so chat-driven learning can never silently rewrite it.

    `includes` and `agent_custom_instructions` both bias that classifier rather than overriding
    it — see services/memory.py's own use of them for why the latter, specifically, is the field
    that actually governs extraction here: mem0 documents `custom_instructions` as covering only
    non-assistant memories once both `agent_id` and `user_id` are present on the same call (its
    "hybrid mode"), which every call this module makes with `user_id` set always is.
    """
    body: dict[str, object] = {"messages": messages, "agent_id": agent_id, "infer": infer}
    if user_id is not None:
        body["user_id"] = user_id
    if immutable:
        body["immutable"] = True
    if includes is not None:
        body["includes"] = includes
    if agent_custom_instructions is not None:
        body["agent_custom_instructions"] = agent_custom_instructions
    try:
        async with _client(api_key) as client:
            response = await client.post("/v3/memories/add/", json=body)
            response.raise_for_status()
        return True
    except httpx.HTTPError:
        return False


async def search(
    *,
    api_key: str,
    query: str,
    filters: dict[str, object],
    top_k: int = 10,
    threshold: float = 0.1,
    rerank: bool = False,
) -> list[dict[str, object]] | None:
    """Semantic search over memories matching `filters`. Returns `None` on any failure — never an
    empty list, which would be indistinguishable from "searched, found nothing relevant."

    `rerank` turns on mem0's own managed reranker — its documented lever for better ordering,
    worth the extra latency for a chat-time recall call where getting the *right* memory back
    matters more than shaving off a few hundred milliseconds (see services/memory.py's own use
    of this).
    """
    body: dict[str, object] = {
        "query": query,
        "filters": filters,
        "top_k": top_k,
        "threshold": threshold,
    }
    if rerank:
        body["rerank"] = True
    try:
        async with _client(api_key) as client:
            response = await client.post("/v3/memories/search/", json=body)
            response.raise_for_status()
        return list(response.json().get("results", []))
    except httpx.HTTPError:
        return None


async def list_memories(*, api_key: str, filters: dict[str, object]) -> list[dict[str, object]] | None:
    """List every memory matching `filters`, one page (mem0 defaults to up to 100 per page, ample
    for what this platform ever lists in one screen). `None` on any failure, same reasoning as
    `search()`."""
    try:
        async with _client(api_key) as client:
            response = await client.post("/v3/memories/", json={"filters": filters, "page_size": 100})
            response.raise_for_status()
        body = response.json()
        # Tolerate either a bare list or a {"results": [...]} envelope — mem0's list endpoint
        # wasn't fully specified in what was verified against its docs before this shipped.
        return list(body.get("results", body) if isinstance(body, dict) else body)
    except httpx.HTTPError:
        return None


async def delete(*, api_key: str, memory_id: str) -> bool:
    """Permanently delete one memory by id. `False` on any failure, including one that was
    already gone — the caller (services/memory.py) confirms existence itself before calling
    this, so a `False` here means the delete call itself failed, not that it was a no-op."""
    try:
        async with _client(api_key) as client:
            response = await client.delete(f"/v1/memories/{memory_id}/")
            response.raise_for_status()
        return True
    except httpx.HTTPError:
        return False
