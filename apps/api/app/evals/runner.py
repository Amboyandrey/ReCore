"""Runs the tasks x models matrix with bounded concurrency."""

import asyncio
from pathlib import Path

from redis.asyncio import Redis

from app.evals.execute import TaskRun, run_task
from app.evals.models_config import Mode, ModelSpec
from app.evals.tasks import Task, ToolDef


async def run_matrix(
    tasks: list[Task],
    models: list[ModelSpec],
    *,
    mode: Mode,
    k: int,
    concurrency: int,
    catalog: dict[str, ToolDef],
    redis: Redis,
    cassette_dir: Path,
    judge_spec: ModelSpec | None,
    judge_counts: bool,
) -> list[TaskRun]:
    """Every task on every model, k times each (once when recording), in a stable order."""
    reps = 1 if mode == "record" else k
    semaphore = asyncio.Semaphore(concurrency)

    async def one(task: Task, spec: ModelSpec, rep: int) -> TaskRun:
        async with semaphore:
            return await run_task(
                task,
                spec=spec,
                mode=mode,
                rep=rep,
                catalog=catalog,
                redis=redis,
                cassette_dir=cassette_dir,
                judge_spec=judge_spec,
                judge_counts=judge_counts,
            )

    jobs = [one(task, spec, rep) for spec in models for task in tasks for rep in range(reps)]
    return list(await asyncio.gather(*jobs))
