"""create workflows

Revision ID: 363f1d96cd92
Revises: 09e1569cc052
Create Date: 2026-09-14 10:20:06.459669

Adds ReFlow (Level 2): a workspace's own saved workflows — an ordered chain of assistant steps
that runs in the background on the arq worker, no chat turn involved. `workflows` is the
definition (name, steps' shared default model, and the schedule/webhook trigger columns a later
PR reads); `workflow_steps` is one row per step (an assistant + a prompt template that can
reference the run's input and earlier steps' outputs); `workflow_runs`/`workflow_step_runs` are
the run history the run page shows, one step run snapshotting its step's key/name/assistant so a
later edit to the workflow never rewrites a past run.

Also widens `usage_events`: `conversation_id`/`message_id` become nullable and a new
`workflow_run_id` is added, since a workflow step's usage has no conversation or message to point
at (see UsageEvent's own docstring for the "exactly one pair is populated" contract).

Every new workspace-scoped table gets the same row-level-security treatment as the rest of this
schema (see 4294287e75c7's docstring): `recore_app` is the only role this restricts, and is
auto-granted CRUD on new tables via that migration's `ALTER DEFAULT PRIVILEGES`. All four new
tables carry their own `workspace_id`, so every policy here is a direct one — no join-through-
parent policy is needed (contrast `assistant_connectors` in 09e1569cc052).

Seeds the `workflows` feature flag, default off.
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "363f1d96cd92"
down_revision: str | Sequence[str] | None = "09e1569cc052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

workflow_trigger_enum = postgresql.ENUM("MANUAL", "SCHEDULE", "WEBHOOK", name="workflow_trigger")
workflow_run_status_enum = postgresql.ENUM(
    "QUEUED", "RUNNING", "WAITING_APPROVAL", "SUCCEEDED", "FAILED", "REJECTED", "CANCELED",
    name="workflow_run_status",
)
workflow_step_status_enum = postgresql.ENUM(
    "PENDING", "RUNNING", "WAITING_APPROVAL", "SUCCEEDED", "FAILED", "SKIPPED",
    name="workflow_step_status",
)

_DIRECT_RLS_TABLES = ["workflows", "workflow_steps", "workflow_runs", "workflow_step_runs"]


def _allow_all_writes(table: str) -> None:
    """INSERT/UPDATE/DELETE stay ungated — see 4294287e75c7's docstring for why a FOR SELECT
    policy alone would otherwise default-deny every write to `table`, not just leave it open."""
    op.execute(f"CREATE POLICY allow_writes_insert ON {table} FOR INSERT WITH CHECK (true)")
    op.execute(f"CREATE POLICY allow_writes_update ON {table} FOR UPDATE USING (true) WITH CHECK (true)")
    op.execute(f"CREATE POLICY allow_writes_delete ON {table} FOR DELETE USING (true)")


def upgrade() -> None:
    workflow_trigger_enum.create(op.get_bind(), checkfirst=True)
    workflow_run_status_enum.create(op.get_bind(), checkfirst=True)
    workflow_step_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "workflows",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), server_default="", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("default_model_id", sa.Uuid(), nullable=True),
        sa.Column("schedule_cron", sa.String(), nullable=True),
        sa.Column("schedule_timezone", sa.String(), server_default="UTC", nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("webhook_secret", sa.String(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["default_model_id"], ["models.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("webhook_secret"),
    )

    op.create_table(
        "workflow_steps",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("assistant_id", sa.Uuid(), nullable=True),
        sa.Column("prompt_template", sa.String(), nullable=False),
        sa.Column("requires_approval", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["assistant_id"], ["assistants.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id", "key"),
        sa.UniqueConstraint("workflow_id", "position"),
    )

    op.create_table(
        "workflow_runs",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("trigger", postgresql.ENUM(name="workflow_trigger", create_type=False), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="workflow_run_status", create_type=False),
            server_default="QUEUED",
            nullable=False,
        ),
        sa.Column("input", sa.String(), nullable=False),
        sa.Column("output", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("started_by", sa.Uuid(), nullable=True),
        sa.Column("current_position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(10, 6), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["started_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workflow_runs_workflow_created", "workflow_runs", ["workflow_id", "created_at"])

    op.create_table(
        "workflow_step_runs",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("step_id", sa.Uuid(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("assistant_id", sa.Uuid(), nullable=True),
        sa.Column("prompt_template", sa.String(), nullable=False),
        sa.Column("requires_approval", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="workflow_step_status", create_type=False),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("prompt", sa.String(), nullable=True),
        sa.Column("output", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tokens_out", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(10, 6), server_default="0", nullable=False),
        sa.Column("latency_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("invocations", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("sources", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["assistant_id"], ["assistants.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["step_id"], ["workflow_steps.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "position"),
    )

    for table in _DIRECT_RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY workspace_isolation ON {table} FOR SELECT "
            f"USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)"
        )
        _allow_all_writes(table)

    op.add_column("usage_events", sa.Column("workflow_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "usage_events_workflow_run_id_fkey",
        "usage_events",
        "workflow_runs",
        ["workflow_run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.alter_column("usage_events", "conversation_id", nullable=True)
    op.alter_column("usage_events", "message_id", nullable=True)

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
                "key": "workflows",
                "description": "Let a workspace save and run background workflows — ordered "
                "chains of assistant steps that run on the worker, no chat turn involved.",
                "type": "BOOLEAN",
                "default_value": False,
            }
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM feature_flags WHERE key = 'workflows'")

    # A workflow step's usage event has no conversation/message — restoring NOT NULL below would
    # fail on any such row still present, so those rows (never billable outside this feature) are
    # removed first rather than left to violate the constraint.
    op.execute("DELETE FROM usage_events WHERE workflow_run_id IS NOT NULL")
    op.drop_constraint("usage_events_workflow_run_id_fkey", "usage_events", type_="foreignkey")
    op.drop_column("usage_events", "workflow_run_id")
    op.alter_column("usage_events", "message_id", nullable=False)
    op.alter_column("usage_events", "conversation_id", nullable=False)

    for table in reversed(_DIRECT_RLS_TABLES):
        op.execute(f"DROP POLICY IF EXISTS allow_writes_delete ON {table}")
        op.execute(f"DROP POLICY IF EXISTS allow_writes_update ON {table}")
        op.execute(f"DROP POLICY IF EXISTS allow_writes_insert ON {table}")
        op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_table("workflow_step_runs")
    op.drop_index("ix_workflow_runs_workflow_created", table_name="workflow_runs")
    op.drop_table("workflow_runs")
    op.drop_table("workflow_steps")
    op.drop_table("workflows")

    workflow_step_status_enum.drop(op.get_bind(), checkfirst=True)
    workflow_run_status_enum.drop(op.get_bind(), checkfirst=True)
    workflow_trigger_enum.drop(op.get_bind(), checkfirst=True)
