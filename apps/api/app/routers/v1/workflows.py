"""Create, list, update, run, and remove a workspace's saved workflows — open to any member, same
floor an assistant or a knowledge connector already has, gated behind the `workflows` flag.
"""

import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.redis import get_redis
from app.core.request_ip import client_ip
from app.deps.flags import flag_gate
from app.deps.workspace import WorkspaceCtx
from app.models import Attachment, Role, Workflow, WorkflowRun, WorkflowStep, WorkflowTrigger
from app.schemas.attachment import AttachmentOut
from app.schemas.workflow import (
    RunAttachmentOut,
    RunCreate,
    WebhookOut,
    WorkflowCreate,
    WorkflowOut,
    WorkflowRunDetailOut,
    WorkflowRunOut,
    WorkflowStepOut,
    WorkflowStepRunOut,
    WorkflowUpdate,
)
from app.services.attachments import attachments_by_run_id, list_run_attachments, save_attachment
from app.services.audit import record_audit
from app.services.generations import read_events
from app.services.jobs import enqueue_run_workflow
from app.services.workflows import (
    StepInput,
    create_workflow,
    delete_workflow,
    disable_webhook,
    enable_webhook,
    get_run,
    get_workflow,
    list_runs,
    list_step_runs,
    list_workflow_steps,
    list_workflows,
    start_run,
    update_workflow,
)
from app.services.workflows import cancel_run as cancel_run_service

workflows_router = APIRouter(prefix="/workspaces/{workspace_id}/workflows", tags=["workflows"])
runs_router = APIRouter(prefix="/workspaces/{workspace_id}/runs", tags=["workflows"])

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _to_step_out(step: WorkflowStep) -> WorkflowStepOut:
    return WorkflowStepOut(
        id=step.id,
        key=step.key,
        name=step.name,
        assistant_id=step.assistant_id,
        prompt_template=step.prompt_template,
        requires_approval=step.requires_approval,
    )


async def _to_workflow_out(db: AsyncSession, workflow: Workflow) -> WorkflowOut:
    steps = await list_workflow_steps(db, workflow_id=workflow.id)
    return WorkflowOut(
        id=workflow.id,
        name=workflow.name,
        description=workflow.description,
        enabled=workflow.enabled,
        default_model_id=workflow.default_model_id,
        steps=[_to_step_out(s) for s in steps],
        webhook_secret=workflow.webhook_secret,
        created_by=workflow.created_by,
        created_at=workflow.created_at,
    )


def _to_run_attachment_out(attachment: Attachment) -> RunAttachmentOut:
    return RunAttachmentOut(
        id=attachment.id,
        original_filename=attachment.original_filename,
        mime=attachment.mime,
        size=attachment.size,
        extract_status=attachment.extract_status,
    )


def _to_run_out(run: WorkflowRun, attachments: list[Attachment]) -> WorkflowRunOut:
    return WorkflowRunOut(
        id=run.id,
        workflow_id=run.workflow_id,
        trigger=run.trigger,
        status=run.status,
        input=run.input,
        attachments=[_to_run_attachment_out(a) for a in attachments],
        output=run.output,
        error=run.error,
        started_by=run.started_by,
        current_position=run.current_position,
        cost_usd=run.cost_usd,
        started_at=run.started_at,
        finished_at=run.finished_at,
        created_at=run.created_at,
    )


def _step_inputs(body: WorkflowCreate | WorkflowUpdate) -> list[StepInput] | None:
    if body.steps is None:
        return None
    return [
        StepInput(
            key=s.key,
            name=s.name,
            assistant_id=s.assistant_id,
            prompt_template=s.prompt_template,
            requires_approval=s.requires_approval,
        )
        for s in body.steps
    ]


@workflows_router.post("", status_code=201, response_model=WorkflowOut)
async def create_workflow_route(
    body: WorkflowCreate,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("workflows", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> WorkflowOut:
    """Save a new workflow: a name, an ordered chain of assistant steps, and an optional default
    model any step without its own assistant model falls back to."""
    steps = _step_inputs(body)
    assert steps is not None  # WorkflowCreate.steps has min_length=1 — never None
    workflow = await create_workflow(
        db,
        workspace_id=ctx.workspace_id,
        created_by=ctx.user.id,
        name=body.name,
        description=body.description,
        default_model_id=body.default_model_id,
        steps=steps,
    )
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="workflow.created",
        target_type="workflow",
        target_id=str(workflow.id),
        ip=client_ip(request),
        metadata={"name": workflow.name, "step_count": len(steps)},
    )
    return await _to_workflow_out(db, workflow)


@workflows_router.get("", response_model=list[WorkflowOut])
async def list_workflows_route(
    ctx: WorkspaceCtx = Depends(flag_gate("workflows", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> list[WorkflowOut]:
    """List every workflow saved in the workspace."""
    workflows = await list_workflows(db, workspace_id=ctx.workspace_id)
    return [await _to_workflow_out(db, w) for w in workflows]


@workflows_router.get("/{workflow_id}", response_model=WorkflowOut)
async def get_workflow_route(
    workflow_id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(flag_gate("workflows", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> WorkflowOut:
    """Load one workflow and its steps — what the edit form reads back."""
    workflow = await get_workflow(db, workspace_id=ctx.workspace_id, workflow_id=workflow_id)
    return await _to_workflow_out(db, workflow)


@workflows_router.put("/{workflow_id}", response_model=WorkflowOut)
async def update_workflow_route(
    workflow_id: uuid.UUID,
    body: WorkflowUpdate,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("workflows", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> WorkflowOut:
    """Edit a workflow's fields or replace its whole step chain — only the fields sent change."""
    changes = body.model_dump(exclude_unset=True)
    if "steps" in changes:
        changes["steps"] = _step_inputs(body)
    workflow = await update_workflow(
        db, workspace_id=ctx.workspace_id, workflow_id=workflow_id, changes=changes
    )
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="workflow.updated",
        target_type="workflow",
        target_id=str(workflow.id),
        ip=client_ip(request),
        metadata={"changes": list(changes)},
    )
    return await _to_workflow_out(db, workflow)


@workflows_router.delete("/{workflow_id}", status_code=204)
async def delete_workflow_route(
    workflow_id: uuid.UUID,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("workflows", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Permanently remove a workflow — its steps and run history cascade with it."""
    await delete_workflow(db, workspace_id=ctx.workspace_id, workflow_id=workflow_id)
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="workflow.deleted",
        target_type="workflow",
        target_id=str(workflow_id),
        ip=client_ip(request),
    )


@workflows_router.post("/{workflow_id}/webhook", response_model=WebhookOut)
async def enable_webhook_route(
    workflow_id: uuid.UUID,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("workflows", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> WebhookOut:
    """Turn the workflow's inbound webhook on — or rotate its secret if it's already on. The
    returned secret is what the hook URL embeds (see routers/v1/hooks.py)."""
    workflow = await get_workflow(db, workspace_id=ctx.workspace_id, workflow_id=workflow_id)
    rotated = workflow.webhook_secret is not None
    secret = await enable_webhook(db, workflow=workflow)
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="workflow.webhook_rotated" if rotated else "workflow.webhook_enabled",
        target_type="workflow",
        target_id=str(workflow.id),
        ip=client_ip(request),
    )
    return WebhookOut(webhook_secret=secret)


@workflows_router.delete("/{workflow_id}/webhook", status_code=204)
async def disable_webhook_route(
    workflow_id: uuid.UUID,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("workflows", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Turn the workflow's inbound webhook off — its URL stops working immediately."""
    workflow = await get_workflow(db, workspace_id=ctx.workspace_id, workflow_id=workflow_id)
    await disable_webhook(db, workflow=workflow)
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="workflow.webhook_disabled",
        target_type="workflow",
        target_id=str(workflow.id),
        ip=client_ip(request),
    )


@workflows_router.post("/{workflow_id}/attachments", status_code=201, response_model=AttachmentOut)
async def upload_run_attachment_route(
    workflow_id: uuid.UUID,
    file: UploadFile = File(...),
    ctx: WorkspaceCtx = Depends(flag_gate("workflows", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> AttachmentOut:
    """Upload a file for a run to start from — pass its id in the run's `attachment_ids`."""
    await get_workflow(db, workspace_id=ctx.workspace_id, workflow_id=workflow_id)  # 404s if not ours
    data = await file.read()
    attachment = await save_attachment(
        db,
        workspace_id=ctx.workspace_id,
        workflow_id=workflow_id,
        uploaded_by=ctx.user.id,
        filename=file.filename or "upload",
        mime=file.content_type or "application/octet-stream",
        data=data,
    )
    return AttachmentOut.model_validate(attachment, from_attributes=True)


@workflows_router.post("/{workflow_id}/runs", status_code=202, response_model=WorkflowRunOut)
async def start_run_route(
    workflow_id: uuid.UUID,
    body: RunCreate,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("workflows", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> WorkflowRunOut:
    """Start a run from text, already-uploaded files, or both — queued on the worker immediately;
    watch it via GET .../runs/{id}/events."""
    workflow = await get_workflow(db, workspace_id=ctx.workspace_id, workflow_id=workflow_id)
    run = await start_run(
        db,
        workspace_id=ctx.workspace_id,
        workflow=workflow,
        trigger=WorkflowTrigger.MANUAL,
        run_input=body.input,
        started_by=ctx.user.id,
        attachment_ids=body.attachment_ids,
    )
    await record_audit(
        db,
        actor_id=ctx.user.id,
        workspace_id=ctx.workspace_id,
        action="workflow.run_started",
        target_type="workflow_run",
        target_id=str(run.id),
        ip=client_ip(request),
        metadata={"workflow_id": str(workflow_id), "attachment_count": len(body.attachment_ids)},
    )
    await enqueue_run_workflow(run.id, ctx.workspace_id)
    return _to_run_out(run, await list_run_attachments(db, run_id=run.id))


@workflows_router.get("/{workflow_id}/runs", response_model=list[WorkflowRunOut])
async def list_runs_route(
    workflow_id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(flag_gate("workflows", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=30, ge=1, le=100),
    before: str | None = Query(default=None),
) -> list[WorkflowRunOut]:
    """List a workflow's runs, most recently started first."""
    await get_workflow(db, workspace_id=ctx.workspace_id, workflow_id=workflow_id)  # 404s if not ours
    runs = await list_runs(db, workflow_id=workflow_id, limit=limit, before=before)
    attachments = await attachments_by_run_id(db, [r.id for r in runs])
    return [_to_run_out(r, attachments.get(r.id, [])) for r in runs]


@runs_router.get("/{run_id}", response_model=WorkflowRunDetailOut)
async def get_run_route(
    run_id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(flag_gate("workflows", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
) -> WorkflowRunDetailOut:
    """One run with all of its step runs — what the run detail page reads."""
    run = await get_run(db, workspace_id=ctx.workspace_id, run_id=run_id)
    steps = await list_step_runs(db, run_id=run.id)
    attachments = await list_run_attachments(db, run_id=run.id)
    return WorkflowRunDetailOut(
        **_to_run_out(run, attachments).model_dump(),
        steps=[WorkflowStepRunOut.model_validate(s, from_attributes=True) for s in steps],
    )


async def _sse_body(redis: Redis, stream_id: str, *, after: str = "0") -> AsyncIterator[bytes]:
    """Format a run's events as SSE — one `id:`/`event:`/`data:` block per event, same shape
    chat.py's own _sse_body follows for a chat generation's stream."""
    async for event in read_events(redis, stream_id, after=after):
        yield f"id: {event.id}\nevent: {event.type}\ndata: {json.dumps(event.data)}\n\n".encode()


@runs_router.get("/{run_id}/events")
async def run_events_route(
    run_id: uuid.UUID,
    request: Request,
    ctx: WorkspaceCtx = Depends(flag_gate("workflows", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> StreamingResponse:
    """Tail a run's event stream, from its Last-Event-ID header or an explicit ?after= param —
    the same resumable shape a chat generation's own stream follows."""
    await get_run(db, workspace_id=ctx.workspace_id, run_id=run_id)  # 404s if not ours
    after = request.headers.get("last-event-id") or request.query_params.get("after") or "0"
    return StreamingResponse(
        _sse_body(redis, f"run:{run_id}", after=after),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@runs_router.post("/{run_id}/cancel", status_code=204)
async def cancel_run_route(
    run_id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(flag_gate("workflows", minimum=Role.VIEWER)),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> None:
    """Cancel a run — queued or awaiting approval is flipped immediately, running is signaled to
    stop between steps (see services/workflows.py's cancel_run)."""
    await cancel_run_service(db, redis, workspace_id=ctx.workspace_id, run_id=run_id)
