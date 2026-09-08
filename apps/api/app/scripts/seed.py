"""Seeds one demo account and workspace — idempotent, run on every container boot.

This is the "fresh clone to a populated platform in one command" half of the hardening phase's
demo: `docker compose up --build` alone lands on a real, sign-in-able account with a workspace
already waiting, no manual setup step required first.

Registering a real provider credential needs a real API key, which this repo obviously can't
ship — set SEED_PROVIDER_API_KEY (and optionally SEED_PROVIDER, default "anthropic") in the
environment to have the seed also validate that key, enable the first model it reports, and
leave the demo workspace ready to chat immediately. Without it, the demo account still works;
it just lands on the empty "add a provider" state Settings -> Providers already explains.
"""

import asyncio
import os
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory
from app.core.errors import EmailAlreadyRegistered
from app.core.logging import configure_logging, get_logger
from app.models import Provider, User
from app.services.auth import signup
from app.services.credentials import create_credential
from app.services.models import enable_model, list_available_models
from app.services.workspaces import create_workspace

logger = get_logger(__name__)

DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "ReCoreDemo123!"
DEMO_WORKSPACE_NAME = "Demo Workspace"


async def _seed_provider(db: AsyncSession, *, workspace_id: uuid.UUID, created_by: User) -> None:
    """Best-effort: register SEED_PROVIDER_API_KEY as a credential and enable one model.

    Never allowed to fail the boot — a bad or rate-limited key here shouldn't stop the app from
    starting, it should just leave the workspace exactly as empty as it'd be without this env
    var set at all.
    """
    api_key = os.environ.get("SEED_PROVIDER_API_KEY")
    if not api_key:
        logger.info("seed.provider_skipped", reason="SEED_PROVIDER_API_KEY not set")
        return
    provider_name = os.environ.get("SEED_PROVIDER", "anthropic")
    try:
        provider = Provider(provider_name)
        credential = await create_credential(
            db,
            workspace_id=workspace_id,
            created_by=created_by,
            provider=provider,
            label="Seeded",
            api_key=api_key,
            base_url=os.environ.get("SEED_PROVIDER_BASE_URL"),
        )
        available = await list_available_models(
            db, workspace_id=workspace_id, credential_id=credential.id
        )
        if not available:
            logger.warning("seed.no_models_reported", provider=provider_name)
            return
        model = available[0]
        await enable_model(
            db,
            workspace_id=workspace_id,
            credential_id=credential.id,
            provider_model_id=model.id,
            display_name=model.display_name,
            context_window=model.context_window,
            cost_per_mtok_in=None,
            cost_per_mtok_out=None,
        )
        await db.commit()
        logger.info("seed.provider_ready", provider=provider_name, model=model.id)
    except Exception:  # noqa: BLE001 — seeding a live key must never block the app from booting
        await db.rollback()
        logger.warning("seed.provider_failed", provider=provider_name, exc_info=True)


async def seed() -> None:
    """Create the demo user and workspace if they don't already exist."""
    async with async_session_factory() as db:
        try:
            user = await signup(db, email=DEMO_EMAIL, password=DEMO_PASSWORD)
        except EmailAlreadyRegistered:
            # This container has booted (and seeded) before — nothing left to do.
            await db.rollback()
            logger.info("seed.already_done", email=DEMO_EMAIL)
            return

        workspace = await create_workspace(db, owner=user, name=DEMO_WORKSPACE_NAME)
        await db.commit()
        logger.info("seed.account_created", email=DEMO_EMAIL, workspace=workspace.slug)

        await _seed_provider(db, workspace_id=workspace.id, created_by=user)


def main() -> None:
    """Entry point for `python -m app.scripts.seed`."""
    configure_logging(debug=False)
    asyncio.run(seed())


if __name__ == "__main__":
    main()
