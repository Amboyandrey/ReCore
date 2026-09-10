"""create memory credentials

Revision ID: 343e56758c33
Revises: f3a1c9d7e421
Create Date: 2026-09-10 09:00:00.000000

Adds `memory_credentials` (a workspace's own mem0.ai API key — one row per workspace, envelope
-encrypted like every other secret this codebase stores), `assistants.memory_enabled` (whether
that assistant's chats are backed by mem0 at all), and the `memory` feature flag (default off,
same rollout treatment `tools` got).

`memory_credentials` uses `workspace_id` itself as its primary key rather than a separate
surrogate one — there is never a reason for a workspace to hold two mem0 keys — and gets the
same row-level-security treatment as every other workspace-scoped table (see 4294287e75c7's
docstring for why: `recore_app`, the app's own low-privilege runtime role, is the only role this
restricts).
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "343e56758c33"
down_revision: str | Sequence[str] | None = "f3a1c9d7e421"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RLS_TABLES = ["memory_credentials"]


def _allow_all_writes(table: str) -> None:
    """INSERT/UPDATE/DELETE stay ungated — see 4294287e75c7's docstring for why a FOR SELECT
    policy alone would otherwise default-deny every write to `table`, not just leave it open."""
    op.execute(f"CREATE POLICY allow_writes_insert ON {table} FOR INSERT WITH CHECK (true)")
    op.execute(f"CREATE POLICY allow_writes_update ON {table} FOR UPDATE USING (true) WITH CHECK (true)")
    op.execute(f"CREATE POLICY allow_writes_delete ON {table} FOR DELETE USING (true)")


def upgrade() -> None:
    op.create_table(
        "memory_credentials",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_key", sa.LargeBinary(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id"),
    )

    op.add_column(
        "assistants",
        sa.Column("memory_enabled", sa.Boolean(), nullable=False, server_default="false"),
    )

    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY workspace_isolation ON {table} FOR SELECT "
            f"USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)"
        )
        _allow_all_writes(table)

    flags_table = sa.table(
        "feature_flags",
        sa.column("id", sa.Uuid()),
        sa.column("key", sa.String()),
        sa.column("description", sa.String()),
        sa.column("type", postgresql.ENUM(name="flag_type", create_type=False)),
        sa.column("default_value", sa.JSON()),
    )
    op.bulk_insert(
        flags_table,
        [
            {
                "id": uuid.uuid4(),
                "key": "memory",
                "description": "Let a memory-enabled assistant recall curated and personal "
                "memories via mem0, and let chat teach it new ones.",
                "type": "BOOLEAN",
                "default_value": False,
            }
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM feature_flags WHERE key = 'memory'")

    for table in reversed(_RLS_TABLES):
        op.execute(f"DROP POLICY IF EXISTS allow_writes_delete ON {table}")
        op.execute(f"DROP POLICY IF EXISTS allow_writes_update ON {table}")
        op.execute(f"DROP POLICY IF EXISTS allow_writes_insert ON {table}")
        op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_column("assistants", "memory_enabled")
    op.drop_table("memory_credentials")
