"""create flags and attachments

Revision ID: b79514c2ae34
Revises: 2866f78585ee
Create Date: 2026-09-08 09:00:00.000000

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b79514c2ae34"
down_revision: str | Sequence[str] | None = "2866f78585ee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same treatment as every enum before it — created/dropped explicitly so a downgrade -> upgrade
# cycle doesn't fail on a duplicate CREATE TYPE (see "role" and "message_role" for the history).
flag_type_enum = postgresql.ENUM("BOOLEAN", name="flag_type")
flag_scope_enum = postgresql.ENUM("USER", "WORKSPACE", name="flag_scope")
extract_status_enum = postgresql.ENUM("PENDING", "DONE", "FAILED", "UNSUPPORTED", name="extract_status")

# Seeded so the demo has something to flip on day one: three provider killswitches (default on)
# and the attachments feature itself (default off, an explicit opt-in rollout).
_SEED_FLAGS = [
    ("provider.anthropic", "Allow sending new messages through Anthropic-backed models.", True),
    ("provider.openai", "Allow sending new messages through OpenAI-backed models.", True),
    ("provider.google", "Allow sending new messages through Google-backed models.", True),
    (
        "provider.openai_compatible",
        "Allow sending new messages through OpenAI-compatible models (Groq, Ollama, etc).",
        True,
    ),
    ("attachments", "Show file upload in the composer and accept attachments on a message.", False),
]


def upgrade() -> None:
    """Create the flag/attachment enums, feature_flags, flag_overrides, and attachments tables."""
    flag_type_enum.create(op.get_bind(), checkfirst=True)
    flag_scope_enum.create(op.get_bind(), checkfirst=True)
    extract_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "feature_flags",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("type", postgresql.ENUM(name="flag_type", create_type=False), nullable=False),
        sa.Column("default_value", sa.JSON(), nullable=False),
        sa.Column("rollout_percentage", sa.Integer(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_feature_flags_key", "feature_flags", ["key"], unique=True)

    op.create_table(
        "flag_overrides",
        sa.Column("flag_id", sa.Uuid(), nullable=False),
        sa.Column("scope", postgresql.ENUM(name="flag_scope", create_type=False), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["flag_id"], ["feature_flags.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("flag_id", "scope", "scope_id"),
    )

    op.create_table(
        "attachments",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("uploaded_by", sa.Uuid(), nullable=False),
        sa.Column("original_filename", sa.String(), nullable=False),
        sa.Column("mime", sa.String(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(), nullable=False),
        sa.Column("extracted_text", sa.String(), nullable=True),
        sa.Column(
            "extract_status", postgresql.ENUM(name="extract_status", create_type=False), nullable=False
        ),
        sa.Column("extract_error", sa.String(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Backs "this conversation's attachments" — the only way they're ever listed.
    op.create_index("ix_attachments_conversation", "attachments", ["conversation_id"])

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
                "key": key,
                "description": description,
                "type": "BOOLEAN",
                "default_value": default_value,
            }
            for key, description, default_value in _SEED_FLAGS
        ],
    )


def downgrade() -> None:
    """Drop attachments, flag_overrides, and feature_flags, then the enums they depended on."""
    op.drop_table("attachments")
    op.drop_table("flag_overrides")
    op.drop_table("feature_flags")
    extract_status_enum.drop(op.get_bind(), checkfirst=True)
    flag_scope_enum.drop(op.get_bind(), checkfirst=True)
    flag_type_enum.drop(op.get_bind(), checkfirst=True)
