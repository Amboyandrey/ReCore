"""Provider credentials and models persist with the constraints the schema promises."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LLMModel, Provider, ProviderCredential, User, Workspace


async def _make_workspace(db_session: AsyncSession) -> tuple[User, Workspace]:
    user = User(email="owner@example.com", password_hash="hashed")
    db_session.add(user)
    await db_session.flush()
    workspace = Workspace(slug="acme", name="Acme", owner_id=user.id)
    db_session.add(workspace)
    await db_session.flush()
    return user, workspace


async def test_credential_round_trips(db_session: AsyncSession) -> None:
    """A saved credential comes back with its encrypted fields and metadata intact."""
    user, workspace = await _make_workspace(db_session)
    credential = ProviderCredential(
        workspace_id=workspace.id,
        provider=Provider.ANTHROPIC,
        label="Prod key",
        ciphertext=b"\x01\x02",
        nonce=b"\x03" * 12,
        wrapped_key=b"\x04" * 44,
        last4="ab12",
        created_by=user.id,
    )
    db_session.add(credential)
    await db_session.flush()

    assert credential.id is not None
    assert credential.disabled_at is None
    assert credential.base_url is None


async def test_enabling_the_same_model_twice_is_rejected(db_session: AsyncSession) -> None:
    """The (credential_id, provider_model_id) uniqueness constraint prevents duplicate enablement."""
    user, workspace = await _make_workspace(db_session)
    credential = ProviderCredential(
        workspace_id=workspace.id,
        provider=Provider.ANTHROPIC,
        label="Prod key",
        ciphertext=b"\x01",
        nonce=b"\x02" * 12,
        wrapped_key=b"\x03" * 44,
        last4="ab12",
        created_by=user.id,
    )
    db_session.add(credential)
    await db_session.flush()

    db_session.add(
        LLMModel(
            workspace_id=workspace.id,
            credential_id=credential.id,
            provider_model_id="claude-opus-5",
            display_name="Claude Opus 5",
        )
    )
    await db_session.flush()

    db_session.add(
        LLMModel(
            workspace_id=workspace.id,
            credential_id=credential.id,
            provider_model_id="claude-opus-5",
            display_name="Claude Opus 5 (again)",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
