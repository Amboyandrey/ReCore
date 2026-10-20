"""Runs one task on one model through the real agent loop and classifies it into exactly one status."""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from redis.asyncio import Redis

from app.core.tracing import get_tracer
from app.evals.cassette import ReplayProvider
from app.evals.judge import Judge
from app.evals.models_config import Mode, ModelSpec, build_adapter
from app.evals.scoring import score_hard
from app.evals.tasks import Category, Task, ToolDef
from app.evals.turn import build_turn
from app.services.chat import TOOL_LOOP_EXHAUSTED, LoopResult, ToolInvocationRecord, run_assistant_task

tracer = get_tracer(__name__)

Status = Literal["pass", "fail", "error", "cassette_miss"]

# Waits between attempts of a run that ended in a provider error; replay never retries.
RETRY_DELAYS = (1.0, 2.0)

# Patched to a no-op in tests so retry backoff costs no wall time.
_sleep = asyncio.sleep


@dataclass
class TaskRun:
    task_id: str
    category: Category
    model_id: str
    rep: int
    status: Status
    checks: dict[str, bool] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    answer: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0


def _delegate_tokens(records: list[ToolInvocationRecord]) -> tuple[int, int]:
    tokens_in = tokens_out = 0
    for record in records:
        if record.usage is not None:
            tokens_in += record.usage.tokens_in
            tokens_out += record.usage.tokens_out
        child_in, child_out = _delegate_tokens(record.children)
        tokens_in, tokens_out = tokens_in + child_in, tokens_out + child_out
    return tokens_in, tokens_out


def _tokens(result: LoopResult) -> tuple[int, int]:
    delegate_in, delegate_out = _delegate_tokens(result.invocations)
    return (result.input_tokens or 0) + delegate_in, (result.output_tokens or 0) + delegate_out


def run_cost(result: LoopResult, spec: ModelSpec) -> float:
    """What the run cost at the spec's prices, delegates included since they run on the same model."""
    tokens_in, tokens_out = _tokens(result)
    return tokens_in / 1_000_000 * spec.cost_per_mtok_in + tokens_out / 1_000_000 * spec.cost_per_mtok_out


async def _attempt(
    task: Task,
    *,
    spec: ModelSpec,
    mode: Mode,
    rep: int,
    catalog: dict[str, ToolDef],
    redis: Redis,
    cassette_dir: Path,
    judge_spec: ModelSpec | None,
    judge_counts: bool,
) -> TaskRun:
    path = cassette_dir / spec.id / f"{task.id}.jsonl"
    if mode == "record":
        # A fresh recording per attempt, so a failed attempt's requests never linger in the cassette.
        path.unlink(missing_ok=True)
    adapter = build_adapter(spec, mode, path)
    run = TaskRun(task_id=task.id, category=task.category, model_id=spec.id, rep=rep, status="error")
    started = time.monotonic()
    result = await run_assistant_task(
        redis=redis,
        stream_id=f"eval:{uuid.uuid4()}",
        workspace_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        turn=build_turn(task, catalog, spec, adapter),
        task=task.prompt,
        emit_deltas=False,
    )
    run.latency_ms = int((time.monotonic() - started) * 1000)
    run.answer = result.text
    run.tokens_in, run.tokens_out = _tokens(result)
    run.cost_usd = run_cost(result, spec)

    if isinstance(adapter, ReplayProvider) and adapter.missed:
        run.status = "cassette_miss"
        run.reasons = ["no recording for a request this run made; re-record this task"]
        return run
    if result.error_message and result.error_message != TOOL_LOOP_EXHAUSTED:
        run.reasons = [result.error_message]
        return run
    scored = score_hard(task, result)
    run.status = "pass" if scored.passed else "fail"
    run.checks, run.reasons = scored.checks, scored.reasons
    rubric = task.expect.answer.judge
    if scored.passed and rubric and judge_spec is not None:
        await _apply_judge(
            run,
            task,
            rubric,
            spec=spec,
            mode=mode,
            cassette_dir=cassette_dir,
            judge_spec=judge_spec,
            judge_counts=judge_counts,
        )
    return run


async def _apply_judge(
    run: TaskRun,
    task: Task,
    rubric: str,
    *,
    spec: ModelSpec,
    mode: Mode,
    cassette_dir: Path,
    judge_spec: ModelSpec,
    judge_counts: bool,
) -> None:
    """Grade the answer; a failing verdict fails the run only when the judge's calibration lets it count."""
    path = cassette_dir / judge_spec.id / "judge" / f"{spec.id}__{task.id}.jsonl"
    if mode == "record":
        path.unlink(missing_ok=True)
    adapter = build_adapter(judge_spec, mode, path)
    verdict = await Judge(adapter, judge_spec).grade(prompt=task.prompt, rubric=rubric, answer=run.answer)
    if isinstance(adapter, ReplayProvider) and adapter.missed:
        run.status, run.reasons = "cassette_miss", ["no recording for the judge call; re-record this task"]
        return
    if verdict is None:
        run.status, run.reasons = "error", ["judge returned no verdict"]
        return
    passed, reason = verdict
    run.checks["judge"] = passed
    if not passed:
        run.reasons.append(f"judge: {reason}" if judge_counts else f"judge (not counted): {reason}")
        if judge_counts:
            run.status = "fail"


async def run_task(
    task: Task,
    *,
    spec: ModelSpec,
    mode: Mode,
    rep: int,
    catalog: dict[str, ToolDef],
    redis: Redis,
    cassette_dir: Path,
    judge_spec: ModelSpec | None = None,
    judge_counts: bool = True,
) -> TaskRun:
    """Run one task, retrying provider errors outside replay, inside an `eval.task` span."""
    attributes: dict[str, str | int] = {"eval.task_id": task.id, "eval.model": spec.id, "eval.rep": rep}
    with tracer.start_as_current_span("eval.task", attributes=attributes) as span:
        attempts = 1 if mode == "replay" else 1 + len(RETRY_DELAYS)
        for attempt in range(attempts):
            run = await _attempt(
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
            if run.status != "error" or attempt == attempts - 1:
                break
            await _sleep(RETRY_DELAYS[attempt])
        span.set_attribute("eval.status", run.status)
        return run
