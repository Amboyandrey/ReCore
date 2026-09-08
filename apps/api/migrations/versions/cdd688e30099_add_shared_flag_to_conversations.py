"""add shared flag to conversations

Revision ID: cdd688e30099
Revises: 2ca6fbc541b7
Create Date: 2026-09-08 20:15:00.000000

A conversation is private to whoever started it by default; this flag is how its owner opts it
into being visible (and, since access gates both reading and sending, writable) by the rest of
the workspace — see get_conversation/list_conversations in services/chat.py.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cdd688e30099"
down_revision: str | Sequence[str] | None = "2ca6fbc541b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("shared", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("conversations", "shared")
