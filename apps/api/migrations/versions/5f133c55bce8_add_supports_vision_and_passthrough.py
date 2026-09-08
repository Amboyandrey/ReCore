"""add supports_vision and passthrough extract status

Revision ID: 5f133c55bce8
Revises: 4294287e75c7
Create Date: 2026-09-08 12:00:00.000000

Two independent additions, bundled because they're both small and both part of native image
support: a `models.supports_vision` flag the admin sets explicitly (model ids can't be reliably
classified across arbitrary OpenAI-compatible endpoints), and a new `extract_status` value for
"this is an image, handed to the model directly — no text extraction needed or attempted."

`supports_vision` is backfilled for models whose id looks first-party and vision-capable
(Anthropic/Gemini/GPT-4o+), so existing workspaces don't all regress to "no vision" and need a
manual admin pass. It's a one-time, visible-and-correctable guess, not a runtime heuristic that
would rot silently as provider model names change — anyone it gets wrong can just flip the
checkbox in Settings -> Providers.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5f133c55bce8"
down_revision: str | Sequence[str] | None = "4294287e75c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# First-party model id conventions that are vision-capable as of this writing. A guess, not a
# guarantee — see this migration's own docstring.
_VISION_ID_PATTERN = r"^(claude-|gemini-|gpt-4o|gpt-5)"


def upgrade() -> None:
    """Add models.supports_vision (backfilled by id pattern) and the extract_status PASSTHROUGH
    value. The enum addition only adds the label — nothing in this migration uses it yet, which
    is required: Postgres won't let a new enum value be used in the same transaction that adds it.

    `IF NOT EXISTS` matters here specifically: downgrade() can't remove the value once added (see
    its own docstring), so a downgrade -> upgrade cycle re-runs this ADD VALUE against a type that
    already has it — discovered by actually running that cycle, not by reasoning about it."""
    op.add_column(
        "models",
        sa.Column("supports_vision", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.execute(
        f"UPDATE models SET supports_vision = true "
        f"WHERE provider_model_id ~* '{_VISION_ID_PATTERN}'"
    )
    op.execute("ALTER TYPE extract_status ADD VALUE IF NOT EXISTS 'PASSTHROUGH'")


def downgrade() -> None:
    """Drops the column cleanly. The enum value is NOT removed: Postgres has no `ALTER TYPE ...
    DROP VALUE`, so reversing it would mean rebuilding the type from scratch (rename, recreate,
    swap the column over, drop the old one) — a bigger, riskier operation than this downgrade
    path is worth, given no upgrade in this codebase has ever needed to roll back past it."""
    op.drop_column("models", "supports_vision")
