"""Liveness and readiness endpoints used by Docker/orchestrator health checks."""

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.redis import get_redis

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Report that the process is up, without checking its dependencies."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, str]:
    """Report that the process can actually reach Postgres and Redis."""
    await db.execute(text("SELECT 1"))
    await redis.ping()
    return {"status": "ready", "postgres": "up", "redis": "up"}
