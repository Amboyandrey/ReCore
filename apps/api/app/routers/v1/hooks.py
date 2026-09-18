"""The inbound webhook that starts a workflow run from outside — no session, no CSRF, no
membership: possession of the URL is the credential, the same model an invitation link follows.

The URL names the workspace *and* the secret: `POST /hooks/workflows/{workspace_id}/{secret}`.
The workspace id is there for row-level security, not authorization — every workflow lookup runs
under a workspace scope (see core/db.py's set_workspace_scope), and a bare secret gives no scope
to look under. The secret alone is what has to be unguessable; a workspace id never is.

Two body shapes, chosen by content type: JSON `{"input": "..."}` for text, or multipart form
with an optional `input` field and any number of `files` parts for a run that starts from
attachments (see WorkflowRun's attachment plumbing in services/workflows.py). Rate limited per
workspace, and a workflow that's switched off, has an active run, or whose workspace has the
`workflows` flag off refuses with the same status a member would see in the app.
"""

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, ValidationError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.core.config import get_settings
from app.core.db import get_db, set_workspace_scope
from app.core.errors import RateLimited, WorkflowDisabled, WorkflowInvalid, WorkflowWebhookInvalid
from app.core.redis import get_redis
from app.models import WorkflowRunStatus, WorkflowTrigger
from app.services.attachments import save_attachment
from app.services.flags import evaluate_flag
from app.services.jobs import enqueue_run_workflow
from app.services.rate_limit import check_rate_limit
from app.services.workflows import get_workflow_by_webhook_secret, start_run

router = APIRouter(prefix="/hooks/workflows", tags=["workflows"])
settings = get_settings()

MAX_WEBHOOK_FILES = 20


class WebhookJsonBody(BaseModel):
    """The JSON shape a caller posts to start a text-only run."""

    input: str = Field(min_length=1)


class WebhookRunOut(BaseModel):
    """What a caller gets back — enough to correlate with the run in the app, nothing more."""

    run_id: uuid.UUID
    status: WorkflowRunStatus


async def _parse_json(request: Request) -> tuple[str, list[UploadFile]]:
    """Read a JSON body into the run's input text — no files come this way."""
    try:
        body = WebhookJsonBody.model_validate(await request.json())
    except (ValueError, ValidationError) as exc:
        raise WorkflowInvalid('Send JSON like {"input": "..."} or a multipart form.') from exc
    return body.input, []


async def _parse_multipart(request: Request) -> tuple[str, list[UploadFile]]:
    """Read a multipart form: an optional `input` text field plus `files` parts."""
    form = await request.form()
    raw_input = form.get("input")
    text = raw_input if isinstance(raw_input, str) else ""
    files = [f for f in form.getlist("files") if isinstance(f, UploadFile)]
    if len(files) > MAX_WEBHOOK_FILES:
        raise WorkflowInvalid(f"At most {MAX_WEBHOOK_FILES} files per run.")
    if not text.strip() and not files:
        raise WorkflowInvalid("Give the run some input text, at least one file, or both.")
    return text, files


@router.post("/{workspace_id}/{secret}", status_code=202, response_model=WebhookRunOut)
async def trigger_workflow_route(
    workspace_id: uuid.UUID,
    secret: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> WebhookRunOut:
    """Start a run of the workflow this hook URL belongs to — queued on the worker immediately."""
    allowed = await check_rate_limit(
        redis,
        f"rl:hook:{workspace_id}",
        limit=settings.webhook_rate_limit_max,
        window_seconds=settings.webhook_rate_limit_window_seconds,
    )
    if not allowed:
        raise RateLimited()

    await set_workspace_scope(db, workspace_id)
    workflow = await get_workflow_by_webhook_secret(db, workspace_id=workspace_id, secret=secret)
    # A workspace with the feature switched off shouldn't be reachable through the back door
    # either — evaluated as the workflow's author, the same actor the worker itself runs as.
    workflows_enabled = await evaluate_flag(
        db, redis, key="workflows", workspace_id=workspace_id, user_id=workflow.created_by
    )
    if not workflows_enabled:
        raise WorkflowWebhookInvalid()
    if not workflow.enabled:
        raise WorkflowDisabled()

    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        text, files = await _parse_multipart(request)
    else:
        text, files = await _parse_json(request)

    attachment_ids: list[uuid.UUID] = []
    for upload in files:
        attachment = await save_attachment(
            db,
            workspace_id=workspace_id,
            workflow_id=workflow.id,
            uploaded_by=workflow.created_by,
            filename=upload.filename or "upload",
            mime=upload.content_type or "application/octet-stream",
            data=await upload.read(),
        )
        attachment_ids.append(attachment.id)

    run = await start_run(
        db,
        workspace_id=workspace_id,
        workflow=workflow,
        trigger=WorkflowTrigger.WEBHOOK,
        run_input=text,
        started_by=None,
        attachment_ids=attachment_ids,
    )
    await enqueue_run_workflow(run.id, workspace_id)
    return WebhookRunOut(run_id=run.id, status=run.status)
