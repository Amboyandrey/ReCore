"""create knowledge connectors

Revision ID: 09e1569cc052
Revises: b3bd5c6720f3
Create Date: 2026-09-14 09:00:00.000000

Adds ReStore: a workspace's own knowledge connectors (a website to crawl, or a set of uploaded
files), indexed in the background into pgvector, and attachable to assistants so chat can retrieve
from them. Adds the `vector` extension, `models.kind` (chat vs embedding — an admin picks one
embedding model per workspace in `knowledge_settings`), `connectors` + `connector_documents` +
`connector_chunks` (the indexed content), `assistant_connectors` (which connectors an assistant
may use — same shape as `assistant_tools`/`assistant_delegates`), and `message_sources` (which
chunks actually made it into a given reply — the sources sidebar's data, added here so the table
exists ahead of the PR 2 work that populates it). Seeds the `knowledge` feature flag, default off.

Every new workspace-scoped table gets the same row-level-security treatment as the rest of this
schema (see 4294287e75c7's docstring): `recore_app` is the only role this restricts, and is
auto-granted CRUD on new tables via that migration's `ALTER DEFAULT PRIVILEGES`. `assistant_connectors`
has no workspace_id of its own, so its policy joins through `assistants` instead, same as
`assistant_delegates`.

`connector_chunks.embedding` is a pgvector column with **no fixed dimension**: a workspace can
change its embedding model (which changes the dimension), and different connectors may be
re-indexed at different times, so no single dimension could be declared for the whole table. This
also means no HNSW/IVFFlat index is created here — those require a fixed dimension. Retrieval
instead does an exact cosine-distance scan filtered to one assistant's ready, current-model
connectors (see services/knowledge.py), which is fast enough at the per-connector chunk cap this
feature enforces; a per-dimension partial index is a natural addition if that ever stops being true.
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "09e1569cc052"
down_revision: str | Sequence[str] | None = "b3bd5c6720f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

model_kind_enum = postgresql.ENUM("CHAT", "EMBEDDING", name="model_kind")
connector_kind_enum = postgresql.ENUM("WEBSITE", "FILE", name="connector_kind")
connector_status_enum = postgresql.ENUM("PENDING", "INDEXING", "READY", "FAILED", name="connector_status")
document_status_enum = postgresql.ENUM("PENDING", "DONE", "FAILED", name="document_status")

_DIRECT_RLS_TABLES = [
    "knowledge_settings",
    "connectors",
    "connector_documents",
    "connector_chunks",
    "message_sources",
]


def _allow_all_writes(table: str) -> None:
    """INSERT/UPDATE/DELETE stay ungated — see 4294287e75c7's docstring for why a FOR SELECT
    policy alone would otherwise default-deny every write to `table`, not just leave it open."""
    op.execute(f"CREATE POLICY allow_writes_insert ON {table} FOR INSERT WITH CHECK (true)")
    op.execute(f"CREATE POLICY allow_writes_update ON {table} FOR UPDATE USING (true) WITH CHECK (true)")
    op.execute(f"CREATE POLICY allow_writes_delete ON {table} FOR DELETE USING (true)")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    model_kind_enum.create(op.get_bind(), checkfirst=True)
    connector_kind_enum.create(op.get_bind(), checkfirst=True)
    connector_status_enum.create(op.get_bind(), checkfirst=True)
    document_status_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "models",
        sa.Column(
            "kind",
            postgresql.ENUM(name="model_kind", create_type=False),
            nullable=False,
            server_default="CHAT",
        ),
    )

    op.create_table(
        "knowledge_settings",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("embedding_model_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["embedding_model_id"], ["models.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id"),
    )

    op.create_table(
        "connectors",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("kind", postgresql.ENUM(name="connector_kind", create_type=False), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("max_pages", sa.Integer(), server_default="30", nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="connector_status", create_type=False),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("embedding_model_id", sa.Uuid(), nullable=True),
        sa.Column("embedding_dim", sa.Integer(), nullable=True),
        sa.Column("document_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("chunk_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["embedding_model_id"], ["models.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "connector_documents",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("connector_id", sa.Uuid(), nullable=False),
        sa.Column("source_url", sa.String(), nullable=True),
        sa.Column("filename", sa.String(), nullable=True),
        sa.Column("mime", sa.String(), nullable=False),
        sa.Column("size", sa.Integer(), server_default="0", nullable=False),
        sa.Column("storage_key", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("char_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="document_status", create_type=False),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["connector_id"], ["connectors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_connector_documents_connector", "connector_documents", ["connector_id"])

    op.create_table(
        "connector_chunks",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("connector_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("embedding", Vector(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["connector_id"], ["connectors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["connector_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_connector_chunks_connector", "connector_chunks", ["connector_id"])

    op.create_table(
        "assistant_connectors",
        sa.Column("assistant_id", sa.Uuid(), nullable=False),
        sa.Column("connector_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["assistant_id"], ["assistants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connector_id"], ["connectors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("assistant_id", "connector_id"),
    )

    op.create_table(
        "message_sources",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("connector_id", sa.Uuid(), nullable=True),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("snippet", sa.String(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["connector_id"], ["connectors.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_id"], ["connector_documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_message_sources_message", "message_sources", ["message_id"])

    for table in _DIRECT_RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY workspace_isolation ON {table} FOR SELECT "
            f"USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)"
        )
        _allow_all_writes(table)

    op.execute("ALTER TABLE assistant_connectors ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE assistant_connectors FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY workspace_isolation ON assistant_connectors FOR SELECT "
        "USING (assistant_id IN ("
        "  SELECT id FROM assistants "
        "  WHERE workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
        "))"
    )
    _allow_all_writes("assistant_connectors")

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
                "key": "knowledge",
                "description": "Let an assistant retrieve from its attached knowledge connectors "
                "(crawled websites, uploaded files) during chat.",
                "type": "BOOLEAN",
                "default_value": False,
            }
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM feature_flags WHERE key = 'knowledge'")

    op.execute("DROP POLICY IF EXISTS allow_writes_delete ON assistant_connectors")
    op.execute("DROP POLICY IF EXISTS allow_writes_update ON assistant_connectors")
    op.execute("DROP POLICY IF EXISTS allow_writes_insert ON assistant_connectors")
    op.execute("DROP POLICY IF EXISTS workspace_isolation ON assistant_connectors")
    op.execute("ALTER TABLE assistant_connectors NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE assistant_connectors DISABLE ROW LEVEL SECURITY")

    for table in reversed(_DIRECT_RLS_TABLES):
        op.execute(f"DROP POLICY IF EXISTS allow_writes_delete ON {table}")
        op.execute(f"DROP POLICY IF EXISTS allow_writes_update ON {table}")
        op.execute(f"DROP POLICY IF EXISTS allow_writes_insert ON {table}")
        op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_message_sources_message", table_name="message_sources")
    op.drop_table("message_sources")
    op.drop_table("assistant_connectors")
    op.drop_index("ix_connector_chunks_connector", table_name="connector_chunks")
    op.drop_table("connector_chunks")
    op.drop_index("ix_connector_documents_connector", table_name="connector_documents")
    op.drop_table("connector_documents")
    op.drop_table("connectors")
    op.drop_table("knowledge_settings")

    op.drop_column("models", "kind")

    document_status_enum.drop(op.get_bind(), checkfirst=True)
    connector_status_enum.drop(op.get_bind(), checkfirst=True)
    connector_kind_enum.drop(op.get_bind(), checkfirst=True)
    model_kind_enum.drop(op.get_bind(), checkfirst=True)

    # The `vector` extension is deliberately left installed — other objects in the database may
    # depend on it, and CREATE EXTENSION IF NOT EXISTS on a later upgrade is a no-op either way.
