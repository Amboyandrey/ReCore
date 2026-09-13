"""A workspace's knowledge settings (its chosen embedding model) and connectors — websites to
crawl or sets of uploaded files, indexed in the background so an assistant can retrieve from them.
Every route here is gated behind the `knowledge` flag."""

import uuid

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.redis import get_redis
from app.core.request_ip import client_ip
from app.deps.flags import flag_gate
from app.deps.workspace import WorkspaceCtx
from app.models import Connector, LLMModel, Role
from app.schemas.knowledge import (
    ConnectorDetailOut,
    ConnectorDocumentOut,
    ConnectorOut,
    KnowledgeSettingsOut,
    KnowledgeSettingsUpdate,
    WebsiteConnectorCreate,
)
from app.schemas.model import ModelOut
from app.services.audit import record_audit
from app.services.credentials import get_credential
from app.services.flags import evaluate_flag
from app.services.jobs import enqueue_index_connector
from app.services.knowledge import (
    add_document,
    create_file_connector,
    create_website_connector,
    delete_connector,
    delete_document,
    get_connector,
    get_settings_row,
    list_connectors,
    list_documents,
    needs_reindex,
    reindex_connector,
    set_embedding_model,
)

settings_router = APIRouter(prefix="/workspaces/{workspace_id}/knowledge", tags=["knowledge"])
connectors_router = APIRouter(prefix="/workspaces/{workspace_id}/connectors", tags=["knowledge"])


async def _to_model_out(db: AsyncSession, redis: Redis, *, ctx: WorkspaceCtx, model: LLMModel) -> ModelOut:
    credential = await get_credential(db, workspace_id=ctx.workspace_id, credential_id=model.credential_id)
    provider_enabled = await evaluate_flag(
        db,
        redis,
        key=f"provider.{credential.provider.value}",
        workspace_id=ctx.workspace_id,
        user_id=ctx.user.id,
    )
    return ModelOut(
        id=model.id,
        credential_id=model.credential_id,
        provider=credential.provider,
        provider_model_id=model.provider_model_id,
        display_name=model.display_name,
        context_window=model.context_window,
        cost_per_mtok_in=model.cost_per_mtok_in,
        cost_per_mtok_out=model.cost_per_mtok_out,
        provider_enabled=bool(provider_enabled),
        supports_vision=model.supports_vision,
        kind=model.kind,
    )


async def _to_connector_out(db: AsyncSession, connector: Connector) -> ConnectorOut:
    settings_row = await get_settings_row(db, workspace_id=connector.workspace_id)
    return ConnectorOut(
        id=connector.id,
        kind=connector.kind,
        name=connector.name,
        url=connector.url,
        max_pages=connector.max_pages,
        status=connector.status,
        error=connector.error,
        embedding_model_id=connector.embedding_model_id,
        embedding_dim=connector.embedding_dim,
        document_count=connector.document_count,
        chunk_count=connector.chunk_count,
        last_indexed_at=connector.last_indexed_at,
        needs_reindex=needs_reindex(connector, settings_row),
        created_by=connector.created_by,
        created_at=connector.created_at,
    )


@settings_router.get("/settings", response_model=KnowledgeSettingsOut)
async def get_knowledge_settings_route(
    ctx: WorkspaceCtx = Depends(flag_gate("knowledge", minimum=Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> KnowledgeSettingsOut:
    row = await get_settings_row(db, workspace_id=ctx.workspace_id)
    model = await db.get(LLMModel, row.embedding_model_id) if row and row.embedding_model_id else None
    return KnowledgeSettingsOut(
        embedding_model_id=row.embedding_model_id if row else None,
        embedding_model=await _to_model_out(db, redis, ctx=ctx, model=model) if model else None,
    )


@settings_router.put("/settings", response_model=KnowledgeSettingsOut)
async def set_knowledge_settings_route(
    body: KnowledgeSettingsUpdate,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("knowledge", minimum=Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> KnowledgeSettingsOut:
    row = await set_embedding_model(
        db, workspace_id=ctx.workspace_id, model_id=body.embedding_model_id, user_id=ctx.user.id
    )
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="knowledge.settings_updated",
        target_type="knowledge_settings",
        target_id=str(ctx.workspace_id),
        ip=client_ip(request),
        metadata={"embedding_model_id": str(body.embedding_model_id) if body.embedding_model_id else None},
    )
    model = await db.get(LLMModel, row.embedding_model_id) if row.embedding_model_id else None
    return KnowledgeSettingsOut(
        embedding_model_id=row.embedding_model_id,
        embedding_model=await _to_model_out(db, redis, ctx=ctx, model=model) if model else None,
    )


@connectors_router.get("", response_model=list[ConnectorOut])
async def list_connectors_route(
    ctx: WorkspaceCtx = Depends(flag_gate("knowledge", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> list[ConnectorOut]:
    connectors = await list_connectors(db, workspace_id=ctx.workspace_id)
    return [await _to_connector_out(db, c) for c in connectors]


@connectors_router.post("", status_code=201, response_model=ConnectorOut)
async def create_website_connector_route(
    body: WebsiteConnectorCreate,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("knowledge", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> ConnectorOut:
    connector = await create_website_connector(
        db,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user.id,
        name=body.name,
        url=body.url,
        max_pages=body.max_pages,
    )
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="connector.created",
        target_type="connector",
        target_id=str(connector.id),
        ip=client_ip(request),
        metadata={"kind": "website", "url": connector.url},
    )
    await enqueue_index_connector(connector.id, ctx.workspace_id)
    return await _to_connector_out(db, connector)


@connectors_router.post("/files", status_code=201, response_model=ConnectorOut)
async def create_file_connector_route(
    request: Request,
    name: str = Form(...),
    files: list[UploadFile] = File(...),
    ctx: WorkspaceCtx = Depends(flag_gate("knowledge", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> ConnectorOut:
    connector = await create_file_connector(db, workspace_id=ctx.workspace_id, user_id=ctx.user.id, name=name)
    for file in files:
        data = await file.read()
        await add_document(
            db,
            connector=connector,
            filename=file.filename or "upload",
            mime=file.content_type or "application/octet-stream",
            data=data,
        )
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="connector.created",
        target_type="connector",
        target_id=str(connector.id),
        ip=client_ip(request),
        metadata={"kind": "file", "document_count": len(files)},
    )
    await enqueue_index_connector(connector.id, ctx.workspace_id)
    return await _to_connector_out(db, connector)


@connectors_router.get("/{connector_id}", response_model=ConnectorDetailOut)
async def get_connector_route(
    connector_id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(flag_gate("knowledge", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> ConnectorDetailOut:
    connector = await get_connector(db, workspace_id=ctx.workspace_id, connector_id=connector_id)
    out = await _to_connector_out(db, connector)
    documents = await list_documents(db, connector_id=connector.id)
    return ConnectorDetailOut(
        **out.model_dump(),
        documents=[ConnectorDocumentOut.model_validate(d, from_attributes=True) for d in documents],
    )


@connectors_router.post("/{connector_id}/documents", status_code=201, response_model=ConnectorDetailOut)
async def add_connector_document_route(
    connector_id: uuid.UUID,
    request: Request,
    files: list[UploadFile] = File(...),
    ctx: WorkspaceCtx = Depends(flag_gate("knowledge", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> ConnectorDetailOut:
    connector = await get_connector(db, workspace_id=ctx.workspace_id, connector_id=connector_id)
    for file in files:
        data = await file.read()
        await add_document(
            db,
            connector=connector,
            filename=file.filename or "upload",
            mime=file.content_type or "application/octet-stream",
            data=data,
        )
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="connector.document_added",
        target_type="connector",
        target_id=str(connector.id),
        ip=client_ip(request),
        metadata={"document_count": len(files)},
    )
    await enqueue_index_connector(connector.id, ctx.workspace_id)
    out = await _to_connector_out(db, connector)
    documents = await list_documents(db, connector_id=connector.id)
    return ConnectorDetailOut(
        **out.model_dump(),
        documents=[ConnectorDocumentOut.model_validate(d, from_attributes=True) for d in documents],
    )


@connectors_router.delete("/{connector_id}/documents/{document_id}", status_code=204)
async def delete_connector_document_route(
    connector_id: uuid.UUID,
    document_id: uuid.UUID,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("knowledge", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> None:
    await delete_document(
        db, workspace_id=ctx.workspace_id, connector_id=connector_id, document_id=document_id
    )
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="connector.document_deleted",
        target_type="connector_document",
        target_id=str(document_id),
        ip=client_ip(request),
    )


@connectors_router.post("/{connector_id}/reindex", status_code=202, response_model=ConnectorOut)
async def reindex_connector_route(
    connector_id: uuid.UUID,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("knowledge", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> ConnectorOut:
    connector = await reindex_connector(db, workspace_id=ctx.workspace_id, connector_id=connector_id)
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="connector.reindexed",
        target_type="connector",
        target_id=str(connector.id),
        ip=client_ip(request),
    )
    await enqueue_index_connector(connector.id, ctx.workspace_id)
    return await _to_connector_out(db, connector)


@connectors_router.delete("/{connector_id}", status_code=204)
async def delete_connector_route(
    connector_id: uuid.UUID,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("knowledge", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> None:
    await delete_connector(db, workspace_id=ctx.workspace_id, connector_id=connector_id)
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="connector.deleted",
        target_type="connector",
        target_id=str(connector_id),
        ip=client_ip(request),
    )
