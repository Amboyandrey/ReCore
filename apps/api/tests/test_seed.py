"""The demo seed script: idempotent account/workspace creation, and best-effort provider setup."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LLMModel, ProviderCredential, User, Workspace
from app.providers.fake import VALID_KEY, FakeProvider
from app.scripts.seed import DEMO_EMAIL, DEMO_WORKSPACE_NAME, seed


def _fake_build_provider(provider, *, api_key, base_url):
    return FakeProvider(api_key=api_key, base_url=base_url)


async def test_seed_creates_the_demo_account_and_workspace(db: AsyncSession) -> None:
    """A fresh database ends up with exactly the demo account and its workspace."""
    await seed()

    user = await db.scalar(select(User).where(User.email == DEMO_EMAIL))
    assert user is not None
    workspace = await db.scalar(select(Workspace).where(Workspace.owner_id == user.id))
    assert workspace is not None
    assert workspace.name == DEMO_WORKSPACE_NAME


async def test_seed_is_idempotent(db: AsyncSession) -> None:
    """Running it again (as every container boot does) doesn't create a second account."""
    await seed()
    await seed()

    users = (await db.scalars(select(User).where(User.email == DEMO_EMAIL))).all()
    assert len(users) == 1
    workspaces = (await db.scalars(select(Workspace).where(Workspace.owner_id == users[0].id))).all()
    assert len(workspaces) == 1


async def test_seed_registers_a_credential_when_an_api_key_is_configured(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With SEED_PROVIDER_API_KEY set, the demo workspace lands with a working model enabled."""
    monkeypatch.setattr("app.services.credentials.build_provider", _fake_build_provider)
    monkeypatch.setattr("app.services.models.build_provider", _fake_build_provider)
    monkeypatch.setenv("SEED_PROVIDER_API_KEY", VALID_KEY)
    monkeypatch.setenv("SEED_PROVIDER", "anthropic")

    await seed()

    user = await db.scalar(select(User).where(User.email == DEMO_EMAIL))
    assert user is not None
    workspace = await db.scalar(select(Workspace).where(Workspace.owner_id == user.id))
    assert workspace is not None
    credential = await db.scalar(
        select(ProviderCredential).where(ProviderCredential.workspace_id == workspace.id)
    )
    assert credential is not None
    model = await db.scalar(select(LLMModel).where(LLMModel.workspace_id == workspace.id))
    assert model is not None


async def test_seed_without_an_api_key_leaves_the_workspace_empty(db: AsyncSession) -> None:
    """No SEED_PROVIDER_API_KEY set (the default) — the account works, no credential appears."""
    await seed()

    user = await db.scalar(select(User).where(User.email == DEMO_EMAIL))
    assert user is not None
    workspace = await db.scalar(select(Workspace).where(Workspace.owner_id == user.id))
    assert workspace is not None
    credential = await db.scalar(
        select(ProviderCredential).where(ProviderCredential.workspace_id == workspace.id)
    )
    assert credential is None
