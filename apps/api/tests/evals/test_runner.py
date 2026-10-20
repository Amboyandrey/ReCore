"""The matrix runner — one broken job becomes an error run instead of discarding every result."""

from pathlib import Path
from typing import Any

import pytest

from app.evals import runner
from app.evals.execute import TaskRun
from app.evals.models_config import ModelSpec
from app.evals.runner import run_matrix
from app.evals.tasks import Task
from app.models.provider import Provider

SPEC = ModelSpec(
    id="m",
    provider=Provider.OPENAI_COMPATIBLE,
    provider_model_id="x",
    key_env="K",
    cost_per_mtok_in=0,
    cost_per_mtok_out=0,
)


def _task(task_id: str) -> Task:
    return Task.model_validate(
        {"id": task_id, "category": "no_tool", "prompt": "p", "tools": [], "expect": {"max_calls": 0}}
    )


async def test_a_raising_job_becomes_an_error_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_run_task(task: Task, **kwargs: Any) -> TaskRun:
        if task.id == "boom":
            raise RuntimeError("harness bug")
        return TaskRun(task_id=task.id, category=task.category, model_id="m", rep=0, status="pass")

    monkeypatch.setattr(runner, "run_task", fake_run_task)
    runs = await run_matrix(
        [_task("ok"), _task("boom")],
        [SPEC],
        mode="replay",
        k=1,
        concurrency=2,
        catalog={},
        redis=None,
        cassette_dir=tmp_path,
        judge_spec=None,
        judge_counts=False,  # type: ignore[arg-type]
    )
    by_id = {r.task_id: r for r in runs}
    assert by_id["ok"].status == "pass"
    assert by_id["boom"].status == "error"
    assert any("harness bug" in r for r in by_id["boom"].reasons)
