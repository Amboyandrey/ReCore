"""Discovering, enabling, and disabling models for chat — each one backed by a credential."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ModelNotFound
from app.models import LLMModel, ModelKind, Provider, ProviderCredential
from app.providers.base import ModelInfo
from app.providers.registry import build_provider
from app.services.credentials import decrypt_credential_key, get_credential


async def list_available_models(
    db: AsyncSession, *, workspace_id: uuid.UUID, credential_id: uuid.UUID
) -> list[ModelInfo]:
    """Ask the provider what models this credential can see — the source for the enable picker."""
    credential = await get_credential(db, workspace_id=workspace_id, credential_id=credential_id)
    api_key = decrypt_credential_key(credential)
    adapter = build_provider(credential.provider, api_key=api_key, base_url=credential.base_url)
    return await adapter.list_models()


async def enable_model(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    credential_id: uuid.UUID,
    provider_model_id: str,
    display_name: str,
    context_window: int | None,
    cost_per_mtok_in: float | None,
    cost_per_mtok_out: float | None,
    supports_vision: bool,
    kind: ModelKind = ModelKind.CHAT,
) -> LLMModel:
    """Make a credential's model available for chat (or embedding), re-enabling it if it was
    disabled before."""
    await get_credential(db, workspace_id=workspace_id, credential_id=credential_id)  # 404s if not ours

    existing = await db.scalar(
        select(LLMModel).where(
            LLMModel.credential_id == credential_id, LLMModel.provider_model_id == provider_model_id
        )
    )
    if existing is not None:
        existing.enabled = True
        existing.display_name = display_name
        existing.context_window = context_window
        existing.cost_per_mtok_in = cost_per_mtok_in
        existing.cost_per_mtok_out = cost_per_mtok_out
        existing.supports_vision = supports_vision
        existing.kind = kind
        await db.flush()
        return existing

    model = LLMModel(
        workspace_id=workspace_id,
        credential_id=credential_id,
        provider_model_id=provider_model_id,
        display_name=display_name,
        context_window=context_window,
        cost_per_mtok_in=cost_per_mtok_in,
        cost_per_mtok_out=cost_per_mtok_out,
        supports_vision=supports_vision,
        kind=kind,
    )
    db.add(model)
    await db.flush()
    return model


async def list_models(
    db: AsyncSession, *, workspace_id: uuid.UUID, kind: ModelKind = ModelKind.CHAT
) -> list[tuple[LLMModel, Provider]]:
    """List every enabled model of the given kind in the workspace, paired with its credential's
    provider — the model picker needs the provider to know which killswitch flag applies to each
    model. Defaults to CHAT so every existing caller (the chat model picker) is unaffected; the
    Knowledge settings page is the one caller that asks for `kind=EMBEDDING`."""
    stmt = (
        select(LLMModel, ProviderCredential.provider)
        .join(ProviderCredential, ProviderCredential.id == LLMModel.credential_id)
        .where(
            LLMModel.workspace_id == workspace_id, LLMModel.enabled.is_(True), LLMModel.kind == kind
        )
        .order_by(LLMModel.created_at)
    )
    return [(model, provider) for model, provider in (await db.execute(stmt)).all()]


async def disable_model(db: AsyncSession, *, workspace_id: uuid.UUID, model_id: uuid.UUID) -> None:
    """Remove a model from the enabled catalog without losing its configured pricing."""
    model = await db.scalar(
        select(LLMModel).where(LLMModel.id == model_id, LLMModel.workspace_id == workspace_id)
    )
    if model is None:
        raise ModelNotFound()
    model.enabled = False
    await db.flush()
