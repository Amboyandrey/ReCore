"""Registering, listing, and removing a workspace's provider credentials.

The plaintext API key exists only for the moment it takes to validate and encrypt it — it's
never logged and never appears in any response after the credential is created.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import EncryptedSecret, decrypt_secret, encrypt_secret
from app.core.errors import CredentialNotFound, CredentialValidationFailed
from app.core.ssrf import assert_safe_base_url
from app.models import Provider, ProviderCredential, User
from app.providers.registry import build_provider


async def create_credential(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    created_by: User,
    provider: Provider,
    label: str,
    api_key: str,
    base_url: str | None,
) -> ProviderCredential:
    """Validate a key against its provider, then store it encrypted — never store one unvalidated."""
    if base_url:
        assert_safe_base_url(base_url)

    adapter = build_provider(provider, api_key=api_key, base_url=base_url)
    check = await adapter.validate()
    if not check.ok:
        raise CredentialValidationFailed(check.detail)

    secret = encrypt_secret(api_key)
    credential = ProviderCredential(
        workspace_id=workspace_id,
        provider=provider,
        label=label,
        base_url=base_url,
        ciphertext=secret.ciphertext,
        nonce=secret.nonce,
        wrapped_key=secret.wrapped_key,
        last4=api_key[-4:],
        created_by=created_by.id,
    )
    db.add(credential)
    await db.flush()
    return credential


async def list_credentials(db: AsyncSession, *, workspace_id: uuid.UUID) -> list[ProviderCredential]:
    """List every credential registered for a workspace, most recently added first."""
    stmt = (
        select(ProviderCredential)
        .where(ProviderCredential.workspace_id == workspace_id)
        .order_by(ProviderCredential.created_at.desc())
    )
    return list((await db.scalars(stmt)).all())


async def get_credential(
    db: AsyncSession, *, workspace_id: uuid.UUID, credential_id: uuid.UUID
) -> ProviderCredential:
    """Load one credential by id, scoped to its workspace, or raise if it isn't there."""
    credential = await db.scalar(
        select(ProviderCredential).where(
            ProviderCredential.id == credential_id, ProviderCredential.workspace_id == workspace_id
        )
    )
    if credential is None:
        raise CredentialNotFound()
    return credential


async def delete_credential(
    db: AsyncSession, *, workspace_id: uuid.UUID, credential_id: uuid.UUID
) -> None:
    """Remove a credential — any models enabled through it cascade-delete with it."""
    credential = await get_credential(db, workspace_id=workspace_id, credential_id=credential_id)
    await db.delete(credential)
    await db.flush()


def decrypt_credential_key(credential: ProviderCredential) -> str:
    """Decrypt a credential's API key — for the provider service layer's use only, never a router."""
    secret = EncryptedSecret(
        ciphertext=credential.ciphertext, nonce=credential.nonce, wrapped_key=credential.wrapped_key
    )
    return decrypt_secret(secret)
