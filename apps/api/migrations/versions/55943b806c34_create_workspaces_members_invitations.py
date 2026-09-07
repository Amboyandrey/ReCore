"""create workspaces, members, invitations

Revision ID: 55943b806c34
Revises: 94c3c88a7504
Create Date: 2026-09-07 14:56:26.868749

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "55943b806c34"
down_revision: str | Sequence[str] | None = "94c3c88a7504"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Shared by both workspace_members and invitations — created once explicitly (create_type=False
# on the columns below) so a second CREATE TYPE doesn't collide, and dropped once explicitly in
# downgrade() once nothing references it any more.
role_enum = postgresql.ENUM("VIEWER", "MEMBER", "ADMIN", "OWNER", name="role")


def upgrade() -> None:
    """Create the workspaces, invitations, and workspace_members tables, and the role enum."""
    role_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "workspaces",
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column(
            "settings", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_workspaces_slug"), "workspaces", ["slug"], unique=True)

    op.create_table(
        "invitations",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("role", postgresql.ENUM(name="role", create_type=False), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("invited_by", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_invitations_token_hash"), "invitations", ["token_hash"], unique=True)

    op.create_table(
        "workspace_members",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", postgresql.ENUM(name="role", create_type=False), nullable=False),
        sa.Column(
            "joined_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "user_id"),
    )


def downgrade() -> None:
    """Drop the three tables and, since nothing references it any more, the role enum."""
    op.drop_table("workspace_members")
    op.drop_index(op.f("ix_invitations_token_hash"), table_name="invitations")
    op.drop_table("invitations")
    op.drop_index(op.f("ix_workspaces_slug"), table_name="workspaces")
    op.drop_table("workspaces")
    role_enum.drop(op.get_bind(), checkfirst=True)
