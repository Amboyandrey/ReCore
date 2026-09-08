"""Row-level security actually enforces `app.workspace_id` — tested against the real database,
connected as the low-privilege `recore_app` role the RLS migration creates.

Every other test in this suite connects as the table owner (`DATABASE_URL`'s role), which Postgres
exempts from row-level security by default — that's deliberate (see the RLS migration's docstring)
and exactly why RLS needs its own test connecting as the role it's actually meant to restrict.
"""

import os
import uuid
from urllib.parse import urlsplit

import asyncpg
from sqlalchemy import select
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


def _app_role_dsn() -> str:
    """The same host/port/database pytest's own DATABASE_URL points at, but as `recore_app`."""
    scheme, netloc, path, _, _ = urlsplit(os.environ["DATABASE_URL"])
    host_port = netloc.rsplit("@", 1)[-1]
    return f"postgresql://recore_app:recore_app_dev_only@{host_port}{path}"


async def _count_credentials_as_app_role(workspace_id: uuid.UUID | None) -> int:
    """Connect as recore_app, optionally set `app.workspace_id`, and count visible credentials."""
    conn = await asyncpg.connect(_app_role_dsn())
    try:
        if workspace_id is not None:
            await conn.execute("SELECT set_config('app.workspace_id', $1, false)", str(workspace_id))
        return await conn.fetchval("SELECT count(*) FROM provider_credentials")
    finally:
        await conn.close()


async def _seed_one_workspace_with_a_credential(db: AsyncSession) -> uuid.UUID:
    """A user, a workspace, and one provider credential in it — real data RLS should hide or
    reveal depending on which workspace `app.workspace_id` is set to. (Not `invitations`: that
    table is deliberately excluded from RLS — see 2ca6fbc541b7's docstring — so it can no longer
    stand in for "a table RLS actually restricts" the way it used to.)"""
    user = User(email="rls@example.com", password_hash="hashed")
    db.add(user)
    await db.flush()
    workspace = Workspace(slug="rls-test", name="RLS Test", owner_id=user.id)
    db.add(workspace)
    await db.flush()
    db.add(
        ProviderCredential(
            workspace_id=workspace.id,
            provider=Provider.ANTHROPIC,
            label="Prod",
            ciphertext=b"\x01",
            nonce=b"\x02" * 12,
            wrapped_key=b"\x03" * 44,
            last4="ab12",
            created_by=user.id,
        )
    )
    await db.commit()  # recore_app's separate connection must see this as truly committed
    return workspace.id


async def test_the_owner_role_bypasses_row_level_security(db: AsyncSession) -> None:
    """Sanity check on the whole suite's own assumption: every other test connects as the table
    owner, which is exempt from RLS by Postgres default even with FORCE ROW LEVEL SECURITY set."""
    workspace_id = await _seed_one_workspace_with_a_credential(db)

    rows = (
        await db.scalars(
            select(ProviderCredential).where(ProviderCredential.workspace_id == workspace_id)
        )
    ).all()
    assert len(rows) == 1


async def test_the_app_role_sees_nothing_with_no_workspace_scope_set(db: AsyncSession) -> None:
    """A connection that never sets `app.workspace_id` at all — the state a bug that skipped
    get_workspace_ctx would leave a connection in — sees zero rows, not everything."""
    await _seed_one_workspace_with_a_credential(db)

    count = await _count_credentials_as_app_role(workspace_id=None)

    assert count == 0


async def test_the_app_role_sees_nothing_for_a_different_workspace(db: AsyncSession) -> None:
    """Scoped to a workspace that isn't the one the row belongs to — still zero rows."""
    await _seed_one_workspace_with_a_credential(db)

    count = await _count_credentials_as_app_role(workspace_id=uuid.uuid4())

    assert count == 0


async def test_the_app_role_sees_its_own_workspace_s_row(db: AsyncSession) -> None:
    """Scoped to the right workspace, the row it's actually allowed to see comes back."""
    workspace_id = await _seed_one_workspace_with_a_credential(db)

    count = await _count_credentials_as_app_role(workspace_id=workspace_id)

    assert count == 1


async def test_the_app_role_can_still_write_regardless_of_workspace_scope(db: AsyncSession) -> None:
    """Writes stay ungated — only reads are — so an insert with no scope set at all still lands,
    same as the app's own request-serving connection can always insert what it's authorized to."""
    user = User(email="rls-writer@example.com", password_hash="hashed")
    db.add(user)
    await db.flush()
    workspace = Workspace(slug="rls-write", name="RLS Write", owner_id=user.id)
    db.add(workspace)
    await db.commit()

    conn = await asyncpg.connect(_app_role_dsn())
    try:
        await conn.execute(
            "INSERT INTO provider_credentials "
            "(id, workspace_id, provider, label, ciphertext, nonce, wrapped_key, last4, "
            "created_by, created_at, updated_at) "
            "VALUES (gen_random_uuid(), $1, 'ANTHROPIC', 'Written by recore_app', "
            "'\\x01', '\\x02', '\\x03', 'zz99', $2, now(), now())",
            workspace.id,
            user.id,
        )
    finally:
        await conn.close()

    rows = (
        await db.scalars(
            select(ProviderCredential).where(ProviderCredential.workspace_id == workspace.id)
        )
    ).all()
    assert len(rows) == 1


async def test_messages_are_scoped_through_their_conversation_s_workspace(db: AsyncSession) -> None:
    """messages has no workspace_id of its own — its policy reaches through conversations."""
    user = User(email="rls2@example.com", password_hash="hashed")
    db.add(user)
    await db.flush()
    workspace = Workspace(slug="rls-msgs", name="RLS Messages", owner_id=user.id)
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
    conversation = Conversation(
        workspace_id=workspace.id, user_id=user.id, model_id=model.id, title="Untitled"
    )
    db.add(conversation)
    await db.flush()
    db.add(Message(conversation_id=conversation.id, role=MessageRole.USER, content="Hi"))
    await db.commit()

    conn = await asyncpg.connect(_app_role_dsn())
    try:
        wrong = await conn.fetchval("SELECT count(*) FROM messages")  # no scope set
        await conn.execute("SELECT set_config('app.workspace_id', $1, false)", str(workspace.id))
        right = await conn.fetchval("SELECT count(*) FROM messages")
    finally:
        await conn.close()

    assert wrong == 0
    assert right == 1


async def test_a_mid_transaction_commit_resets_the_scope_until_reapplied(
    db: AsyncSession,
) -> None:
    """The exact bug a real user hit: `set_config(..., true)` is transaction-local (the
    parameterized equivalent of `SET LOCAL`), so a commit ends its effect along with the
    transaction — a later query on the same connection sees nothing again unless the scope is
    set a second time. This is why send_message() (services/chat.py) re-applies it right after
    its own mid-function commit, and why the seed script's provider step needs the same call
    before reading back the credential it just wrote. This test would have failed before either
    fix existed — a `client`-fixture test wouldn't have, since every other test in this suite
    connects as the table owner, which bypasses row-level security entirely regardless."""
    user = User(email="rls-txn@example.com", password_hash="hashed")
    db.add(user)
    await db.flush()
    workspace = Workspace(slug="rls-txn", name="RLS Txn", owner_id=user.id)
    db.add(workspace)
    await db.flush()
    db.add(
        ProviderCredential(
            workspace_id=workspace.id,
            provider=Provider.ANTHROPIC,
            label="Prod",
            ciphertext=b"\x01",
            nonce=b"\x02" * 12,
            wrapped_key=b"\x03" * 44,
            last4="ab12",
            created_by=user.id,
        )
    )
    await db.commit()

    conn = await asyncpg.connect(_app_role_dsn())
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.workspace_id', $1, true)", str(workspace.id))
            visible_before_commit = await conn.fetchval("SELECT count(*) FROM provider_credentials")
        # The `async with` block above committed on exit — same shape as send_message's own
        # mid-function commit ending the transaction the scope was set for.
        visible_after_commit = await conn.fetchval("SELECT count(*) FROM provider_credentials")

        async with conn.transaction():
            await conn.execute("SELECT set_config('app.workspace_id', $1, true)", str(workspace.id))
            visible_after_reapplying = await conn.fetchval(
                "SELECT count(*) FROM provider_credentials"
            )
    finally:
        await conn.close()

    assert visible_before_commit == 1
    assert visible_after_commit == 0
    assert visible_after_reapplying == 1
