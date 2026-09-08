"""row level security

Revision ID: 4294287e75c7
Revises: 5f97f36064b9
Create Date: 2026-09-08 11:00:00.000000

Introduces `recore_app`: a low-privilege Postgres role granted CRUD (no DDL) on every app table.
Row-level security is otherwise nearly theater — Postgres exempts a table's *owner* from RLS by
default, and the owner is exactly who runs migrations and would otherwise serve requests too. The
app's own runtime engine connects as `recore_app` when `APP_DATABASE_URL` is set (see
app/core/db.py); Alembic and the test suite keep using the owner role unconditionally, so this
migration changes nothing about how migrations or `pytest` connect — RLS is inert for both,
by design, and every existing test keeps working unchanged.

Reads are gated by `app.workspace_id`; writes are not — the failure mode row-level security exists
for here is a query that forgot its `WHERE workspace_id = ...` clause leaking rows into a response,
not a write path. Gating INSERT/UPDATE too would require every write path (not just every request)
to set `app.workspace_id` first, including the background generation task's own connection — real,
but a bigger and riskier change than this phase's "backstop under application scoping" framing
calls for. That said, **a `FOR SELECT`-only policy does not simply leave writes unrestricted**:
once `FORCE ROW LEVEL SECURITY` is set, Postgres default-denies any command with no applicable
policy at all — an INSERT under a table with only a SELECT policy is rejected outright, discovered
the hard way by actually running this against a live app rather than reasoning about it. Each
table below therefore also gets explicit, unconditional allow-policies for INSERT/UPDATE/DELETE,
scoped to those commands specifically — a combined `FOR ALL USING (true)` policy would OR against
the SELECT policy and make every row readable too, since permissive policies for the same command
combine with OR.

`workspaces` and `workspace_members` are deliberately excluded: `GET /workspaces` legitimately
spans every workspace one user belongs to, so a single-`app.workspace_id`-per-request policy
would break it. That query is scoped by `user_id` in application code today, same as before.

`audit_logs` is excluded for a related but distinct reason, also only found by testing this for
real: some of what it records — creating a workspace, accepting an invitation, every flag-admin
action — happens on routes with no single workspace in scope yet (or, for flags, ever). Since
SQLAlchemy's INSERT always asks for `RETURNING` to read back server-generated columns, and
Postgres checks a row against a table's SELECT policy before it can be returned that way (not
just its INSERT policy), an audit row written from one of those routes would fail to insert at
all — `app.workspace_id` unset (or a workspace-scoped audit row written from a request that
never had a reason to set it) can never satisfy `workspace_id = app.workspace_id`. Its reads stay
explicitly filtered by `workspace_id` in services/audit.py, same app-level scoping as always.

Every policy wraps the GUC read in `NULLIF(..., '')` before casting to uuid: a custom (non-
built-in) Postgres parameter that's never been set in a session reports as `''`, not `NULL` —
casting that directly to uuid raises `invalid input syntax`, also only found by testing this
against a real database.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "4294287e75c7"
down_revision: str | Sequence[str] | None = "5f97f36064b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Dev-only, fine to commit — same treatment as MASTER_KEY. Rotate for anything that isn't
# throwaway; a role's password is cluster-wide, not per-database, so it can't be scoped per-env
# the way an application secret can.
_APP_ROLE = "recore_app"
_APP_ROLE_PASSWORD = "recore_app_dev_only"

# workspace_id lives directly on these tables — a plain equality check against the GUC. Every
# route that writes to one of these already runs through get_workspace_ctx (or, for the
# background generation task, calls set_workspace_scope itself) before doing so — see this
# migration's docstring for why audit_logs, written from routes that don't fit that pattern,
# isn't on this list despite also carrying its own workspace_id column.
_DIRECT_TABLES = [
    "invitations",
    "provider_credentials",
    "models",
    "conversations",
    "attachments",
    "usage_events",
]

# messages has no workspace_id of its own — reached through conversations instead.
_ALL_TABLES = [*_DIRECT_TABLES, "messages"]


def _allow_all_writes(table: str) -> None:
    """INSERT/UPDATE/DELETE stay ungated — see this migration's docstring for why a `FOR SELECT`
    policy alone would otherwise default-deny every write to `table`, not just leave it open."""
    op.execute(f"CREATE POLICY allow_writes_insert ON {table} FOR INSERT WITH CHECK (true)")
    op.execute(f"CREATE POLICY allow_writes_update ON {table} FOR UPDATE USING (true) WITH CHECK (true)")
    op.execute(f"CREATE POLICY allow_writes_delete ON {table} FOR DELETE USING (true)")


def upgrade() -> None:
    """Create the low-privilege role (idempotent — roles are cluster-wide) and grant it CRUD,
    then enable and force row-level security with a `FOR SELECT` policy per tenant table."""
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '{_APP_ROLE}') THEN
                CREATE ROLE {_APP_ROLE} LOGIN PASSWORD '{_APP_ROLE_PASSWORD}';
            END IF;
        END
        $$;
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {_APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {_APP_ROLE}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {_APP_ROLE}"
    )

    for table in _DIRECT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY workspace_isolation ON {table} FOR SELECT "
            f"USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)"
        )
        _allow_all_writes(table)

    # messages has no workspace_id of its own — it's reached through conversations, which is
    # itself already RLS'd, so this subquery is filtered twice over (harmless, not wrong).
    op.execute("ALTER TABLE messages ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE messages FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY workspace_isolation ON messages FOR SELECT "
        "USING (conversation_id IN ("
        "  SELECT id FROM conversations "
        "  WHERE workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"
        "))"
    )
    _allow_all_writes("messages")


def downgrade() -> None:
    """Drop every policy and disable RLS. The role itself is left in place, deliberately: it's
    cluster-wide, and another database on the same cluster (the dev/test split this project
    uses) may still have live connections authenticated as it."""
    for table in reversed(_ALL_TABLES):
        op.execute(f"DROP POLICY IF EXISTS allow_writes_delete ON {table}")
        op.execute(f"DROP POLICY IF EXISTS allow_writes_update ON {table}")
        op.execute(f"DROP POLICY IF EXISTS allow_writes_insert ON {table}")
        op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.execute(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {_APP_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {_APP_ROLE}")
