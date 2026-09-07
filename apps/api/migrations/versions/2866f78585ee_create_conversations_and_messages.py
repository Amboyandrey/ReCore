"""create conversations and messages

Revision ID: 2866f78585ee
Revises: 0cf77b019ab9
Create Date: 2026-09-07 16:09:25.249147

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2866f78585ee"
down_revision: str | Sequence[str] | None = "0cf77b019ab9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same treatment as "role" and "provider" before it — created/dropped explicitly so a
# downgrade -> upgrade cycle doesn't fail on a duplicate CREATE TYPE.
message_role_enum = postgresql.ENUM("USER", "ASSISTANT", "SYSTEM", name="message_role")


def upgrade() -> None:
    """Create the message_role enum, conversations, and messages, plus their listing indexes."""
    message_role_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "conversations",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("model_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("system_prompt", sa.String(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Backs "this workspace's conversations, newest first" — the only way the list is ever read.
    op.create_index(
        "ix_conversations_workspace_updated",
        "conversations",
        ["workspace_id", sa.text("updated_at DESC")],
    )

    op.create_table(
        "messages",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("role", postgresql.ENUM(name="message_role", create_type=False), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(precision=10, scale=6, asdecimal=False), nullable=True),
        sa.Column("finish_reason", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Backs "this conversation's messages, oldest first" — the only way a thread is ever read.
    op.create_index("ix_messages_conversation_created", "messages", ["conversation_id", "created_at"])


def downgrade() -> None:
    """Drop messages and conversations, then the message_role enum they depended on."""
    op.drop_table("messages")
    op.drop_table("conversations")
    message_role_enum.drop(op.get_bind(), checkfirst=True)
