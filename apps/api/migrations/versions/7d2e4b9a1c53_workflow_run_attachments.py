"""workflow run attachments

Revision ID: 7d2e4b9a1c53
Revises: 363f1d96cd92
Create Date: 2026-09-18 10:05:00.000000

Lets a workflow run start from uploaded files, not only typed text. An attachment used to belong
to exactly one conversation; now it belongs to exactly one of a conversation *or* a workflow —
`conversation_id` becomes nullable, `workflow_id` is added, and a CHECK constraint keeps "exactly
one parent" a database fact rather than an application convention. `workflow_run_id` is null
until the run that uses the file actually starts (same upload-ahead-of-send shape `message_id`
already follows for chat), and cascades with the run.

`workflow_runs.input` keeps its NOT NULL: a run started from files alone stores the empty string,
so every existing `{{input}}` template still renders (to nothing) rather than erroring.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7d2e4b9a1c53"
down_revision: str | Sequence[str] | None = "363f1d96cd92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Let an attachment belong to a workflow (and later its run) instead of a conversation."""
    op.alter_column("attachments", "conversation_id", nullable=True)
    op.add_column("attachments", sa.Column("workflow_id", sa.Uuid(), nullable=True))
    op.add_column("attachments", sa.Column("workflow_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "attachments_workflow_id_fkey",
        "attachments",
        "workflows",
        ["workflow_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "attachments_workflow_run_id_fkey",
        "attachments",
        "workflow_runs",
        ["workflow_run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "attachments_one_parent",
        "attachments",
        "(conversation_id IS NOT NULL) <> (workflow_id IS NOT NULL)",
    )
    # Backs "this run's attachments" — how the worker loads a run's files at each step.
    op.create_index("ix_attachments_workflow_run", "attachments", ["workflow_run_id"])


def downgrade() -> None:
    """Drop workflow-owned attachments, then restore conversation ownership as mandatory."""
    op.execute("DELETE FROM attachments WHERE workflow_id IS NOT NULL")
    op.drop_index("ix_attachments_workflow_run", table_name="attachments")
    op.drop_constraint("attachments_one_parent", "attachments", type_="check")
    op.drop_constraint("attachments_workflow_run_id_fkey", "attachments", type_="foreignkey")
    op.drop_constraint("attachments_workflow_id_fkey", "attachments", type_="foreignkey")
    op.drop_column("attachments", "workflow_run_id")
    op.drop_column("attachments", "workflow_id")
    op.alter_column("attachments", "conversation_id", nullable=False)
