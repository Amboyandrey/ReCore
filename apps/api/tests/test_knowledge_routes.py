"""Knowledge settings and connector routes — CRUD, permissions, and gating, against a fake
provider (same pattern as test_models.py) and with indexing itself stubbed out (a no-op) since
these tests are about the API surface, not the worker — see test_index_connector.py for that."""

import uuid

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import EmbeddingsNotSupported
from app.models import FeatureFlag, FlagScope
from app.providers.fake import VALID_KEY, FakeProvider
from app.services.flags import set_override
from app.services.knowledge import set_embedding_model

OWNER = {"email": "owner@example.com", "password": "correct horse battery staple"}
MEMBER = {"email": "member@example.com", "password": "correct horse battery staple"}


def _fake_build_provider(provider, *, api_key, base_url):
    return FakeProvider(api_key=api_key, base_url=base_url)


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credential validation goes through FakeProvider; indexing itself is a no-op — these tests
    exercise the HTTP surface, not the worker."""
    monkeypatch.setattr("app.services.credentials.build_provider", _fake_build_provider)
    monkeypatch.setattr("app.services.models.build_provider", _fake_build_provider)

    async def _noop_enqueue(connector_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        del connector_id, workspace_id

    monkeypatch.setattr("app.routers.v1.knowledge.enqueue_index_connector", _noop_enqueue)


async def _owner_with_workspace(client: AsyncClient) -> str:
    await client.post("/api/v1/auth/signup", json=OWNER)
    await client.post("/api/v1/auth/login", json=OWNER)
    created = await client.post("/api/v1/workspaces", json={"name": "Acme"})
    return str(created.json()["id"])


async def _enable_knowledge_flag(db: AsyncSession, redis: Redis, *, workspace_id: str) -> None:
    flag = await db.scalar(select(FeatureFlag).where(FeatureFlag.key == "knowledge"))
    assert flag is not None
    await set_override(
        db, redis, flag_id=flag.id, scope=FlagScope.WORKSPACE, scope_id=uuid.UUID(workspace_id), value=True
    )
    await db.commit()


async def _embedding_model(client: AsyncClient, workspace_id: str) -> str:
    """Register an OpenAI credential (supports embeddings) and enable an embedding-kind model
    on it — the minimum needed for knowledge settings to accept a choice."""
    credential = await client.post(
        f"/api/v1/workspaces/{workspace_id}/credentials",
        json={"provider": "openai", "label": "Embeddings", "api_key": VALID_KEY},
    )
    credential_id = credential.json()["id"]
    model = await client.post(
        f"/api/v1/workspaces/{workspace_id}/models",
        json={
            "credential_id": credential_id,
            "provider_model_id": "fake-embed",
            "display_name": "Fake Embed",
            "kind": "embedding",
        },
    )
    return str(model.json()["id"])


async def test_connector_routes_404_while_the_flag_is_off(client: AsyncClient) -> None:
    workspace_id = await _owner_with_workspace(client)
    response = await client.get(f"/api/v1/workspaces/{workspace_id}/connectors")
    assert response.status_code == 404


async def test_a_plain_member_cannot_change_knowledge_settings(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    workspace_id = await _owner_with_workspace(client)
    await _enable_knowledge_flag(db, redis_client, workspace_id=workspace_id)
    model_id = await _embedding_model(client, workspace_id)

    invite = await client.post(
        f"/api/v1/workspaces/{workspace_id}/invitations", json={"email": MEMBER["email"], "role": "member"}
    )
    token = invite.json()["token"]
    await client.post("/api/v1/auth/logout")
    await client.post("/api/v1/auth/signup", json=MEMBER)
    await client.post("/api/v1/auth/login", json=MEMBER)
    await client.post(f"/api/v1/invitations/{token}/accept")

    response = await client.put(
        f"/api/v1/workspaces/{workspace_id}/knowledge/settings", json={"embedding_model_id": model_id}
    )

    assert response.status_code == 403


async def test_owner_can_set_and_read_knowledge_settings(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    workspace_id = await _owner_with_workspace(client)
    await _enable_knowledge_flag(db, redis_client, workspace_id=workspace_id)
    model_id = await _embedding_model(client, workspace_id)

    put = await client.put(
        f"/api/v1/workspaces/{workspace_id}/knowledge/settings", json={"embedding_model_id": model_id}
    )
    assert put.status_code == 200
    assert put.json()["embedding_model_id"] == model_id
    assert put.json()["embedding_model"]["id"] == model_id

    got = await client.get(f"/api/v1/workspaces/{workspace_id}/knowledge/settings")
    assert got.status_code == 200
    assert got.json()["embedding_model_id"] == model_id


async def test_setting_a_chat_kind_model_as_the_embedding_model_is_rejected(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    workspace_id = await _owner_with_workspace(client)
    await _enable_knowledge_flag(db, redis_client, workspace_id=workspace_id)
    credential = await client.post(
        f"/api/v1/workspaces/{workspace_id}/credentials",
        json={"provider": "openai", "label": "Chat", "api_key": VALID_KEY},
    )
    chat_model = await client.post(
        f"/api/v1/workspaces/{workspace_id}/models",
        json={
            "credential_id": credential.json()["id"],
            "provider_model_id": "fake-small",
            "display_name": "Fake Small",
        },
    )

    response = await client.put(
        f"/api/v1/workspaces/{workspace_id}/knowledge/settings",
        json={"embedding_model_id": chat_model.json()["id"]},
    )

    assert response.status_code == 400


async def test_set_embedding_model_rejects_an_anthropic_backed_model(db: AsyncSession) -> None:
    """Direct service-level check: even a model explicitly marked kind=embedding is rejected if
    its own provider has no embeddings API at all."""
    from app.core.crypto import encrypt_secret
    from app.models import LLMModel, ModelKind, Provider, ProviderCredential, User, Workspace

    user = User(email="svc@example.com", password_hash="hashed")
    db.add(user)
    await db.flush()
    workspace = Workspace(slug="svc-ws", name="Svc", owner_id=user.id)
    db.add(workspace)
    await db.flush()
    secret = encrypt_secret(VALID_KEY)
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
        provider_model_id="claude-embed-pretend",
        display_name="Pretend",
        kind=ModelKind.EMBEDDING,
    )
    db.add(model)
    await db.flush()

    with pytest.raises(EmbeddingsNotSupported):
        await set_embedding_model(db, workspace_id=workspace.id, model_id=model.id, user_id=user.id)


async def test_creating_a_website_connector_and_full_crud_lifecycle(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    workspace_id = await _owner_with_workspace(client)
    await _enable_knowledge_flag(db, redis_client, workspace_id=workspace_id)

    created = await client.post(
        f"/api/v1/workspaces/{workspace_id}/connectors",
        json={"name": "Docs site", "url": "https://example.com/", "max_pages": 10},
    )
    assert created.status_code == 201
    connector_id = created.json()["id"]
    assert created.json()["kind"] == "website"
    assert created.json()["status"] == "pending"
    assert created.json()["needs_reindex"] is True

    listed = await client.get(f"/api/v1/workspaces/{workspace_id}/connectors")
    assert len(listed.json()) == 1

    detail = await client.get(f"/api/v1/workspaces/{workspace_id}/connectors/{connector_id}")
    assert detail.status_code == 200
    assert detail.json()["documents"] == []

    reindexed = await client.post(f"/api/v1/workspaces/{workspace_id}/connectors/{connector_id}/reindex")
    assert reindexed.status_code == 202

    deleted = await client.delete(f"/api/v1/workspaces/{workspace_id}/connectors/{connector_id}")
    assert deleted.status_code == 204
    listed_after = await client.get(f"/api/v1/workspaces/{workspace_id}/connectors")
    assert listed_after.json() == []


async def test_creating_a_file_connector_via_multipart_upload(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    workspace_id = await _owner_with_workspace(client)
    await _enable_knowledge_flag(db, redis_client, workspace_id=workspace_id)

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/connectors/files",
        data={"name": "My files"},
        files={"files": ("notes.txt", b"Some notes to index.", "text/plain")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["kind"] == "file"

    detail = await client.get(f"/api/v1/workspaces/{workspace_id}/connectors/{body['id']}")
    assert len(detail.json()["documents"]) == 1
    assert detail.json()["documents"][0]["filename"] == "notes.txt"


async def test_website_connector_rejects_a_private_url(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    workspace_id = await _owner_with_workspace(client)
    await _enable_knowledge_flag(db, redis_client, workspace_id=workspace_id)

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/connectors",
        json={"name": "Internal", "url": "http://127.0.0.1:6379/", "max_pages": 5},
    )

    assert response.status_code == 400


async def test_a_connector_from_another_workspace_is_a_404(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    workspace_id = await _owner_with_workspace(client)
    await _enable_knowledge_flag(db, redis_client, workspace_id=workspace_id)
    created = await client.post(
        f"/api/v1/workspaces/{workspace_id}/connectors",
        json={"name": "Docs", "url": "https://example.com/", "max_pages": 5},
    )
    connector_id = created.json()["id"]

    await client.post("/api/v1/auth/logout")
    other_owner = {"email": "other@example.com", "password": "correct horse battery staple"}
    await client.post("/api/v1/auth/signup", json=other_owner)
    await client.post("/api/v1/auth/login", json=other_owner)
    other_workspace_id = str((await client.post("/api/v1/workspaces", json={"name": "Other"})).json()["id"])
    await _enable_knowledge_flag(db, redis_client, workspace_id=other_workspace_id)

    response = await client.get(f"/api/v1/workspaces/{other_workspace_id}/connectors/{connector_id}")

    assert response.status_code == 404


async def test_connector_limit_is_enforced(
    client: AsyncClient, db: AsyncSession, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.knowledge.MAX_CONNECTORS_PER_WORKSPACE", 1)
    workspace_id = await _owner_with_workspace(client)
    await _enable_knowledge_flag(db, redis_client, workspace_id=workspace_id)
    first = await client.post(
        f"/api/v1/workspaces/{workspace_id}/connectors",
        json={"name": "First", "url": "https://example.com/", "max_pages": 5},
    )
    assert first.status_code == 201

    second = await client.post(
        f"/api/v1/workspaces/{workspace_id}/connectors",
        json={"name": "Second", "url": "https://example.com/", "max_pages": 5},
    )

    assert second.status_code == 409


async def test_models_endpoint_filters_by_kind(
    client: AsyncClient, db: AsyncSession, redis_client: Redis
) -> None:
    del db, redis_client
    workspace_id = await _owner_with_workspace(client)
    credential = await client.post(
        f"/api/v1/workspaces/{workspace_id}/credentials",
        json={"provider": "openai", "label": "Prod", "api_key": VALID_KEY},
    )
    credential_id = credential.json()["id"]
    await client.post(
        f"/api/v1/workspaces/{workspace_id}/models",
        json={
            "credential_id": credential_id,
            "provider_model_id": "fake-small",
            "display_name": "Chat model",
        },
    )
    await client.post(
        f"/api/v1/workspaces/{workspace_id}/models",
        json={
            "credential_id": credential_id,
            "provider_model_id": "fake-embed",
            "display_name": "Embed model",
            "kind": "embedding",
        },
    )

    chat_models = await client.get(f"/api/v1/workspaces/{workspace_id}/models")
    embedding_models = await client.get(
        f"/api/v1/workspaces/{workspace_id}/models", params={"kind": "embedding"}
    )

    assert [m["display_name"] for m in chat_models.json()] == ["Chat model"]
    assert [m["display_name"] for m in embedding_models.json()] == ["Embed model"]
