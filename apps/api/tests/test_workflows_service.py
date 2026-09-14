"""Saving a workflow's steps and starting runs — every validation branch, and the one-active-run
cap, exercised directly against the service (see test_workflow_routes.py for the HTTP surface and
test_run_workflow.py for what actually happens once a run reaches the worker).
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.core.errors import (
    AssistantNotFound,
    ModelNotFound,
    WorkflowInvalid,
    WorkflowNotFound,
    WorkflowRunActive,
)
from app.models import LLMModel, Provider, ProviderCredential, User, WorkflowTrigger, Workspace
from app.providers.fake import VALID_KEY
from app.services.assistants import create_assistant
from app.services.workflows import (
    StepInput,
    create_workflow,
    delete_workflow,
    get_workflow,
    list_workflow_steps,
    start_run,
    update_workflow,
)


async def _workspace_with_model(db: AsyncSession) -> tuple[User, Workspace, LLMModel]:
    user = User(email=f"wfs-{uuid.uuid4().hex[:8]}@example.com", password_hash="hashed")
    db.add(user)
    await db.flush()
    workspace = Workspace(slug=f"wfs-{uuid.uuid4().hex[:8]}", name="Wfs", owner_id=user.id)
    db.add(workspace)
    await db.flush()
    secret = encrypt_secret(VALID_KEY)
    credential = ProviderCredential(
        workspace_id=workspace.id, provider=Provider.ANTHROPIC, label="Prod",
        ciphertext=secret.ciphertext, nonce=secret.nonce, wrapped_key=secret.wrapped_key,
        last4=VALID_KEY[-4:], created_by=user.id,
    )
    db.add(credential)
    await db.flush()
    model = LLMModel(
        workspace_id=workspace.id, credential_id=credential.id, provider_model_id="fake-small",
        display_name="Fake Small",
    )
    db.add(model)
    await db.flush()
    return user, workspace, model


async def test_create_workflow_requires_at_least_one_step(db: AsyncSession) -> None:
    user, workspace, model = await _workspace_with_model(db)
    with pytest.raises(WorkflowInvalid):
        await create_workflow(
            db, workspace_id=workspace.id, created_by=user.id, name="Empty", description="",
            default_model_id=None, steps=[],
        )


async def test_create_workflow_rejects_an_invalid_step_key(db: AsyncSession) -> None:
    user, workspace, model = await _workspace_with_model(db)
    assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="A", instructions="x",
        model_id=model.id, tool_ids=[],
    )
    with pytest.raises(WorkflowInvalid):
        await create_workflow(
            db, workspace_id=workspace.id, created_by=user.id, name="Wf", description="",
            default_model_id=None,
            steps=[StepInput(key="Bad Key!", name="A", assistant_id=assistant.id, prompt_template="x")],
        )


async def test_create_workflow_rejects_duplicate_step_keys(db: AsyncSession) -> None:
    user, workspace, model = await _workspace_with_model(db)
    assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="A", instructions="x",
        model_id=model.id, tool_ids=[],
    )
    with pytest.raises(WorkflowInvalid):
        await create_workflow(
            db, workspace_id=workspace.id, created_by=user.id, name="Wf", description="",
            default_model_id=None,
            steps=[
                StepInput(key="a", name="A", assistant_id=assistant.id, prompt_template="x"),
                StepInput(key="a", name="A2", assistant_id=assistant.id, prompt_template="y"),
            ],
        )


async def test_create_workflow_rejects_a_forward_step_reference(db: AsyncSession) -> None:
    """A template can only reference a step that comes *before* it in the chain."""
    user, workspace, model = await _workspace_with_model(db)
    assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="A", instructions="x",
        model_id=model.id, tool_ids=[],
    )
    with pytest.raises(WorkflowInvalid):
        await create_workflow(
            db, workspace_id=workspace.id, created_by=user.id, name="Wf", description="",
            default_model_id=None,
            steps=[
                StepInput(
                    key="a", name="A", assistant_id=assistant.id,
                    prompt_template="{{steps.b.output}}",
                ),
                StepInput(key="b", name="B", assistant_id=assistant.id, prompt_template="x"),
            ],
        )


async def test_create_workflow_rejects_a_foreign_workspace_assistant(db: AsyncSession) -> None:
    user, workspace, model = await _workspace_with_model(db)
    other_user, other_workspace, other_model = await _workspace_with_model(db)
    other_assistant = await create_assistant(
        db, workspace_id=other_workspace.id, created_by=other_user, name="Foreign",
        instructions="x", model_id=other_model.id, tool_ids=[],
    )
    with pytest.raises(AssistantNotFound):
        await create_workflow(
            db, workspace_id=workspace.id, created_by=user.id, name="Wf", description="",
            default_model_id=None,
            steps=[
                StepInput(key="a", name="A", assistant_id=other_assistant.id, prompt_template="x")
            ],
        )


async def test_create_workflow_rejects_a_foreign_workspace_default_model(db: AsyncSession) -> None:
    user, workspace, model = await _workspace_with_model(db)
    _other_user, _other_workspace, other_model = await _workspace_with_model(db)
    assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="A", instructions="x",
        model_id=model.id, tool_ids=[],
    )
    with pytest.raises(ModelNotFound):
        await create_workflow(
            db, workspace_id=workspace.id, created_by=user.id, name="Wf", description="",
            default_model_id=other_model.id,
            steps=[StepInput(key="a", name="A", assistant_id=assistant.id, prompt_template="x")],
        )


async def test_create_workflow_rejects_a_step_with_no_resolvable_model(db: AsyncSession) -> None:
    """A step's assistant has no model of its own, and the workflow sets no default either."""
    user, workspace, model = await _workspace_with_model(db)
    assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="A", instructions="x",
        model_id=None, tool_ids=[],
    )
    with pytest.raises(WorkflowInvalid):
        await create_workflow(
            db, workspace_id=workspace.id, created_by=user.id, name="Wf", description="",
            default_model_id=None,
            steps=[StepInput(key="a", name="A", assistant_id=assistant.id, prompt_template="x")],
        )


async def test_a_step_with_no_own_model_resolves_via_the_workflow_default(db: AsyncSession) -> None:
    user, workspace, model = await _workspace_with_model(db)
    assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="A", instructions="x",
        model_id=None, tool_ids=[],
    )
    workflow = await create_workflow(
        db, workspace_id=workspace.id, created_by=user.id, name="Wf", description="",
        default_model_id=model.id,
        steps=[StepInput(key="a", name="A", assistant_id=assistant.id, prompt_template="x")],
    )
    assert workflow.default_model_id == model.id


async def test_starting_a_second_run_while_one_is_active_is_rejected(db: AsyncSession) -> None:
    user, workspace, model = await _workspace_with_model(db)
    assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="A", instructions="x",
        model_id=model.id, tool_ids=[],
    )
    workflow = await create_workflow(
        db, workspace_id=workspace.id, created_by=user.id, name="Wf", description="",
        default_model_id=None,
        steps=[StepInput(key="a", name="A", assistant_id=assistant.id, prompt_template="{{input}}")],
    )
    await start_run(
        db, workspace_id=workspace.id, workflow=workflow, trigger=WorkflowTrigger.MANUAL,
        run_input="hi", started_by=user.id,
    )
    await db.commit()

    with pytest.raises(WorkflowRunActive):
        await start_run(
            db, workspace_id=workspace.id, workflow=workflow, trigger=WorkflowTrigger.MANUAL,
            run_input="hi again", started_by=user.id,
        )


async def test_update_workflow_replaces_the_whole_step_chain(db: AsyncSession) -> None:
    user, workspace, model = await _workspace_with_model(db)
    first = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="First", instructions="x",
        model_id=model.id, tool_ids=[],
    )
    second = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Second", instructions="x",
        model_id=model.id, tool_ids=[],
    )
    workflow = await create_workflow(
        db, workspace_id=workspace.id, created_by=user.id, name="Wf", description="",
        default_model_id=None,
        steps=[StepInput(key="a", name="A", assistant_id=first.id, prompt_template="{{input}}")],
    )

    await update_workflow(
        db, workspace_id=workspace.id, workflow_id=workflow.id,
        changes={
            "steps": [
                StepInput(key="b", name="B", assistant_id=second.id, prompt_template="{{input}}")
            ]
        },
    )

    steps = await list_workflow_steps(db, workflow_id=workflow.id)
    assert [s.key for s in steps] == ["b"]
    assert steps[0].assistant_id == second.id


async def test_delete_workflow_removes_it(db: AsyncSession) -> None:
    user, workspace, model = await _workspace_with_model(db)
    assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="A", instructions="x",
        model_id=model.id, tool_ids=[],
    )
    workflow = await create_workflow(
        db, workspace_id=workspace.id, created_by=user.id, name="Wf", description="",
        default_model_id=None,
        steps=[StepInput(key="a", name="A", assistant_id=assistant.id, prompt_template="x")],
    )
    await delete_workflow(db, workspace_id=workspace.id, workflow_id=workflow.id)

    with pytest.raises(WorkflowNotFound):
        await get_workflow(db, workspace_id=workspace.id, workflow_id=workflow.id)
