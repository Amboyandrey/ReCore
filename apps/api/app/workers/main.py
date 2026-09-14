"""The arq worker's entry point — run as `arq app.workers.main.WorkerSettings` (see the `worker`
service in infra/docker-compose.yml)."""

from arq.connections import RedisSettings
from arq.worker import func

from app.core.config import get_settings
from app.workers.index_connector import index_connector
from app.workers.run_workflow import run_workflow

settings = get_settings()


class WorkerSettings:
    functions = [
        index_connector,
        # A workflow run is potentially many steps, each its own tool-calling loop — needs far
        # more headroom than the 600s default below, which stays right for a single connector
        # index (see WORKFLOW_STEP_TIMEOUT_SECONDS in run_workflow.py for the per-step cap).
        func(run_workflow, timeout=3600),
    ]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = 2
    job_timeout = 600
    health_check_interval = 30
    # Load-bearing, not just a memory saver: arq refuses a duplicate `_job_id` while a job *or its
    # stored result* still exists, so keeping results around would silently swallow a "Reindex"
    # click (or a workflow "Run") for up to an hour after the original run actually finished (see
    # services/jobs.py).
    keep_result = 0
