"""Discover a credential's available models, enable them for chat, and disable them again."""

import uuid

from fastapi import APIRouter, Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.redis import get_redis
from app.core.request_ip import client_ip
from app.deps.workspace import WorkspaceCtx, require_role
from app.models import LLMModel, Provider, Role
from app.schemas.model import AvailableModelOut, EnableModelRequest, ModelOut
from app.services.audit import record_audit
from app.services.credentials import get_credential
from app.services.flags import evaluate_flag
from app.services.models import disable_model, enable_model, list_available_models, list_models

credentials_router = APIRouter(
    prefix="/workspaces/{workspace_id}/credentials/{credential_id}/available-models", tags=["models"]
)
models_router = APIRouter(prefix="/workspaces/{workspace_id}/models", tags=["models"])


async def _to_model_out(
    db: AsyncSession, redis: Redis, *, ctx: WorkspaceCtx, model: LLMModel, provider: Provider
) -> ModelOut:
    """Attach a model's provider and that provider's live killswitch state to its pricing row."""
    provider_enabled = await evaluate_flag(
        db, redis, key=f"provider.{provider.value}", workspace_id=ctx.workspace_id, user_id=ctx.user.id
    )
    return ModelOut(
        id=model.id,
        credential_id=model.credential_id,
        provider=provider,
        provider_model_id=model.provider_model_id,
        display_name=model.display_name,
        context_window=model.context_window,
        cost_per_mtok_in=model.cost_per_mtok_in,
        cost_per_mtok_out=model.cost_per_mtok_out,
        provider_enabled=bool(provider_enabled),
        supports_vision=model.supports_vision,
    )


@credentials_router.get("", response_model=list[AvailableModelOut])
async def list_available_models_route(
    credential_id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(require_role(Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> list[AvailableModelOut]:
    """Ask the provider what models this credential can see, for the enable-a-model picker."""
    models = await list_available_models(db, workspace_id=ctx.workspace_id, credential_id=credential_id)
    return [
        AvailableModelOut(id=m.id, display_name=m.display_name, context_window=m.context_window)
        for m in models
    ]


@models_router.post("", status_code=201, response_model=ModelOut)
async def enable_model_route(
    body: EnableModelRequest,
    request: Request,
    ctx: WorkspaceCtx = Depends(require_role(Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> ModelOut:
    """Make one of a credential's models available for chat."""
    model = await enable_model(
        db,
        workspace_id=ctx.workspace_id,
        credential_id=body.credential_id,
        provider_model_id=body.provider_model_id,
        display_name=body.display_name,
        context_window=body.context_window,
        cost_per_mtok_in=body.cost_per_mtok_in,
        cost_per_mtok_out=body.cost_per_mtok_out,
        supports_vision=body.supports_vision,
    )
    credential = await get_credential(db, workspace_id=ctx.workspace_id, credential_id=body.credential_id)
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="model.enabled",
        target_type="model",
        target_id=str(model.id),
        ip=client_ip(request),
        metadata={"provider_model_id": model.provider_model_id},
    )
    return await _to_model_out(db, redis, ctx=ctx, model=model, provider=credential.provider)


@models_router.get("", response_model=list[ModelOut])
async def list_models_route(
    ctx: WorkspaceCtx = Depends(require_role(Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> list[ModelOut]:
    """List every model enabled for chat in the workspace, with its pricing and whether that
    model's provider is currently switched on."""
    models = await list_models(db, workspace_id=ctx.workspace_id)
    return [
        await _to_model_out(db, redis, ctx=ctx, model=model, provider=provider) for model, provider in models
    ]


@models_router.delete("/{model_id}", status_code=204)
async def disable_model_route(
    model_id: uuid.UUID,
    request: Request,
    ctx: WorkspaceCtx = Depends(require_role(Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a model from the workspace's enabled catalog."""
    await disable_model(db, workspace_id=ctx.workspace_id, model_id=model_id)
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="model.disabled",
        target_type="model",
        target_id=str(model_id),
        ip=client_ip(request),
    )
