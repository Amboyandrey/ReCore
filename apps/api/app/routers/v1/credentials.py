"""Register, list, and remove a workspace's provider credentials."""

import uuid

from fastapi import APIRouter, Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import CredentialValidationFailed, RateLimited
from app.core.redis import get_redis
from app.core.request_ip import client_ip
from app.deps.workspace import WorkspaceCtx, require_role
from app.models import Role
from app.schemas.credential import CredentialCreate, CredentialOut
from app.services.audit import record_audit
from app.services.credentials import create_credential, delete_credential, list_credentials
from app.services.rate_limit import check_rate_limit

router = APIRouter(prefix="/workspaces/{workspace_id}/credentials", tags=["credentials"])
settings = get_settings()


@router.post("", status_code=201, response_model=CredentialOut)
async def create_credential_route(
    body: CredentialCreate,
    request: Request,
    ctx: WorkspaceCtx = Depends(require_role(Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> CredentialOut:
    """Validate a key against its provider and store it, encrypted, if the provider accepts it."""
    allowed = await check_rate_limit(
        redis,
        f"rl:credential:{ctx.workspace_id}:{client_ip(request)}",
        limit=settings.auth_rate_limit_max,
        window_seconds=settings.auth_rate_limit_window_seconds,
    )
    if not allowed:
        raise RateLimited()
    try:
        credential = await create_credential(
            db,
            workspace_id=ctx.workspace_id,
            created_by=ctx.user,
            provider=body.provider,
            label=body.label,
            api_key=body.api_key,
            base_url=body.base_url,
        )
    except CredentialValidationFailed:
        # Committed explicitly: get_db() rolls back the request's transaction on this re-raised
        # exception, which would otherwise take this audit row down with it.
        await record_audit(
            db,
            actor_id=ctx.user.id,
            workspace_id=ctx.workspace_id,
            action="credential.validation_failed",
            target_type="provider",
            target_id=body.provider.value,
            ip=client_ip(request),
        )
        await db.commit()
        raise
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="credential.created",
        target_type="credential",
        target_id=str(credential.id),
        ip=client_ip(request),
        metadata={"provider": credential.provider.value, "label": credential.label},
    )
    return CredentialOut.model_validate(credential, from_attributes=True)


@router.get("", response_model=list[CredentialOut])
async def list_credentials_route(
    ctx: WorkspaceCtx = Depends(require_role(Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> list[CredentialOut]:
    """List every credential registered for the workspace — never the keys themselves."""
    credentials = await list_credentials(db, workspace_id=ctx.workspace_id)
    return [CredentialOut.model_validate(c, from_attributes=True) for c in credentials]


@router.delete("/{credential_id}", status_code=204)
async def delete_credential_route(
    credential_id: uuid.UUID,
    request: Request,
    ctx: WorkspaceCtx = Depends(require_role(Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a credential and every model enabled through it."""
    await delete_credential(db, workspace_id=ctx.workspace_id, credential_id=credential_id)
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="credential.deleted",
        target_type="credential",
        target_id=str(credential_id),
        ip=client_ip(request),
    )
