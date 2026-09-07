"""Discover a credential's available models, enable them for chat, and disable them again."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.deps.workspace import WorkspaceCtx, require_role
from app.models import Role
from app.schemas.model import AvailableModelOut, EnableModelRequest, ModelOut
from app.services.models import disable_model, enable_model, list_available_models, list_models

credentials_router = APIRouter(
    prefix="/workspaces/{workspace_id}/credentials/{credential_id}/available-models", tags=["models"]
)
models_router = APIRouter(prefix="/workspaces/{workspace_id}/models", tags=["models"])


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
    ctx: WorkspaceCtx = Depends(require_role(Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
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
    )
    return ModelOut.model_validate(model, from_attributes=True)


@models_router.get("", response_model=list[ModelOut])
async def list_models_route(
    ctx: WorkspaceCtx = Depends(require_role(Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> list[ModelOut]:
    """List every model enabled for chat in the workspace, with its pricing."""
    models = await list_models(db, workspace_id=ctx.workspace_id)
    return [ModelOut.model_validate(m, from_attributes=True) for m in models]


@models_router.delete("/{model_id}", status_code=204)
async def disable_model_route(
    model_id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(require_role(Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a model from the workspace's enabled catalog."""
    await disable_model(db, workspace_id=ctx.workspace_id, model_id=model_id)
