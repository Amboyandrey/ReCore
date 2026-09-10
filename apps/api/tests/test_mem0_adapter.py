"""The mem0 wire adapter (app/memory/mem0.py) against a mock transport — the request shape sent
for add/search/list/delete, and that every one of them turns a transport failure into a plain
`None`/`False` return rather than an exception, exactly like web_search.py and http_tool.py do.
"""

import json

import httpx
import pytest

from app.memory import mem0


async def test_add_sends_agent_id_infer_and_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.content
        return httpx.Response(200, json={"event_id": "evt-1", "status": "PENDING"})

    monkeypatch.setattr(mem0, "_transport", httpx.MockTransport(handler))

    ok = await mem0.add(
        api_key="m0-x",
        messages=[{"role": "user", "content": "hi"}],
        agent_id="ns-1",
        user_id="user-1",
        infer=True,
    )

    assert ok is True
    body = json.loads(captured["json"])  # type: ignore[arg-type]
    assert body["agent_id"] == "ns-1"
    assert body["user_id"] == "user-1"
    assert body["infer"] is True
    assert "immutable" not in body  # only ever sent when explicitly true


async def test_add_omits_user_id_and_sets_immutable_when_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json={"event_id": "evt-2", "status": "PENDING"})

    monkeypatch.setattr(mem0, "_transport", httpx.MockTransport(handler))

    await mem0.add(
        api_key="m0-x",
        messages=[{"role": "user", "content": "fact"}],
        agent_id="ns-curated",
        infer=False,
        immutable=True,
    )

    body = json.loads(captured["body"])  # type: ignore[arg-type]
    assert "user_id" not in body
    assert body["infer"] is False
    assert body["immutable"] is True


async def test_add_sends_includes_and_agent_custom_instructions_only_when_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, json={"event_id": "evt-3", "status": "PENDING"})

    monkeypatch.setattr(mem0, "_transport", httpx.MockTransport(handler))

    await mem0.add(
        api_key="m0-x",
        messages=[{"role": "user", "content": "x"}],
        agent_id="ns-1",
        includes="preferences",
        agent_custom_instructions="capture likes and dislikes",
    )
    await mem0.add(api_key="m0-x", messages=[{"role": "user", "content": "x"}], agent_id="ns-1")

    with_hints = json.loads(bodies[0])
    without_hints = json.loads(bodies[1])
    assert with_hints["includes"] == "preferences"
    assert with_hints["agent_custom_instructions"] == "capture likes and dislikes"
    assert "includes" not in without_hints
    assert "agent_custom_instructions" not in without_hints


async def test_add_returns_false_on_a_rejected_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid api key"})

    monkeypatch.setattr(mem0, "_transport", httpx.MockTransport(handler))

    ok = await mem0.add(api_key="m0-bad", messages=[{"role": "user", "content": "x"}], agent_id="ns-1")

    assert ok is False


async def test_add_returns_false_on_a_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(mem0, "_transport", httpx.MockTransport(handler))

    ok = await mem0.add(api_key="m0-x", messages=[{"role": "user", "content": "x"}], agent_id="ns-1")

    assert ok is False


async def test_search_sends_rerank_only_when_true(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, json={"results": []})

    monkeypatch.setattr(mem0, "_transport", httpx.MockTransport(handler))

    await mem0.search(api_key="m0-x", query="q", filters={"agent_id": "ns-1"}, rerank=True)
    await mem0.search(api_key="m0-x", query="q", filters={"agent_id": "ns-1"})

    with_rerank = json.loads(bodies[0])
    without_rerank = json.loads(bodies[1])
    assert with_rerank["rerank"] is True
    assert "rerank" not in without_rerank


async def test_search_returns_the_results_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"id": "m1", "memory": "a fact"}]})

    monkeypatch.setattr(mem0, "_transport", httpx.MockTransport(handler))

    results = await mem0.search(api_key="m0-x", query="q", filters={"agent_id": "ns-1"})

    assert results == [{"id": "m1", "memory": "a fact"}]


async def test_search_returns_none_never_an_empty_list_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "server error"})

    monkeypatch.setattr(mem0, "_transport", httpx.MockTransport(handler))

    results = await mem0.search(api_key="m0-x", query="q", filters={"agent_id": "ns-1"})

    assert results is None


async def test_list_memories_tolerates_a_bare_list_response(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "m1", "memory": "a fact"}])

    monkeypatch.setattr(mem0, "_transport", httpx.MockTransport(handler))

    results = await mem0.list_memories(api_key="m0-x", filters={"agent_id": "ns-1"})

    assert results == [{"id": "m1", "memory": "a fact"}]


async def test_list_memories_tolerates_a_results_wrapped_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"id": "m1", "memory": "a fact"}]})

    monkeypatch.setattr(mem0, "_transport", httpx.MockTransport(handler))

    results = await mem0.list_memories(api_key="m0-x", filters={"agent_id": "ns-1"})

    assert results == [{"id": "m1", "memory": "a fact"}]


async def test_list_memories_returns_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "forbidden"})

    monkeypatch.setattr(mem0, "_transport", httpx.MockTransport(handler))

    results = await mem0.list_memories(api_key="m0-x", filters={"agent_id": "ns-1"})

    assert results is None


async def test_delete_returns_true_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json={"message": "deleted"})

    monkeypatch.setattr(mem0, "_transport", httpx.MockTransport(handler))

    ok = await mem0.delete(api_key="m0-x", memory_id="mem-1")

    assert ok is True
    assert captured["path"] == "/v1/memories/mem-1/"


async def test_delete_returns_false_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    monkeypatch.setattr(mem0, "_transport", httpx.MockTransport(handler))

    ok = await mem0.delete(api_key="m0-x", memory_id="mem-1")

    assert ok is False
