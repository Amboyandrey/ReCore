"""create assistant delegates

Revision ID: b3bd5c6720f3
Revises: 343e56758c33
Create Date: 2026-09-13 09:00:00.000000

Adds `assistant_delegates` (which other assistants an assistant may hand a task to during chat,
via a synthesized `ask_<name>` tool — see services/chat.py) and the `delegation` feature flag
(default off, same rollout treatment `memory` got).

`assistant_delegates` is a self-referential many-to-many join, same shape as `assistant_tools`
(f3a1c9d7e421) — reached only through its parent (`assistants`, itself already RLS'd), so its
policy joins through the orchestrating side the same way `assistant_tools`' does. The check
constraint makes self-delegation impossible at the database level, not just in the service layer.
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b3bd5c6720f3"
down_revision: str | Sequence[str] | None = "343e56758c33"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RLS_TABLES = ["assistant_delegates"]


def _allow_all_writes(table: str) -> None:
    """INSERT/UPDATE/DELETE stay ungated — see 4294287e75c7's docstring for why a FOR SELECT
    policy alone would otherwise default-deny every write to `table`, not just leave it open."""
    op.execute(f"CREATE POLICY allow_writes_insert ON {table} FOR INSERT WITH CHECK (true)")
    op.execute(f"CREATE POLICY allow_writes_update ON {table} FOR UPDATE USING (true) WITH CHECK (true)")
    op.execute(f"CREATE POLICY allow_writes_delete ON {table} FOR DELETE USING (true)")


def upgrade() -> None:
    op.create_table(
        "assistant_delegates",
        sa.Column("assistant_id", sa.Uuid(), nullable=False),
        sa.Column("delegate_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["assistant_id"], ["assistants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["delegate_id"], ["assistants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("assistant_id", "delegate_id"),
        sa.CheckConstraint("assistant_id <> delegate_id", name="ck_assistant_delegates_not_self"),
    )

    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY workspace_isolation ON {table} FOR SELECT "
            "USING (assistant_id IN ("
            "  SELECT id FROM assistants "
            "  WHERE workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
            "))"
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
                "key": "delegation",
                "description": "Let an assistant hand a task to another assistant in the "
                "workspace as a tool call, and use its answer.",
                "type": "BOOLEAN",
                "default_value": False,
            }
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM feature_flags WHERE key = 'delegation'")

    for table in reversed(_RLS_TABLES):
        op.execute(f"DROP POLICY IF EXISTS allow_writes_delete ON {table}")
        op.execute(f"DROP POLICY IF EXISTS allow_writes_update ON {table}")
        op.execute(f"DROP POLICY IF EXISTS allow_writes_insert ON {table}")
        op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_table("assistant_delegates")
