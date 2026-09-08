"""exclude invitations from row level security

Revision ID: 2ca6fbc541b7
Revises: 5f133c55bce8
Create Date: 2026-09-08 19:44:03.930485

`invitations` turns out to have the same problem `audit_logs` was already excluded from RLS for
(see 4294287e75c7's docstring): not every legitimate read of this table has a single workspace in
scope. The admin listing route (`GET /workspaces/{id}/invitations`) does, and was already gated
by `require_role(Role.ADMIN)` at the application layer regardless of RLS. But previewing or
accepting an invite by its token — the whole point of the feature — happens before the caller is
a member of (or even knows) any workspace at all: `get_invitation_by_token` looks a row up by
`token_hash` alone, with no `app.workspace_id` ever set for that connection. Under the
`workspace_isolation` SELECT policy this migration removes, that lookup always returned zero
rows, so every invite link was silently "invalid or expired" for anyone who followed it — the
whole feature was broken in any deployment that sets `APP_DATABASE_URL` (i.e. any real one; the
test suite and `alembic` itself connect as the RLS-exempt table owner, which is exactly why this
was never caught by a test).

The real access control for this table was already at the application layer: the admin-only
listing checks role and workspace membership before it ever queries, and `accept_invitation()`
checks the invited email against the signed-in account. RLS's workspace-id-equality model doesn't
fit a row whose authorization is "possession of an unguessable token" (or, for the newer
accept-by-id flow, "signed in as the invited email") rather than membership in a workspace — so,
like `audit_logs`, `workspaces`, and `workspace_members`, `invitations` is excluded outright
rather than reshaped to fit a policy model it was never a good match for.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "2ca6fbc541b7"
down_revision: str | Sequence[str] | None = "5f133c55bce8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop invitations' RLS policies and disable enforcement — see this migration's docstring."""
    op.execute("DROP POLICY IF EXISTS allow_writes_delete ON invitations")
    op.execute("DROP POLICY IF EXISTS allow_writes_update ON invitations")
    op.execute("DROP POLICY IF EXISTS allow_writes_insert ON invitations")
    op.execute("DROP POLICY IF EXISTS workspace_isolation ON invitations")
    op.execute("ALTER TABLE invitations NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE invitations DISABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    """Restore invitations' original RLS policies, exactly as 4294287e75c7 first created them."""
    op.execute("ALTER TABLE invitations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE invitations FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY workspace_isolation ON invitations FOR SELECT "
        "USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)"
    )
    op.execute("CREATE POLICY allow_writes_insert ON invitations FOR INSERT WITH CHECK (true)")
    op.execute(
        "CREATE POLICY allow_writes_update ON invitations FOR UPDATE USING (true) WITH CHECK (true)"
    )
    op.execute("CREATE POLICY allow_writes_delete ON invitations FOR DELETE USING (true)")
