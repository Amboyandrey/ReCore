"""The workflow HTTP surface: CRUD, flag gating, starting a run, reading a run back, and its SSE
event stream — indexing/running itself is stubbed out (a no-op enqueue), since these tests are
about the API surface, not the worker (see test_run_workflow.py for that).
"""

import uuid

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FeatureFlag, FlagScope, Provider
from app.providers.fake import VALID_KEY, FakeProvider
from app.services.flags import set_override
from app.services.generations import append_event

OWNER = {"email": "owner@example.com", "password": "correct horse battery staple"}


def _fake_build_provider(provider: Provider, *, api_key: str, base_url: str | None) -> FakeProvider:
    return FakeProvider(api_key=api_key, base_url=base_url)


@pytest.fixture(autouse=True)
def _fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credential validation goes through FakeProvider — no real network call happens."""
    monkeypatch.setattr("app.services.credentials.build_provider", _fake_build_provider)


@pytest.fixture(autouse=True)
def _noop_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests exercise the HTTP surface, not the worker — starting a run should queue
    nothing real."""

    async def _noop(run_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        del run_id, workspace_id

    monkeypatch.setattr("app.routers.v1.workflows.enqueue_run_workflow", _noop)


async def _enable_workflows_flag(db: AsyncSession, redis: Redis, *, workspace_id: str) -> None:
    flag = await db.scalar(select(FeatureFlag).where(FeatureFlag.key == "workflows"))
    assert flag is not None
    await set_override(
        db, redis, flag_id=flag.id, scope=FlagScope.WORKSPACE, scope_id=uuid.UUID(workspace_id), value=True
    )
    await db.commit()


async def _workspace_with_assistant(client: AsyncClient) -> tuple[str, str]:
    """Sign up as owner, create a workspace, register a credential+model, and save one assistant
    using it — the minimum a workflow step needs."""
    await client.post("/api/v1/auth/signup", json=OWNER)
    await client.post("/api/v1/auth/login", json=OWNER)
    workspace_id = str((await client.post("/api/v1/workspaces", json={"name": "Acme"})).json()["id"])
    credential = await client.post(
        f"/api/v1/workspaces/{workspace_id}/credentials",
        json={"provider": "anthropic", "label": "Prod", "api_key": VALID_KEY},
    )
    credential_id = credential.json()["id"]
    model = await client.post(
        f"/api/v1/workspaces/{workspace_id}/models",
        json={
            "credential_id": credential_id,
            "provider_model_id": "fake-small",
            "display_name": "Fake Small",
        },
    )
    model_id = model.json()["id"]
    assistant = await client.post(
        f"/api/v1/workspaces/{workspace_id}/assistants",
        json={"name": "Bot", "instructions": "x", "model_id": model_id},
    )
    return workspace_id, assistant.json()["id"]


def _one_step_body(assistant_id: str) -> dict:
    return {
        "name": "Pipeline",
        "steps": [
            {
                "key": "a",
                "name": "Step A",
                "assistant_id": assistant_id,
                "prompt_template": "{{input}}",
            }
        ],
    }


async def test_workflow_routes_404_while_the_flag_is_off(client: AsyncClient) -> None:
    workspace_id, _assistant_id = await _workspace_with_assistant(client)
    response = await client.get(f"/api/v1/workspaces/{workspace_id}/workflows")
    assert response.status_code == 404


async def test_create_get_update_delete_a_workflow(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    workspace_id, assistant_id = await _workspace_with_assistant(client)
    await _enable_workflows_flag(db, redis_client, workspace_id=workspace_id)

    created = await client.post(
        f"/api/v1/workspaces/{workspace_id}/workflows", json=_one_step_body(assistant_id)
    )
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "Pipeline"
    assert len(body["steps"]) == 1
    assert body["steps"][0]["key"] == "a"
    workflow_id = body["id"]

    fetched = await client.get(f"/api/v1/workspaces/{workspace_id}/workflows/{workflow_id}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == workflow_id

    updated = await client.put(
        f"/api/v1/workspaces/{workspace_id}/workflows/{workflow_id}", json={"name": "Renamed"}
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Renamed"
    assert len(updated.json()["steps"]) == 1  # untouched — "steps" wasn't in the update body

    listed = await client.get(f"/api/v1/workspaces/{workspace_id}/workflows")
    assert listed.status_code == 200
    assert [w["id"] for w in listed.json()] == [workflow_id]

    deleted = await client.delete(f"/api/v1/workspaces/{workspace_id}/workflows/{workflow_id}")
    assert deleted.status_code == 204
    assert (await client.get(f"/api/v1/workspaces/{workspace_id}/workflows/{workflow_id}")).status_code == 404


async def test_creating_a_workflow_with_an_invalid_chain_is_rejected(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    workspace_id, assistant_id = await _workspace_with_assistant(client)
    await _enable_workflows_flag(db, redis_client, workspace_id=workspace_id)

    body = _one_step_body(assistant_id)
    body["steps"][0]["prompt_template"] = "{{steps.missing.output}}"
    response = await client.post(f"/api/v1/workspaces/{workspace_id}/workflows", json=body)
    assert response.status_code == 400


async def test_starting_a_run_queues_it_and_returns_its_id(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    workspace_id, assistant_id = await _workspace_with_assistant(client)
    await _enable_workflows_flag(db, redis_client, workspace_id=workspace_id)
    workflow_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/workflows", json=_one_step_body(assistant_id))
    ).json()["id"]

    started = await client.post(
        f"/api/v1/workspaces/{workspace_id}/workflows/{workflow_id}/runs", json={"input": "hi"}
    )
    assert started.status_code == 202
    run = started.json()
    assert run["status"] == "queued"
    assert run["trigger"] == "manual"
    assert run["input"] == "hi"

    listed = await client.get(f"/api/v1/workspaces/{workspace_id}/workflows/{workflow_id}/runs")
    assert listed.status_code == 200
    assert [r["id"] for r in listed.json()] == [run["id"]]

    detail = await client.get(f"/api/v1/workspaces/{workspace_id}/runs/{run['id']}")
    assert detail.status_code == 200
    assert len(detail.json()["steps"]) == 1
    assert detail.json()["steps"][0]["key"] == "a"


async def test_run_events_replays_appended_events_as_sse(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    workspace_id, assistant_id = await _workspace_with_assistant(client)
    await _enable_workflows_flag(db, redis_client, workspace_id=workspace_id)
    workflow_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/workflows", json=_one_step_body(assistant_id))
    ).json()["id"]
    run_id = (
        await client.post(
            f"/api/v1/workspaces/{workspace_id}/workflows/{workflow_id}/runs", json={"input": "hi"}
        )
    ).json()["id"]

    await append_event(redis_client, f"run:{run_id}", "run_started", {})
    await append_event(redis_client, f"run:{run_id}", "run_done", {"output": "done"})

    events = []
    async with client.stream(
        "GET", f"/api/v1/workspaces/{workspace_id}/runs/{run_id}/events"
    ) as response:
        event_type = None
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event_type = line.removeprefix("event:").strip()
            elif line.startswith("data:") and event_type is not None:
                events.append(event_type)
                event_type = None

    assert events == ["run_started", "run_done"]


async def test_a_run_from_another_workspace_is_not_found(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    workspace_id, assistant_id = await _workspace_with_assistant(client)
    await _enable_workflows_flag(db, redis_client, workspace_id=workspace_id)
    workflow_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/workflows", json=_one_step_body(assistant_id))
    ).json()["id"]
    run_id = (
        await client.post(
            f"/api/v1/workspaces/{workspace_id}/workflows/{workflow_id}/runs", json={"input": "hi"}
        )
    ).json()["id"]

    other_workspace_id = str((await client.post("/api/v1/workspaces", json={"name": "Other"})).json()["id"])
    await _enable_workflows_flag(db, redis_client, workspace_id=other_workspace_id)

    response = await client.get(f"/api/v1/workspaces/{other_workspace_id}/runs/{run_id}")
    assert response.status_code == 404


async def test_starting_a_second_run_while_one_is_active_returns_409(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    workspace_id, assistant_id = await _workspace_with_assistant(client)
    await _enable_workflows_flag(db, redis_client, workspace_id=workspace_id)
    workflow_id = (
        await client.post(f"/api/v1/workspaces/{workspace_id}/workflows", json=_one_step_body(assistant_id))
    ).json()["id"]

    first = await client.post(
        f"/api/v1/workspaces/{workspace_id}/workflows/{workflow_id}/runs", json={"input": "hi"}
    )
    assert first.status_code == 202

    second = await client.post(
        f"/api/v1/workspaces/{workspace_id}/workflows/{workflow_id}/runs", json={"input": "hi again"}
    )
    assert second.status_code == 409
