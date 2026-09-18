"""The arq job that runs a workflow end to end — against a real (committing) database session,
the same `db` fixture chat's own background-generation tests use, since run_workflow opens its
own session independent of any fixture's transaction (see test_index_connector.py's own docstring
for why this matters).
"""

import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.models import (
    Assistant,
    Connector,
    ConnectorChunk,
    ConnectorDocument,
    ConnectorKind,
    ConnectorStatus,
    FeatureFlag,
    FlagScope,
    KnowledgeSettings,
    LLMModel,
    ModelKind,
    Provider,
    ProviderCredential,
    UsageEvent,
    User,
    Workflow,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowStepRun,
    WorkflowStepStatus,
    WorkflowTrigger,
    Workspace,
)
from app.providers.base import (
    ChatMessage,
    Chunk,
    Done,
    StreamError,
    TextDelta,
    ToolCall,
    ToolCallRequest,
    ToolDefinition,
    Usage,
)
from app.providers.fake import FAKE_REPLY, VALID_KEY, FakeProvider
from app.services.assistants import create_assistant
from app.services.attachments import save_attachment
from app.services.flags import set_override
from app.services.workflows import StepInput, create_workflow, start_run
from app.workers.run_workflow import run_workflow


class ErrorFakeProvider(FakeProvider):
    """Fails every stream() call — for proving a step failure lands cleanly."""

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        max_tokens: int,
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[Chunk]:
        del model, max_tokens, tools
        self.last_messages = messages
        yield StreamError(message="the provider disconnected")


class DelegatingFakeProvider(FakeProvider):
    """Offers a delegate tool once, then answers normally once it's been used — the minimum
    needed to exercise a workflow step whose assistant delegates to another."""

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        max_tokens: int,
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[Chunk]:
        del model, max_tokens
        self.last_messages, self.last_tools = messages, tools
        self._stream_calls += 1
        answered = any(m.role == "tool" for m in messages)
        ask_tools = [t for t in tools if t.name.startswith("ask_")]
        if ask_tools and not answered:
            yield Usage(input_tokens=1, output_tokens=0)
            yield ToolCallRequest(
                calls=(ToolCall(id="call_1", name=ask_tools[0].name, arguments={"task": "Find X"}),)
            )
            return
        for word in FAKE_REPLY.split(" "):
            yield TextDelta(text=word + " ")
        yield Usage(input_tokens=2, output_tokens=len(FAKE_REPLY.split(" ")))
        yield Done(finish_reason="stop")


def _build_fake(provider: Provider, *, api_key: str, base_url: str | None) -> FakeProvider:
    return FakeProvider(api_key=api_key, base_url=base_url)


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every step's own model resolution in run_workflow.py builds this fake instead of a real
    provider adapter."""
    monkeypatch.setattr("app.workers.run_workflow.build_provider", _build_fake)


async def _enable_flag(db: AsyncSession, redis: Redis, *, key: str, workspace_id: uuid.UUID) -> None:
    flag = await db.scalar(select(FeatureFlag).where(FeatureFlag.key == key))
    assert flag is not None
    await set_override(
        db, redis, flag_id=flag.id, scope=FlagScope.WORKSPACE, scope_id=workspace_id, value=True
    )
    await db.commit()  # this test's `db` session must commit for the worker's own session to see it


async def _workspace_with_model(db: AsyncSession) -> tuple[User, Workspace, LLMModel]:
    """A user, workspace, credential, and one chat model — the minimum a workflow step needs to
    resolve a provider."""
    user = User(email=f"wf-{uuid.uuid4().hex[:8]}@example.com", password_hash="hashed")
    db.add(user)
    await db.flush()
    workspace = Workspace(slug=f"wf-{uuid.uuid4().hex[:8]}", name="Wf", owner_id=user.id)
    db.add(workspace)
    await db.flush()
    secret = encrypt_secret(VALID_KEY)
    credential = ProviderCredential(
        workspace_id=workspace.id,
        provider=Provider.ANTHROPIC,
        label="Prod",
        ciphertext=secret.ciphertext,
        nonce=secret.nonce,
        wrapped_key=secret.wrapped_key,
        last4=VALID_KEY[-4:],
        created_by=user.id,
    )
    db.add(credential)
    await db.flush()
    model = LLMModel(
        workspace_id=workspace.id,
        credential_id=credential.id,
        provider_model_id="fake-small",
        display_name="Fake Small",
        cost_per_mtok_in=1.0,
        cost_per_mtok_out=2.0,
    )
    db.add(model)
    await db.flush()
    return user, workspace, model


async def _refresh_run_and_steps(db: AsyncSession, run: WorkflowRun) -> list[WorkflowStepRun]:
    """The worker opens its own session — this test's own `run`/step rows must be refreshed from
    the database to see what it committed, not read back from stale in-memory state (see
    test_index_connector.py's own tests for the same lesson with a connector row)."""
    await db.refresh(run)
    return list(
        (
            await db.scalars(
                select(WorkflowStepRun)
                .where(WorkflowStepRun.run_id == run.id)
                .order_by(WorkflowStepRun.position)
            )
        ).all()
    )


async def test_two_steps_chain_output_into_the_next_prompt(
    db: AsyncSession, fake_provider: None
) -> None:
    user, workspace, model = await _workspace_with_model(db)
    research = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Research", instructions="Research.",
        model_id=model.id, tool_ids=[],
    )
    write = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Write", instructions="Write.",
        model_id=model.id, tool_ids=[],
    )
    workflow = await create_workflow(
        db, workspace_id=workspace.id, created_by=user.id, name="Pipeline", description="",
        default_model_id=None,
        steps=[
            StepInput(key="research", name="Research", assistant_id=research.id, prompt_template="{{input}}"),
            StepInput(
                key="write", name="Write", assistant_id=write.id,
                prompt_template="From: {{steps.research.output}}",
            ),
        ],
    )
    run = await start_run(
        db, workspace_id=workspace.id, workflow=workflow, trigger=WorkflowTrigger.MANUAL,
        run_input="What is the capital of France?", started_by=user.id,
    )
    await db.commit()

    await run_workflow({}, str(run.id), str(workspace.id))

    steps = await _refresh_run_and_steps(db, run)
    assert run.status == WorkflowRunStatus.SUCCEEDED
    assert run.output is not None and run.output.strip() == FAKE_REPLY
    assert [s.status for s in steps] == [WorkflowStepStatus.SUCCEEDED, WorkflowStepStatus.SUCCEEDED]
    assert steps[0].output is not None and steps[0].output.strip() == FAKE_REPLY
    assert steps[1].prompt == f"From: {steps[0].output}"

    events = (
        await db.scalars(select(UsageEvent).where(UsageEvent.workflow_run_id == run.id))
    ).all()
    assert len(events) == 2
    for event in events:
        assert event.conversation_id is None
        assert event.message_id is None
        assert event.workflow_run_id == run.id


async def test_a_failing_step_fails_the_run_and_skips_the_rest(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, workspace, model = await _workspace_with_model(db)
    ok_assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Ok", instructions="x",
        model_id=model.id, tool_ids=[],
    )
    bad_assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Bad", instructions="x",
        model_id=model.id, tool_ids=[],
    )
    third_assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Third", instructions="x",
        model_id=model.id, tool_ids=[],
    )
    workflow = await create_workflow(
        db, workspace_id=workspace.id, created_by=user.id, name="Pipeline", description="",
        default_model_id=None,
        steps=[
            StepInput(key="a", name="A", assistant_id=ok_assistant.id, prompt_template="{{input}}"),
            StepInput(key="b", name="B", assistant_id=bad_assistant.id, prompt_template="{{input}}"),
            StepInput(key="c", name="C", assistant_id=third_assistant.id, prompt_template="{{input}}"),
        ],
    )
    run = await start_run(
        db, workspace_id=workspace.id, workflow=workflow, trigger=WorkflowTrigger.MANUAL,
        run_input="hi", started_by=user.id,
    )
    await db.commit()

    calls = {"n": 0}

    def _build(provider: Provider, *, api_key: str, base_url: str | None) -> FakeProvider:
        calls["n"] += 1
        return ErrorFakeProvider(api_key=api_key) if calls["n"] == 2 else FakeProvider(api_key=api_key)

    monkeypatch.setattr("app.workers.run_workflow.build_provider", _build)
    await run_workflow({}, str(run.id), str(workspace.id))

    steps = await _refresh_run_and_steps(db, run)
    assert run.status == WorkflowRunStatus.FAILED
    assert run.error is not None and "b" in run.error
    assert steps[0].status == WorkflowStepStatus.SUCCEEDED
    assert steps[1].status == WorkflowStepStatus.FAILED
    assert steps[1].error is not None
    assert steps[2].status == WorkflowStepStatus.SKIPPED


async def test_a_deleted_assistant_fails_its_step_cleanly(
    db: AsyncSession, fake_provider: None
) -> None:
    user, workspace, model = await _workspace_with_model(db)
    assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Gone", instructions="x",
        model_id=model.id, tool_ids=[],
    )
    workflow = await create_workflow(
        db, workspace_id=workspace.id, created_by=user.id, name="Pipeline", description="",
        default_model_id=None,
        steps=[StepInput(key="a", name="A", assistant_id=assistant.id, prompt_template="{{input}}")],
    )
    run = await start_run(
        db, workspace_id=workspace.id, workflow=workflow, trigger=WorkflowTrigger.MANUAL,
        run_input="hi", started_by=user.id,
    )
    await db.commit()

    await db.execute(delete(Assistant).where(Assistant.id == assistant.id))
    await db.commit()

    await run_workflow({}, str(run.id), str(workspace.id))

    steps = await _refresh_run_and_steps(db, run)
    assert run.status == WorkflowRunStatus.FAILED
    assert steps[0].status == WorkflowStepStatus.FAILED
    assert steps[0].error is not None and "no longer exists" in steps[0].error


async def test_a_step_with_a_delegate_bills_a_separate_usage_event(
    db: AsyncSession, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, workspace, model = await _workspace_with_model(db)
    delegate_model = LLMModel(
        workspace_id=workspace.id, credential_id=model.credential_id, provider_model_id="fake-large",
        display_name="Fake Large", cost_per_mtok_in=5.0, cost_per_mtok_out=10.0,
    )
    db.add(delegate_model)
    await db.flush()
    researcher = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Researcher", instructions="x",
        model_id=delegate_model.id, tool_ids=[],
    )
    orchestrator = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Orchestrator", instructions="x",
        model_id=model.id, tool_ids=[], delegate_ids=[researcher.id],
    )
    await _enable_flag(db, redis_client, key="delegation", workspace_id=workspace.id)
    workflow = await create_workflow(
        db, workspace_id=workspace.id, created_by=user.id, name="Pipeline", description="",
        default_model_id=None,
        steps=[StepInput(key="a", name="A", assistant_id=orchestrator.id, prompt_template="{{input}}")],
    )
    run = await start_run(
        db, workspace_id=workspace.id, workflow=workflow, trigger=WorkflowTrigger.MANUAL,
        run_input="hi", started_by=user.id,
    )
    await db.commit()

    def _build(provider: Provider, *, api_key: str, base_url: str | None) -> DelegatingFakeProvider:
        return DelegatingFakeProvider(api_key=api_key, base_url=base_url)

    monkeypatch.setattr("app.workers.run_workflow.build_provider", _build)
    # A delegate resolving its own model/credential builds its adapter in assistant_runtime.py.
    monkeypatch.setattr("app.services.assistant_runtime.build_provider", _build)
    await run_workflow({}, str(run.id), str(workspace.id))

    steps = await _refresh_run_and_steps(db, run)
    assert run.status == WorkflowRunStatus.SUCCEEDED
    assert steps[0].invocations  # the delegate call was recorded
    events = (
        await db.scalars(select(UsageEvent).where(UsageEvent.workflow_run_id == run.id))
    ).all()
    assert {e.model_id for e in events} == {model.id, delegate_model.id}


async def test_a_connector_attached_step_folds_knowledge_into_the_prompt(
    db: AsyncSession, redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, workspace, model = await _workspace_with_model(db)
    embed_model = LLMModel(
        workspace_id=workspace.id, credential_id=model.credential_id, provider_model_id="fake-embed",
        display_name="Fake Embed", kind=ModelKind.EMBEDDING,
    )
    db.add(embed_model)
    await db.flush()
    db.add(
        KnowledgeSettings(workspace_id=workspace.id, embedding_model_id=embed_model.id, created_by=user.id)
    )
    connector = Connector(
        workspace_id=workspace.id, kind=ConnectorKind.FILE, name="Docs", status=ConnectorStatus.READY,
        embedding_model_id=embed_model.id, created_by=user.id,
    )
    db.add(connector)
    await db.flush()
    document = ConnectorDocument(
        workspace_id=workspace.id, connector_id=connector.id, filename="doc.txt", mime="text/plain",
        title="Doc",
    )
    db.add(document)
    await db.flush()
    db.add(
        ConnectorChunk(
            workspace_id=workspace.id, connector_id=connector.id, document_id=document.id, ordinal=0,
            content="The answer is 42.", embedding=[1.0, 0.0, 0.0],
        )
    )
    assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Bot", instructions="x",
        model_id=model.id, tool_ids=[], connector_ids=[connector.id],
    )
    await _enable_flag(db, redis_client, key="knowledge", workspace_id=workspace.id)

    workflow = await create_workflow(
        db, workspace_id=workspace.id, created_by=user.id, name="Pipeline", description="",
        default_model_id=None,
        steps=[StepInput(key="a", name="A", assistant_id=assistant.id, prompt_template="{{input}}")],
    )
    run = await start_run(
        db, workspace_id=workspace.id, workflow=workflow, trigger=WorkflowTrigger.MANUAL,
        run_input="what is the answer?", started_by=user.id,
    )
    await db.commit()

    captured: list[FakeProvider] = []

    def _build(provider: Provider, *, api_key: str, base_url: str | None) -> FakeProvider:
        instance = FakeProvider(api_key=api_key, base_url=base_url)
        captured.append(instance)
        return instance

    async def _fake_embed_texts(db: AsyncSession, *, workspace_id: uuid.UUID, texts: list[str]):  # type: ignore[no-untyped-def]
        del db, workspace_id, texts
        return [[1.0, 0.0, 0.0]], None

    monkeypatch.setattr("app.workers.run_workflow.build_provider", _build)
    monkeypatch.setattr("app.services.knowledge.embed_texts", _fake_embed_texts)
    await run_workflow({}, str(run.id), str(workspace.id))

    steps = await _refresh_run_and_steps(db, run)
    assert run.status == WorkflowRunStatus.SUCCEEDED
    assert steps[0].sources
    assert steps[0].sources[0]["label"] == "Doc"
    assert "Relevant knowledge:" in captured[0].last_messages[0].content


class _RecordingBuilder:
    """A build_provider stand-in that keeps every FakeProvider it made, in step order, so a test
    can read back exactly what each step's model was sent."""

    def __init__(self) -> None:
        self.built: list[FakeProvider] = []

    def __call__(self, provider: Provider, *, api_key: str, base_url: str | None) -> FakeProvider:
        fake = FakeProvider(api_key=api_key, base_url=base_url)
        self.built.append(fake)
        return fake


def _png_bytes() -> bytes:
    """The smallest real PNG Pillow will both write and read back."""
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (4, 4), (255, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


async def _two_step_workflow(db: AsyncSession) -> tuple[User, Workspace, LLMModel, Workflow]:
    """A workflow whose first step reads {{input}} and whose second reads only the first's output."""
    user, workspace, model = await _workspace_with_model(db)
    assistant = await create_assistant(
        db, workspace_id=workspace.id, created_by=user, name="Bot", instructions="Help.",
        model_id=model.id, tool_ids=[],
    )
    workflow = await create_workflow(
        db, workspace_id=workspace.id, created_by=user.id, name="Files", description="",
        default_model_id=None,
        steps=[
            StepInput(
                key="read", name="Read", assistant_id=assistant.id, prompt_template="Summarize: {{input}}",
            ),
            StepInput(
                key="polish", name="Polish", assistant_id=assistant.id,
                prompt_template="Polish: {{steps.read.output}}",
            ),
        ],
    )
    return user, workspace, model, workflow


async def test_run_files_reach_only_the_steps_that_read_input(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = _RecordingBuilder()
    monkeypatch.setattr("app.workers.run_workflow.build_provider", builder)
    user, workspace, _model, workflow = await _two_step_workflow(db)
    attachment = await save_attachment(
        db, workspace_id=workspace.id, workflow_id=workflow.id, uploaded_by=user.id,
        filename="notes.txt", mime="text/plain", data=b"quarterly numbers",
    )
    run = await start_run(
        db, workspace_id=workspace.id, workflow=workflow, trigger=WorkflowTrigger.MANUAL,
        run_input="", started_by=user.id, attachment_ids=[attachment.id],
    )
    await db.commit()

    await run_workflow({}, str(run.id), str(workspace.id))

    steps = await _refresh_run_and_steps(db, run)
    assert run.status == WorkflowRunStatus.SUCCEEDED
    await db.refresh(attachment)
    assert attachment.workflow_run_id == run.id

    first_user_turn = next(m for m in builder.built[0].last_messages if m.role == "user")
    assert first_user_turn.content == "Summarize: \n\n[Attached file: notes.txt]\nquarterly numbers"
    assert steps[0].prompt == "Summarize: "  # the stored prompt is the rendered template alone
    second_user_turn = next(m for m in builder.built[1].last_messages if m.role == "user")
    assert "notes.txt" not in second_user_turn.content


async def test_an_image_needs_a_vision_model_to_run(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = _RecordingBuilder()
    monkeypatch.setattr("app.workers.run_workflow.build_provider", builder)
    user, workspace, model, workflow = await _two_step_workflow(db)
    attachment = await save_attachment(
        db, workspace_id=workspace.id, workflow_id=workflow.id, uploaded_by=user.id,
        filename="chart.png", mime="image/png", data=_png_bytes(),
    )
    run = await start_run(
        db, workspace_id=workspace.id, workflow=workflow, trigger=WorkflowTrigger.MANUAL,
        run_input="", started_by=user.id, attachment_ids=[attachment.id],
    )
    await db.commit()

    await run_workflow({}, str(run.id), str(workspace.id))
    steps = await _refresh_run_and_steps(db, run)
    assert run.status == WorkflowRunStatus.FAILED
    assert steps[0].error is not None and "can't read images" in steps[0].error
    assert steps[1].status == WorkflowStepStatus.SKIPPED
    assert builder.built == []  # refused before any provider call was made

    # The same run on a vision-capable model hands the image to the step as a native part.
    model.supports_vision = True
    retry = await save_attachment(
        db, workspace_id=workspace.id, workflow_id=workflow.id, uploaded_by=user.id,
        filename="chart.png", mime="image/png", data=_png_bytes(),
    )
    run = await start_run(
        db, workspace_id=workspace.id, workflow=workflow, trigger=WorkflowTrigger.MANUAL,
        run_input="", started_by=user.id, attachment_ids=[retry.id],
    )
    await db.commit()

    await run_workflow({}, str(run.id), str(workspace.id))
    await db.refresh(run)
    assert run.status == WorkflowRunStatus.SUCCEEDED
    first_user_turn = next(m for m in builder.built[0].last_messages if m.role == "user")
    assert [image.mime for image in first_user_turn.images] == ["image/png"]
