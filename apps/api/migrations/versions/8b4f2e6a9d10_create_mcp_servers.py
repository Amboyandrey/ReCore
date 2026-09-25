"""create mcp servers

Revision ID: 8b4f2e6a9d10
Revises: 7d2e4b9a1c53
Create Date: 2026-09-25 10:00:00.000000

Adds `mcp_servers` (a workspace's connected remote MCP endpoints) and lets a `tools` row belong to
one: the `MCP` tool kind, `tools.mcp_server_id` (cascading, so removing a server removes its
tools), and `tools.remote_name` (what the server itself calls the tool). No new flag — MCP tools
ride on the existing `tools` one.

`mcp_servers` gets the same row-level-security treatment as every other workspace-scoped table
(see 4294287e75c7's docstring).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8b4f2e6a9d10"
down_revision: str | Sequence[str] | None = "7d2e4b9a1c53"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create mcp_servers, link tools to it, and add the MCP tool kind (unused in this
    transaction, which Postgres requires of a freshly added enum value)."""
    op.execute("ALTER TYPE tool_kind ADD VALUE IF NOT EXISTS 'MCP'")

    op.create_table(
        "mcp_servers",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("auth_header", sa.String(), nullable=True),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("nonce", sa.LargeBinary(), nullable=True),
        sa.Column("wrapped_key", sa.LargeBinary(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("workspace_id", "name", name="uq_mcp_servers_workspace_name"),
    )

    op.add_column("tools", sa.Column("mcp_server_id", sa.Uuid(), nullable=True))
    op.add_column("tools", sa.Column("remote_name", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_tools_mcp_server_id", "tools", "mcp_servers", ["mcp_server_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index("ix_tools_mcp_server", "tools", ["mcp_server_id"])

    op.execute("ALTER TABLE mcp_servers ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE mcp_servers FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY workspace_isolation ON mcp_servers FOR SELECT "
        "USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)"
    )
    op.execute("CREATE POLICY allow_writes_insert ON mcp_servers FOR INSERT WITH CHECK (true)")
    op.execute(
        "CREATE POLICY allow_writes_update ON mcp_servers FOR UPDATE USING (true) WITH CHECK (true)"
    )
    op.execute("CREATE POLICY allow_writes_delete ON mcp_servers FOR DELETE USING (true)")


def downgrade() -> None:
    """Drop mcp_servers and its tools. The MCP enum value stays behind — Postgres has no
    `ALTER TYPE ... DROP VALUE` (see 5f133c55bce8's downgrade for the same trade-off)."""
    op.execute("DELETE FROM tools WHERE mcp_server_id IS NOT NULL")
    op.drop_index("ix_tools_mcp_server", table_name="tools")
    op.drop_constraint("fk_tools_mcp_server_id", "tools", type_="foreignkey")
    op.drop_column("tools", "remote_name")
    op.drop_column("tools", "mcp_server_id")

    policies = ("allow_writes_delete", "allow_writes_update", "allow_writes_insert", "workspace_isolation")
    for policy in policies:
        op.execute(f"DROP POLICY IF EXISTS {policy} ON mcp_servers")
    op.drop_table("mcp_servers")
