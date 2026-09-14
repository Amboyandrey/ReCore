"""Saving a workspace's workflows — an ordered chain of assistant steps — and starting and
tracking runs of them on the background worker (app/workers/run_workflow.py).
"""

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    AssistantNotFound,
    InvalidCursor,
    ModelNotFound,
    WorkflowInvalid,
    WorkflowNotFound,
    WorkflowRunActive,
    WorkflowRunNotFound,
)
from app.models import (
    Assistant,
    LLMModel,
    Workflow,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowStep,
    WorkflowStepRun,
    WorkflowTrigger,
)
from app.services.generations import request_stop
from app.workflows.template import referenced_step_keys

_ACTIVE_RUN_STATUSES = (
    WorkflowRunStatus.QUEUED,
    WorkflowRunStatus.RUNNING,
    WorkflowRunStatus.WAITING_APPROVAL,
)

# A step key is used both as a Postgres-unique column and inside {{steps.<key>.output}} — kept to
# a safe, readable subset rather than allowing anything a display name could contain.
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class StepInput:
    """One step as given to create_workflow/update_workflow, before it's a persisted row."""

    key: str
    name: str
    assistant_id: uuid.UUID
    prompt_template: str
    requires_approval: bool = False


async def _assert_model_in_workspace(
    db: AsyncSession, *, workspace_id: uuid.UUID, model_id: uuid.UUID
) -> None:
    exists = await db.scalar(
        select(LLMModel.id).where(LLMModel.id == model_id, LLMModel.workspace_id == workspace_id)
    )
    if exists is None:
        raise ModelNotFound()


async def _load_assistants(
    db: AsyncSession, *, workspace_id: uuid.UUID, assistant_ids: set[uuid.UUID]
) -> dict[uuid.UUID, Assistant]:
    if not assistant_ids:
        return {}
    stmt = select(Assistant).where(Assistant.id.in_(assistant_ids), Assistant.workspace_id == workspace_id)
    found = {a.id: a for a in (await db.scalars(stmt)).all()}
    if assistant_ids - set(found):
        raise AssistantNotFound()
    return found


def _validate_steps(
    steps: list[StepInput],
    *,
    assistants: dict[uuid.UUID, Assistant],
    default_model_id: uuid.UUID | None,
) -> None:
    """Every check that must hold for a workflow to actually be runnable, applied at save time
    rather than left to surface mid-run: step keys are well-formed and unique, every template
    reference names a *strictly earlier* step, and every step resolves to some model (its own
    assistant's, or the workflow's own default) before the worker ever has to find out it can't.
    """
    if not steps:
        raise WorkflowInvalid("A workflow needs at least one step.")
    seen_keys: set[str] = set()
    for step in steps:
        if not _KEY_PATTERN.match(step.key):
            raise WorkflowInvalid(
                f'Invalid step key "{step.key}" — lowercase letters, digits, and underscores '
                "only, starting with a letter."
            )
        if step.key in seen_keys:
            raise WorkflowInvalid(f'Duplicate step key "{step.key}".')
        unknown_refs = referenced_step_keys(step.prompt_template) - seen_keys
        if unknown_refs:
            raise WorkflowInvalid(
                f'Step "{step.key}" references a step that is not earlier in the chain: '
                f"{sorted(unknown_refs)}"
            )
        assistant = assistants[step.assistant_id]
        if assistant.model_id is None and default_model_id is None:
            raise WorkflowInvalid(
                f'Step "{step.key}"\'s assistant has no model of its own, and this workflow has '
                "no default model set."
            )
        seen_keys.add(step.key)


async def _set_workflow_steps(
    db: AsyncSession, *, workspace_id: uuid.UUID, workflow_id: uuid.UUID, steps: list[StepInput]
) -> None:
    """Replace a workflow's entire step chain with `steps`, in order — always the full set, never
    an incremental add/remove, same shape an assistant's tool_ids assignment already follows."""
    await db.execute(delete(WorkflowStep).where(WorkflowStep.workflow_id == workflow_id))
    for position, step in enumerate(steps):
        db.add(
            WorkflowStep(
                workspace_id=workspace_id,
                workflow_id=workflow_id,
                position=position,
                key=step.key,
                name=step.name,
                assistant_id=step.assistant_id,
                prompt_template=step.prompt_template,
                requires_approval=step.requires_approval,
            )
        )


async def create_workflow(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    created_by: uuid.UUID,
    name: str,
    description: str,
    default_model_id: uuid.UUID | None,
    steps: list[StepInput],
) -> Workflow:
    """Save a new workflow. `default_model_id` and every step's `assistant_id` must already
    belong to this workspace; see _validate_steps for the rest of what makes a chain runnable."""
    if default_model_id is not None:
        await _assert_model_in_workspace(db, workspace_id=workspace_id, model_id=default_model_id)
    assistants = await _load_assistants(
        db, workspace_id=workspace_id, assistant_ids={s.assistant_id for s in steps}
    )
    _validate_steps(steps, assistants=assistants, default_model_id=default_model_id)

    workflow = Workflow(
        workspace_id=workspace_id,
        name=name,
        description=description,
        default_model_id=default_model_id,
        created_by=created_by,
    )
    db.add(workflow)
    await db.flush()  # materializes workflow.id — needed below to attach its steps
    await _set_workflow_steps(db, workspace_id=workspace_id, workflow_id=workflow.id, steps=steps)
    await db.flush()
    return workflow


async def get_workflow(db: AsyncSession, *, workspace_id: uuid.UUID, workflow_id: uuid.UUID) -> Workflow:
    """Load one workflow by id, scoped to its workspace, or raise if it isn't there."""
    workflow = await db.scalar(
        select(Workflow).where(Workflow.id == workflow_id, Workflow.workspace_id == workspace_id)
    )
    if workflow is None:
        raise WorkflowNotFound()
    return workflow


async def list_workflows(db: AsyncSession, *, workspace_id: uuid.UUID) -> list[Workflow]:
    """List every workflow saved in the workspace, oldest first."""
    stmt = select(Workflow).where(Workflow.workspace_id == workspace_id).order_by(Workflow.created_at)
    return list((await db.scalars(stmt)).all())


async def list_workflow_steps(db: AsyncSession, *, workflow_id: uuid.UUID) -> list[WorkflowStep]:
    """A workflow's steps, in run order."""
    stmt = (
        select(WorkflowStep)
        .where(WorkflowStep.workflow_id == workflow_id)
        .order_by(WorkflowStep.position)
    )
    return list((await db.scalars(stmt)).all())


async def update_workflow(
    db: AsyncSession, *, workspace_id: uuid.UUID, workflow_id: uuid.UUID, changes: dict[str, Any]
) -> Workflow:
    """Apply only the fields present in `changes` (built with the request schema's
    `exclude_unset`) — so omitting a field leaves it untouched. Sending `steps` always replaces
    the full chain, same as `_set_workflow_steps` follows for create."""
    workflow = await get_workflow(db, workspace_id=workspace_id, workflow_id=workflow_id)
    if "name" in changes:
        workflow.name = changes["name"]
    if "description" in changes:
        workflow.description = changes["description"] or ""
    if "enabled" in changes:
        workflow.enabled = changes["enabled"]
    if "default_model_id" in changes:
        if changes["default_model_id"] is not None:
            await _assert_model_in_workspace(
                db, workspace_id=workspace_id, model_id=changes["default_model_id"]
            )
        workflow.default_model_id = changes["default_model_id"]
    if "steps" in changes:
        steps: list[StepInput] = changes["steps"]
        assistants = await _load_assistants(
            db, workspace_id=workspace_id, assistant_ids={s.assistant_id for s in steps}
        )
        _validate_steps(steps, assistants=assistants, default_model_id=workflow.default_model_id)
        await _set_workflow_steps(db, workspace_id=workspace_id, workflow_id=workflow.id, steps=steps)
    await db.flush()
    return workflow


async def delete_workflow(db: AsyncSession, *, workspace_id: uuid.UUID, workflow_id: uuid.UUID) -> None:
    """Permanently remove a workflow — its steps and run history cascade with it."""
    workflow = await get_workflow(db, workspace_id=workspace_id, workflow_id=workflow_id)
    await db.delete(workflow)
    await db.flush()


async def _has_active_run(db: AsyncSession, *, workflow_id: uuid.UUID) -> bool:
    exists = await db.scalar(
        select(WorkflowRun.id)
        .where(WorkflowRun.workflow_id == workflow_id, WorkflowRun.status.in_(_ACTIVE_RUN_STATUSES))
        .limit(1)
    )
    return exists is not None


async def start_run(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    workflow: Workflow,
    trigger: WorkflowTrigger,
    run_input: str,
    started_by: uuid.UUID | None,
) -> WorkflowRun:
    """Create a run and one PENDING step run per step, snapshotting each step's key/name/assistant
    so a later edit to the workflow's own steps never rewrites this run's history.

    Only flushes — same as every other create_* here (see create_website_connector's own
    docstring for why): the caller commits (or, inside a request, `get_db()` does) and then
    enqueues the job via services/jobs.py, exactly the same order connector indexing follows.
    """
    if await _has_active_run(db, workflow_id=workflow.id):
        raise WorkflowRunActive()
    steps = await list_workflow_steps(db, workflow_id=workflow.id)
    run = WorkflowRun(
        workspace_id=workspace_id,
        workflow_id=workflow.id,
        trigger=trigger,
        input=run_input,
        started_by=started_by,
    )
    db.add(run)
    await db.flush()  # materializes run.id — needed below to attach its step runs
    for position, step in enumerate(steps):
        db.add(
            WorkflowStepRun(
                workspace_id=workspace_id,
                run_id=run.id,
                step_id=step.id,
                position=position,
                key=step.key,
                name=step.name,
                assistant_id=step.assistant_id,
                prompt_template=step.prompt_template,
                requires_approval=step.requires_approval,
            )
        )
    await db.flush()
    return run


async def get_run(db: AsyncSession, *, workspace_id: uuid.UUID, run_id: uuid.UUID) -> WorkflowRun:
    """Load one run by id, scoped to its workspace, or raise if it isn't there."""
    run = await db.scalar(
        select(WorkflowRun).where(WorkflowRun.id == run_id, WorkflowRun.workspace_id == workspace_id)
    )
    if run is None:
        raise WorkflowRunNotFound()
    return run


async def list_runs(
    db: AsyncSession, *, workflow_id: uuid.UUID, limit: int = 30, before: str | None = None
) -> list[WorkflowRun]:
    """A workflow's runs, most recently created first — cursor-paginated the same way
    list_conversations is: `before` is a previous page's last row's `created_at`."""
    stmt = select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id)
    if before is not None:
        try:
            cursor = datetime.fromisoformat(before)
        except ValueError as exc:
            raise InvalidCursor() from exc
        stmt = stmt.where(WorkflowRun.created_at < cursor)
    stmt = stmt.order_by(WorkflowRun.created_at.desc()).limit(limit)
    return list((await db.scalars(stmt)).all())


async def list_step_runs(db: AsyncSession, *, run_id: uuid.UUID) -> list[WorkflowStepRun]:
    """One run's step runs, in run order."""
    stmt = (
        select(WorkflowStepRun).where(WorkflowStepRun.run_id == run_id).order_by(WorkflowStepRun.position)
    )
    return list((await db.scalars(stmt)).all())


async def cancel_run(
    db: AsyncSession, redis: Redis, *, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> WorkflowRun:
    """Cancel a run. A QUEUED or WAITING_APPROVAL run has no live worker listening for a stop
    signal — nothing has started yet, or the job already exited after parking for approval — so
    its status is flipped directly. A RUNNING run's job is actively polling its own stop signal
    (see chat.py's run_assistant_task / _StopSignal), so this publishes on its stream instead,
    exactly as stopping a chat generation does."""
    run = await get_run(db, workspace_id=workspace_id, run_id=run_id)
    if run.status == WorkflowRunStatus.RUNNING:
        await request_stop(redis, f"run:{run.id}")
    elif run.status in (WorkflowRunStatus.QUEUED, WorkflowRunStatus.WAITING_APPROVAL):
        run.status = WorkflowRunStatus.CANCELED
        run.finished_at = datetime.now(UTC)
        await db.flush()
    return run
