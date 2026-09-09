"""create assistants

Revision ID: f3a1c9d7e421
Revises: a48fb9bf0123
Create Date: 2026-09-09 12:00:00.000000

Adds the `assistants` table (a workspace's own saved name + required instructions + optional
model), `assistant_tools` (which of a workspace's tools each assistant is equipped with — a
plain many-to-many join, same composite-key shape `workspace_members` uses), and
`conversations.assistant_id` (nullable — a conversation with no assistant behaves exactly as
before this migration).

`assistants` gets the same row-level-security treatment as every other workspace-scoped table
(see 4294287e75c7's docstring). `assistant_tools` has no workspace_id of its own — like
`messages`, it's reached through its parent (`assistants`, itself already RLS'd), so its own
policy joins through that instead of a direct column check.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3a1c9d7e421"
down_revision: str | Sequence[str] | None = "a48fb9bf0123"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _allow_all_writes(table: str) -> None:
    """INSERT/UPDATE/DELETE stay ungated — see 4294287e75c7's docstring for why a FOR SELECT
    policy alone would otherwise default-deny every write to `table`, not just leave it open."""
    op.execute(f"CREATE POLICY allow_writes_insert ON {table} FOR INSERT WITH CHECK (true)")
    op.execute(f"CREATE POLICY allow_writes_update ON {table} FOR UPDATE USING (true) WITH CHECK (true)")
    op.execute(f"CREATE POLICY allow_writes_delete ON {table} FOR DELETE USING (true)")


def upgrade() -> None:
    op.create_table(
        "assistants",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("instructions", sa.String(), nullable=False),
        sa.Column("model_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "assistant_tools",
        sa.Column("assistant_id", sa.Uuid(), nullable=False),
        sa.Column("tool_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["assistant_id"], ["assistants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tool_id"], ["tools.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("assistant_id", "tool_id"),
    )

    op.add_column("conversations", sa.Column("assistant_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_conversations_assistant_id",
        "conversations",
        "assistants",
        ["assistant_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.execute("ALTER TABLE assistants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE assistants FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY workspace_isolation ON assistants FOR SELECT "
        "USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)"
    )
    _allow_all_writes("assistants")

    op.execute("ALTER TABLE assistant_tools ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE assistant_tools FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY workspace_isolation ON assistant_tools FOR SELECT "
        "USING (assistant_id IN ("
        "  SELECT id FROM assistants "
        "  WHERE workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
        "))"
    )
    _allow_all_writes("assistant_tools")


def downgrade() -> None:
    for table in ["assistant_tools", "assistants"]:
        op.execute(f"DROP POLICY IF EXISTS allow_writes_delete ON {table}")
        op.execute(f"DROP POLICY IF EXISTS allow_writes_update ON {table}")
        op.execute(f"DROP POLICY IF EXISTS allow_writes_insert ON {table}")
        op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_constraint("fk_conversations_assistant_id", "conversations", type_="foreignkey")
    op.drop_column("conversations", "assistant_id")
    op.drop_table("assistant_tools")
    op.drop_table("assistants")
