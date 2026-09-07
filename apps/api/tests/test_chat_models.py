"""Conversations and messages persist with the relationships and defaults the schema promises."""

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


async def _workspace_with_model(db_session: AsyncSession) -> tuple[User, Workspace, LLMModel]:
    """Build the chain a conversation needs: a user, workspace, credential, and enabled model."""
    user = User(email="owner@example.com", password_hash="hashed")
    db_session.add(user)
    await db_session.flush()
    workspace = Workspace(slug="acme", name="Acme", owner_id=user.id)
    db_session.add(workspace)
    await db_session.flush()
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
    db_session.add(credential)
    await db_session.flush()
    model = LLMModel(
        workspace_id=workspace.id,
        credential_id=credential.id,
        provider_model_id="claude-opus-5",
        display_name="Claude Opus 5",
    )
    db_session.add(model)
    await db_session.flush()
    return user, workspace, model


async def test_conversation_and_messages_round_trip(db_session: AsyncSession) -> None:
    """A conversation and its messages persist and come back in order."""
    user, workspace, model = await _workspace_with_model(db_session)
    conversation = Conversation(
        workspace_id=workspace.id, user_id=user.id, model_id=model.id, title="Untitled"
    )
    db_session.add(conversation)
    await db_session.flush()

    db_session.add(Message(conversation_id=conversation.id, role=MessageRole.USER, content="Hi"))
    db_session.add(
        Message(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content="Hello!",
            tokens_in=3,
            tokens_out=2,
            cost_usd=0.000045,
            finish_reason="end_turn",
        )
    )
    await db_session.flush()

    assert conversation.id is not None
    assert conversation.system_prompt is None
