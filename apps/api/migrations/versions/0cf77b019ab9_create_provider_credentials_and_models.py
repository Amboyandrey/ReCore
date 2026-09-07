"""create provider_credentials and models

Revision ID: 0cf77b019ab9
Revises: 55943b806c34
Create Date: 2026-09-07 15:28:28.938423

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0cf77b019ab9"
down_revision: str | Sequence[str] | None = "55943b806c34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Created/dropped explicitly (create_type=False on the column) rather than left to
# create_table/drop_table — see the "role" enum in the previous migration for why: autogenerate's
# downgrade doesn't drop the type, so a downgrade -> upgrade cycle fails on a duplicate CREATE TYPE.
provider_enum = postgresql.ENUM("ANTHROPIC", "OPENAI", "GOOGLE", "OPENAI_COMPATIBLE", name="provider")


def upgrade() -> None:
    """Create the provider enum, provider_credentials, and models."""
    provider_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "provider_credentials",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("provider", postgresql.ENUM(name="provider", create_type=False), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("base_url", sa.String(), nullable=True),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_key", sa.LargeBinary(), nullable=False),
        sa.Column("last4", sa.String(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
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
    )

    op.create_table(
        "models",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("credential_id", sa.Uuid(), nullable=False),
        sa.Column("provider_model_id", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("context_window", sa.Integer(), nullable=True),
        sa.Column("cost_per_mtok_in", sa.Numeric(precision=10, scale=4, asdecimal=False), nullable=True),
        sa.Column("cost_per_mtok_out", sa.Numeric(precision=10, scale=4, asdecimal=False), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["credential_id"], ["provider_credentials.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("credential_id", "provider_model_id"),
    )


def downgrade() -> None:
    """Drop models and provider_credentials, then the provider enum they depended on."""
    op.drop_table("models")
    op.drop_table("provider_credentials")
    provider_enum.drop(op.get_bind(), checkfirst=True)
