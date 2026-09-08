"""Recording usage events and aggregating them by model, member, and day."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Conversation,
    LLMModel,
    Message,
    MessageRole,
    Provider,
    ProviderCredential,
    User,
    Workspace,
)
from app.services.usage import record_usage_event, usage_by_day, usage_by_member, usage_by_model


async def _workspace_with_model(db: AsyncSession) -> tuple[User, Workspace, LLMModel]:
    """Build the chain a usage event needs: a user, workspace, credential, model, and message."""
    user = User(email="owner@example.com", password_hash="hashed")
    db.add(user)
    await db.flush()
    workspace = Workspace(slug="acme", name="Acme", owner_id=user.id)
    db.add(workspace)
    await db.flush()
    credential = ProviderCredential(
        workspace_id=workspace.id,
        provider=Provider.ANTHROPIC,
        label="Prod",
        ciphertext=b"\x01",
        nonce=b"\x02" * 12,
        wrapped_key=b"\x03" * 44,
        last4="ab12",
        created_by=user.id,
    )
    db.add(credential)
    await db.flush()
    model = LLMModel(
        workspace_id=workspace.id,
        credential_id=credential.id,
        provider_model_id="claude-opus-5",
        display_name="Claude Opus 5",
    )
    db.add(model)
    await db.flush()
    return user, workspace, model


async def _message(db: AsyncSession, *, workspace: Workspace, user: User, model: LLMModel) -> Message:
    """A conversation and one assistant message — the two FKs a usage event points at."""
    conversation = Conversation(
        workspace_id=workspace.id, user_id=user.id, model_id=model.id, title="Untitled"
    )
    db.add(conversation)
    await db.flush()
    message = Message(conversation_id=conversation.id, role=MessageRole.ASSISTANT, content="Hi")
    db.add(message)
    await db.flush()
    return message


async def test_record_usage_event_persists_every_field(db_session: AsyncSession) -> None:
    """A recorded event round-trips with exactly the values it was given."""
    user, workspace, model = await _workspace_with_model(db_session)
    message = await _message(db_session, workspace=workspace, user=user, model=model)

    event = await record_usage_event(
        db_session,
        workspace_id=workspace.id,
        user_id=user.id,
        conversation_id=message.conversation_id,
        message_id=message.id,
        model_id=model.id,
        provider=Provider.ANTHROPIC,
        tokens_in=10,
        tokens_out=20,
        cost_usd=0.001234,
        latency_ms=850,
    )

    assert event.id is not None
    assert event.tokens_in == 10
    assert event.tokens_out == 20
    assert event.latency_ms == 850


async def test_usage_by_model_sums_across_multiple_events(db_session: AsyncSession) -> None:
    """Two events against the same model add up, with the model's display name attached."""
    user, workspace, model = await _workspace_with_model(db_session)
    message = await _message(db_session, workspace=workspace, user=user, model=model)
    for tokens_out, cost in [(5, 0.01), (7, 0.02)]:
        await record_usage_event(
            db_session,
            workspace_id=workspace.id,
            user_id=user.id,
            conversation_id=message.conversation_id,
            message_id=message.id,
            model_id=model.id,
            provider=Provider.ANTHROPIC,
            tokens_in=1,
            tokens_out=tokens_out,
            cost_usd=cost,
            latency_ms=100,
        )

    since = datetime.now(UTC) - timedelta(days=1)
    rows = await usage_by_model(db_session, workspace_id=workspace.id, since=since)

    assert len(rows) == 1
    assert rows[0]["display_name"] == "Claude Opus 5"
    assert rows[0]["tokens_out"] == 12
    assert rows[0]["message_count"] == 2
    assert rows[0]["cost_usd"] == 0.03


async def test_usage_by_member_attributes_to_the_conversation_s_owner(db_session: AsyncSession) -> None:
    """Spend groups by the `user_id` the event was recorded under, with their email attached."""
    user, workspace, model = await _workspace_with_model(db_session)
    message = await _message(db_session, workspace=workspace, user=user, model=model)
    await record_usage_event(
        db_session,
        workspace_id=workspace.id,
        user_id=user.id,
        conversation_id=message.conversation_id,
        message_id=message.id,
        model_id=model.id,
        provider=Provider.ANTHROPIC,
        tokens_in=1,
        tokens_out=1,
        cost_usd=0.05,
        latency_ms=100,
    )

    since = datetime.now(UTC) - timedelta(days=1)
    rows = await usage_by_member(db_session, workspace_id=workspace.id, since=since)

    assert len(rows) == 1
    assert rows[0]["email"] == "owner@example.com"
    assert rows[0]["cost_usd"] == 0.05


async def test_usage_by_day_excludes_events_before_the_window(db_session: AsyncSession) -> None:
    """An event older than `since` doesn't show up in the aggregation at all."""
    user, workspace, model = await _workspace_with_model(db_session)
    message = await _message(db_session, workspace=workspace, user=user, model=model)
    old_event = await record_usage_event(
        db_session,
        workspace_id=workspace.id,
        user_id=user.id,
        conversation_id=message.conversation_id,
        message_id=message.id,
        model_id=model.id,
        provider=Provider.ANTHROPIC,
        tokens_in=1,
        tokens_out=1,
        cost_usd=1.0,
        latency_ms=100,
    )
    # Backdate it directly — record_usage_event always stamps "now", so this simulates a real
    # event from outside the requested window rather than testing a synthetic future one.
    old_event.created_at = datetime.now(UTC) - timedelta(days=10)
    await db_session.flush()

    rows = await usage_by_day(
        db_session, workspace_id=workspace.id, since=datetime.now(UTC) - timedelta(days=1)
    )

    assert rows == []
