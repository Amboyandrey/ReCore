"""Enqueuing background work for the arq worker (app/workers/main.py) — currently just indexing
a connector, but the one seam anything else that needs a real background job would go through
too, rather than another asyncio.create_task fire-and-forget that doesn't survive a restart."""

import uuid

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from app.core.config import get_settings

settings = get_settings()

# One pool for the whole process, created lazily on first use — arq's own client, entirely
# separate from app.core.redis's pool (that one runs with decode_responses=True for the app's own
# string-keyed uses; arq needs raw bytes for its job payloads).
_pool: ArqRedis | None = None


async def _get_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _pool


async def enqueue_index_connector(connector_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    """Queue (or re-queue) indexing for one connector.

    `_job_id` is the connector's own id, so a second enqueue — someone hits Reindex twice, or a
    document is added while one's already running — coalesces into the same job rather than
    running two indexing passes over the same connector concurrently. This only works because the
    worker is configured with `keep_result=0` (see app/workers/main.py): arq refuses a duplicate
    `_job_id` while a prior job with that id still has a stored result, which a nonzero
    keep_result would extend for up to an hour after the job most definitely finished.
    """
    pool = await _get_pool()
    await pool.enqueue_job(
        "index_connector", str(connector_id), str(workspace_id), _job_id=f"index:{connector_id}"
    )


async def enqueue_run_workflow(run_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    """Queue (or resume) one workflow run.

    `_job_id` is the run's own id — safe for the same reason enqueue_index_connector's is: a
    second enqueue for a run already QUEUED/RUNNING (a double-click, or PR 2's approve endpoint
    re-enqueuing a run whose original job already exited to park at WAITING_APPROVAL) coalesces
    into one job rather than two workers racing the same run.
    """
    pool = await _get_pool()
    await pool.enqueue_job("run_workflow", str(run_id), str(workspace_id), _job_id=f"run:{run_id}")
