"""The arq job that runs one workflow end to end, one step at a time — no chat turn, no
conversation, just an assistant answering a rendered prompt built from the run's own input and
earlier steps' completed outputs (see app/workflows/template.py).

Runs in the worker process (see app/workers/main.py), entirely independent of any HTTP request:
it opens its own database session and re-establishes row-level-security scope after every commit
(transaction-local — the same reasoning app/workers/index_connector.py's own job follows). Any
failure in one step lands that step, and the run, in FAILED with the error recorded and every
remaining step marked SKIPPED — never an unhandled exception that would just look like a job
silently vanishing. A step marked `requires_approval` that succeeds instead parks the run at
WAITING_APPROVAL and this job simply returns; approving it (see routers/v1/workflows.py, PR 2)
re-enqueues the same job id, which resumes from `run.current_position`.
"""

import asyncio
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.db import async_session_factory, set_workspace_scope
from app.core.redis import new_redis_client
from app.models import Assistant, LLMModel, Workflow, WorkflowRun, WorkflowRunStatus, WorkflowStepStatus
from app.providers.registry import build_provider
from app.services.assistant_runtime import prepare_assistant_turn
from app.services.chat import run_assistant_task
from app.services.credentials import decrypt_credential_key, get_credential
from app.services.flags import evaluate_flag
from app.services.generations import append_event
from app.services.usage import record_usage_event
from app.services.workflows import list_step_runs
from app.workflows.template import render

# A step is itself a whole tool-calling loop (up to MAX_TOOL_ITERATIONS provider round trips,
# possibly including a delegation) — generous relative to a single chat turn's own headroom,
# since nothing here is a person waiting in real time for a reply.
WORKFLOW_STEP_TIMEOUT_SECONDS = 600
# Trimmed before going into the event stream — a step's full output is always in the database;
# this is only what a live viewer sees scroll by before the run detail page refetches the row.
_EVENT_PREVIEW_CHARS = 2000


def _serialize_invocation(record: Any) -> dict[str, Any]:
    """Turn one chat.py _ToolInvocationRecord into the plain JSON a step run's `invocations`
    column stores — the same information a chat ToolInvocation row carries, just inline rather
    than in its own table (see WorkflowStepRun's own docstring for why). Typed as `Any` rather
    than importing that private name — see chat.py's run_assistant_task, the public seam this
    worker goes through instead."""
    return {
        "tool_id": str(record.tool_id) if record.tool_id else None,
        "name": record.name,
        "arguments": record.arguments,
        "result": record.result,
        "status": record.status.value,
        "error": record.error,
        "latency_ms": record.latency_ms,
        "children": [_serialize_invocation(child) for child in record.children],
    }


def _serialize_source(source: Any) -> dict[str, Any]:
    """Turn one services/knowledge.py SourceHit into the plain JSON a step run's `sources` column
    stores — the same shape a chat MessageSource row carries."""
    return {
        "ordinal": source.ordinal,
        "connector_id": str(source.connector_id),
        "connector_name": source.connector_name,
        "document_id": str(source.document_id),
        "label": source.label,
        "url": source.url,
        "snippet": source.snippet,
        "score": source.score,
    }


async def run_workflow(ctx: dict[str, Any], run_id: str, workspace_id: str) -> None:
    """Run (or resume) one workflow run. `ctx` is arq's own per-job context, unused here."""
    del ctx
    rid, wid = uuid.UUID(run_id), uuid.UUID(workspace_id)
    stream_id = f"run:{rid}"
    redis = new_redis_client()

    try:
        async with async_session_factory() as db:
            await set_workspace_scope(db, wid)
            run = await db.get(WorkflowRun, rid)
            if run is None or run.status not in (WorkflowRunStatus.QUEUED, WorkflowRunStatus.RUNNING):
                return  # deleted, canceled, or already finished before this job started

            workflow = await db.get(Workflow, run.workflow_id)
            assert workflow is not None  # CASCADE — a run can't outlive its own workflow
            acting_user_id = run.started_by or workflow.created_by

            run.status = WorkflowRunStatus.RUNNING
            if run.started_at is None:
                run.started_at = datetime.now(UTC)
            await db.commit()
            await set_workspace_scope(db, wid)
            await append_event(redis, stream_id, "run_started", {})

            step_runs = await list_step_runs(db, run_id=rid)
            outputs: dict[str, str] = {
                prior.key: prior.output
                for prior in step_runs[: run.current_position]
                if prior.output is not None
            }

            for step_run in step_runs[run.current_position :]:
                await append_event(
                    redis, stream_id, "step_started", {"key": step_run.key, "name": step_run.name}
                )
                step_run.status = WorkflowStepStatus.RUNNING
                step_run.started_at = datetime.now(UTC)
                await db.commit()
                await set_workspace_scope(db, wid)

                error_message: str | None = None
                started_at = time.monotonic()
                try:
                    prompt = render(step_run.prompt_template, run_input=run.input, outputs=outputs)
                    step_run.prompt = prompt

                    assistant = (
                        await db.get(Assistant, step_run.assistant_id)
                        if step_run.assistant_id is not None
                        else None
                    )
                    if assistant is None:
                        raise ValueError("This step's assistant no longer exists.")

                    model_id = assistant.model_id or workflow.default_model_id
                    model = await db.get(LLMModel, model_id) if model_id is not None else None
                    if model is None:
                        raise ValueError("This step has no model to run on.")

                    credential = await get_credential(
                        db, workspace_id=wid, credential_id=model.credential_id
                    )
                    provider_enabled = await evaluate_flag(
                        db,
                        redis,
                        key=f"provider.{credential.provider.value}",
                        workspace_id=wid,
                        user_id=acting_user_id,
                    )
                    if not provider_enabled:
                        raise ValueError("This step's provider has been disabled by an administrator.")

                    adapter = build_provider(
                        credential.provider,
                        api_key=decrypt_credential_key(credential),
                        base_url=credential.base_url,
                    )
                    turn = await prepare_assistant_turn(
                        db,
                        redis,
                        workspace_id=wid,
                        user_id=acting_user_id,
                        assistant=assistant,
                        model=model,
                        credential=credential,
                        adapter=adapter,
                        query=prompt,
                    )
                    async with asyncio.timeout(WORKFLOW_STEP_TIMEOUT_SECONDS):
                        result = await run_assistant_task(
                            redis=redis,
                            stream_id=stream_id,
                            workspace_id=wid,
                            user_id=acting_user_id,
                            turn=turn,
                            task=prompt,
                            emit_deltas=False,
                        )
                    if result.error_message:
                        raise ValueError(result.error_message)
                except TimeoutError:
                    error_message = (
                        f"Step timed out after {WORKFLOW_STEP_TIMEOUT_SECONDS} seconds."
                    )
                except Exception as exc:  # noqa: BLE001 — a step must land in FAILED, never crash the job
                    error_message = str(exc)[:1000]
                else:
                    if result.stopped:
                        step_run.status = WorkflowStepStatus.FAILED
                        step_run.error = "Canceled."
                        step_run.finished_at = datetime.now(UTC)
                        run.status = WorkflowRunStatus.CANCELED
                        run.error = "Canceled."
                        run.finished_at = datetime.now(UTC)
                        await db.commit()
                        await append_event(redis, stream_id, "run_canceled", {})
                        return

                    latency_ms = int((time.monotonic() - started_at) * 1000)
                    tokens_in, tokens_out = result.input_tokens or 0, result.output_tokens or 0
                    own_cost = 0.0
                    if model.cost_per_mtok_in is not None and model.cost_per_mtok_out is not None:
                        own_cost = (
                            tokens_in / 1_000_000 * model.cost_per_mtok_in
                            + tokens_out / 1_000_000 * model.cost_per_mtok_out
                        )
                    total_cost = own_cost

                    # A delegate's own model is very often not this step's — cached by id so a
                    # step with several delegate calls on the same model doesn't reload it each
                    # time (mirrors chat.py's _run_generation persistence step exactly).
                    models_by_id: dict[uuid.UUID, LLMModel] = {model.id: model}
                    for record in result.invocations:
                        if record.usage is None:
                            continue
                        usage = record.usage
                        delegate_model = models_by_id.get(usage.model_id)
                        if delegate_model is None:
                            delegate_model = await db.get(LLMModel, usage.model_id)
                            if delegate_model is not None:
                                models_by_id[usage.model_id] = delegate_model
                        delegate_cost = 0.0
                        if (
                            delegate_model is not None
                            and delegate_model.cost_per_mtok_in is not None
                            and delegate_model.cost_per_mtok_out is not None
                        ):
                            delegate_cost = (
                                usage.tokens_in / 1_000_000 * delegate_model.cost_per_mtok_in
                                + usage.tokens_out / 1_000_000 * delegate_model.cost_per_mtok_out
                            )
                        total_cost += delegate_cost
                        await record_usage_event(
                            db,
                            workspace_id=wid,
                            user_id=acting_user_id,
                            workflow_run_id=rid,
                            model_id=usage.model_id,
                            provider=usage.provider,
                            tokens_in=usage.tokens_in,
                            tokens_out=usage.tokens_out,
                            cost_usd=delegate_cost,
                            latency_ms=usage.latency_ms,
                        )

                    await record_usage_event(
                        db,
                        workspace_id=wid,
                        user_id=acting_user_id,
                        workflow_run_id=rid,
                        model_id=model.id,
                        provider=credential.provider,
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        cost_usd=own_cost,
                        latency_ms=latency_ms,
                    )

                    step_run.output = result.text
                    step_run.tokens_in = tokens_in
                    step_run.tokens_out = tokens_out
                    step_run.cost_usd = total_cost
                    step_run.latency_ms = latency_ms
                    step_run.invocations = [_serialize_invocation(r) for r in result.invocations]
                    step_run.sources = [_serialize_source(s) for s in turn.sources]
                    step_run.finished_at = datetime.now(UTC)
                    run.cost_usd = float(run.cost_usd) + total_cost
                    outputs[step_run.key] = step_run.output

                    if step_run.requires_approval:
                        step_run.status = WorkflowStepStatus.WAITING_APPROVAL
                        run.status = WorkflowRunStatus.WAITING_APPROVAL
                        await db.commit()
                        await append_event(redis, stream_id, "run_waiting", {"key": step_run.key})
                        return

                    step_run.status = WorkflowStepStatus.SUCCEEDED
                    run.current_position += 1
                    await db.commit()
                    await set_workspace_scope(db, wid)
                    await append_event(
                        redis,
                        stream_id,
                        "step_done",
                        {"key": step_run.key, "output": step_run.output[:_EVENT_PREVIEW_CHARS]},
                    )
                    continue

                # Reached only when the try block above raised — one failed step ends the run.
                step_run.status = WorkflowStepStatus.FAILED
                step_run.error = error_message
                step_run.finished_at = datetime.now(UTC)
                run.status = WorkflowRunStatus.FAILED
                run.error = f'Step "{step_run.key}" failed: {error_message}'
                run.finished_at = datetime.now(UTC)
                remaining = step_runs[step_runs.index(step_run) + 1 :]
                for skipped in remaining:
                    skipped.status = WorkflowStepStatus.SKIPPED
                await db.commit()
                await append_event(
                    redis, stream_id, "step_failed", {"key": step_run.key, "error": error_message}
                )
                await append_event(redis, stream_id, "run_failed", {"error": run.error})
                return

            run.status = WorkflowRunStatus.SUCCEEDED
            run.output = outputs.get(step_runs[-1].key) if step_runs else None
            run.finished_at = datetime.now(UTC)
            await db.commit()
            await append_event(
                redis, stream_id, "run_done", {"output": (run.output or "")[:_EVENT_PREVIEW_CHARS]}
            )
    finally:
        await redis.aclose()
