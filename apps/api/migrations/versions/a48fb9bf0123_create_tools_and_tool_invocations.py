"""create tools and tool invocations

Revision ID: a48fb9bf0123
Revises: cdd688e30099
Create Date: 2026-09-09 10:00:00.000000

Adds the `tools` feature flag (default off, same rollout treatment `attachments` got), the
`tools` table (a workspace's own tools — the built-in web search is a row here like any other,
not a separate seeded/virtual concept), and `tool_invocations` (one row per call the model
actually made, for the transcript and for audit).

Both new tables get the same row-level-security treatment as every other workspace-scoped table
(see 4294287e75c7's docstring for why: `recore_app`, the app's own low-privilege runtime role, is
the only role this restricts — migrations and tests connect as the table owner, exempt from RLS
by Postgres default, so nothing about how migrations or `pytest` run changes here).
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a48fb9bf0123"
down_revision: str | Sequence[str] | None = "cdd688e30099"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

tool_kind_enum = postgresql.ENUM("BUILTIN", "HTTP", name="tool_kind")
tool_invocation_status_enum = postgresql.ENUM("SUCCESS", "ERROR", name="tool_invocation_status")

_RLS_TABLES = ["tools", "tool_invocations"]


def _allow_all_writes(table: str) -> None:
    """INSERT/UPDATE/DELETE stay ungated — see 4294287e75c7's docstring for why a FOR SELECT
    policy alone would otherwise default-deny every write to `table`, not just leave it open."""
    op.execute(f"CREATE POLICY allow_writes_insert ON {table} FOR INSERT WITH CHECK (true)")
    op.execute(f"CREATE POLICY allow_writes_update ON {table} FOR UPDATE USING (true) WITH CHECK (true)")
    op.execute(f"CREATE POLICY allow_writes_delete ON {table} FOR DELETE USING (true)")


def upgrade() -> None:
    tool_kind_enum.create(op.get_bind(), checkfirst=True)
    tool_invocation_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "tools",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("kind", postgresql.ENUM(name="tool_kind", create_type=False), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("method", sa.String(), nullable=True),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("secret_header", sa.String(), nullable=True),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("nonce", sa.LargeBinary(), nullable=True),
        sa.Column("wrapped_key", sa.LargeBinary(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_tools_workspace_name"),
    )

    op.create_table(
        "tool_invocations",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("tool_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("result", sa.String(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(name="tool_invocation_status", create_type=False),
            nullable=False,
        ),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tool_id"], ["tools.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Backs "this message's tool calls" — the only way they're ever listed, same shape as
    # attachments' own conversation index.
    op.create_index("ix_tool_invocations_message", "tool_invocations", ["message_id"])

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
                "key": "tools",
                "description": "Let the model call tools (web search, and any registered "
                "third-party tools) mid-reply.",
                "type": "BOOLEAN",
                "default_value": False,
            }
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM feature_flags WHERE key = 'tools'")

    for table in reversed(_RLS_TABLES):
        op.execute(f"DROP POLICY IF EXISTS allow_writes_delete ON {table}")
        op.execute(f"DROP POLICY IF EXISTS allow_writes_update ON {table}")
        op.execute(f"DROP POLICY IF EXISTS allow_writes_insert ON {table}")
        op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_tool_invocations_message", table_name="tool_invocations")
    op.drop_table("tool_invocations")
    op.drop_table("tools")
    tool_invocation_status_enum.drop(op.get_bind(), checkfirst=True)
    tool_kind_enum.drop(op.get_bind(), checkfirst=True)
