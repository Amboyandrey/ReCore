"""Register, list, and remove a workspace's provider credentials."""

import uuid

from fastapi import APIRouter, Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import RateLimited
from app.core.redis import get_redis
from app.deps.workspace import WorkspaceCtx, require_role
from app.models import Role
from app.schemas.credential import CredentialCreate, CredentialOut
from app.services.credentials import create_credential, delete_credential, list_credentials
from app.services.rate_limit import check_rate_limit

router = APIRouter(prefix="/workspaces/{workspace_id}/credentials", tags=["credentials"])
settings = get_settings()


def _client_ip(request: Request) -> str:
    """Extract the caller's IP for rate-limit keys, falling back when the test client omits it."""
    return request.client.host if request.client else "unknown"


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
        f"rl:credential:{ctx.workspace_id}:{_client_ip(request)}",
        limit=settings.auth_rate_limit_max,
        window_seconds=settings.auth_rate_limit_window_seconds,
    )
    if not allowed:
        raise RateLimited()
    credential = await create_credential(
        db,
        workspace_id=ctx.workspace_id,
        created_by=ctx.user,
        provider=body.provider,
        label=body.label,
        api_key=body.api_key,
        base_url=body.base_url,
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
    ctx: WorkspaceCtx = Depends(require_role(Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a credential and every model enabled through it."""
    await delete_credential(db, workspace_id=ctx.workspace_id, credential_id=credential_id)
