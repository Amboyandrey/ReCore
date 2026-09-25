"""tool image attachments

Revision ID: c4e7a1f2b9d3
Revises: 8b4f2e6a9d10
Create Date: 2026-09-25 14:00:00.000000

Lets an attachment be an image a tool returned, not only a user's upload: `source` tells the two
apart (every existing row is an upload), and `tool_invocation_id` ties a tool image to the call
that produced it, cascading with it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4e7a1f2b9d3"
down_revision: str | Sequence[str] | None = "8b4f2e6a9d10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

attachment_source_enum = postgresql.ENUM("UPLOAD", "TOOL", name="attachment_source")


def upgrade() -> None:
    """Add attachments.source (backfilled as UPLOAD) and attachments.tool_invocation_id."""
    attachment_source_enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "attachments",
        sa.Column(
            "source",
            postgresql.ENUM(name="attachment_source", create_type=False),
            nullable=False,
            server_default="UPLOAD",
        ),
    )
    op.add_column("attachments", sa.Column("tool_invocation_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_attachments_tool_invocation_id",
        "attachments",
        "tool_invocations",
        ["tool_invocation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_attachments_tool_invocation", "attachments", ["tool_invocation_id"])


def downgrade() -> None:
    """Drop tool images and both columns; the files themselves are left on disk."""
    op.execute("DELETE FROM attachments WHERE source = 'TOOL'")
    op.drop_index("ix_attachments_tool_invocation", table_name="attachments")
    op.drop_constraint("fk_attachments_tool_invocation_id", "attachments", type_="foreignkey")
    op.drop_column("attachments", "tool_invocation_id")
    op.drop_column("attachments", "source")
    attachment_source_enum.drop(op.get_bind(), checkfirst=True)
