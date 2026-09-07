"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useRequireAuth } from "@/lib/auth-context";
import {
  changeMemberRole,
  createInvitation,
  listInvitations,
  listMembers,
  removeMember,
  MemberError,
  type Invitation,
  type Member,
} from "@/lib/member-client";
import { useWorkspaceBySlug } from "@/lib/workspace-context";
import type { Role } from "@/lib/workspace-client";

const ASSIGNABLE_ROLES: Role[] = ["viewer", "member", "admin"];

// The members & invitations page for one workspace — visible to any member, editable by admins.
export function MembersSettings({ slug }: { slug: string }) {
  const { user, loading: authLoading } = useRequireAuth();
  const { workspace, loading: wsLoading } = useWorkspaceBySlug(slug);
  const isAdmin = workspace?.role === "admin" || workspace?.role === "owner";

  const [members, setMembers] = useState<Member[]>([]);
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [loadingData, setLoadingData] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<Role>("member");
  const [inviting, setInviting] = useState(false);
  const [newInviteLink, setNewInviteLink] = useState<string | null>(null);

  useEffect(() => {
    if (!workspace) return;
    let cancelled = false;
    const tasks = [listMembers(workspace.id).then((m) => !cancelled && setMembers(m))];
    if (isAdmin) tasks.push(listInvitations(workspace.id).then((i) => !cancelled && setInvitations(i)));
    Promise.all(tasks).finally(() => !cancelled && setLoadingData(false));
    return () => {
      cancelled = true;
    };
  }, [workspace, isAdmin]);

  async function handleRoleChange(userId: string, role: Role) {
    if (!workspace) return;
    setError(null);
    try {
      const updated = await changeMemberRole(workspace.id, userId, role);
      setMembers((prev) => prev.map((m) => (m.user_id === userId ? updated : m)));
    } catch (err) {
      setError(err instanceof MemberError ? err.message : "Something went wrong.");
    }
  }

  async function handleRemove(userId: string) {
    if (!workspace) return;
    setError(null);
    try {
      await removeMember(workspace.id, userId);
      setMembers((prev) => prev.filter((m) => m.user_id !== userId));
    } catch (err) {
      setError(err instanceof MemberError ? err.message : "Something went wrong.");
    }
  }

  async function handleInvite(e: FormEvent) {
    e.preventDefault();
    if (!workspace) return;
    setError(null);
    setInviting(true);
    setNewInviteLink(null);
    try {
      const invitation = await createInvitation(workspace.id, inviteEmail, inviteRole);
      setInvitations((prev) => [invitation, ...prev]);
      setNewInviteLink(`${window.location.origin}/invite/${invitation.token}`);
      setInviteEmail("");
    } catch (err) {
      setError(err instanceof MemberError ? err.message : "Something went wrong.");
    } finally {
      setInviting(false);
    }
  }

  if (authLoading || wsLoading || loadingData) {
    return <div className="mx-auto max-w-3xl px-6 py-16 text-sm text-text-muted">Loading…</div>;
  }

  if (!workspace) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-16">
        <p className="text-sm text-text-soft">You don&apos;t have access to this workspace.</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-16">
      <h1 className="text-2xl font-semibold tracking-tight text-text">Members</h1>
      <p className="mt-1 text-sm text-text-muted">{workspace.name}</p>

      {error && (
        <p className="mt-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      <div className="mt-8 overflow-x-auto rounded-lg border border-border">
        <table className="w-full min-w-[420px] text-sm">
          <thead>
            <tr className="border-b border-border bg-surface-sunk text-left text-xs uppercase tracking-wide text-text-muted">
              <th className="px-4 py-2 font-medium">Email</th>
              <th className="px-4 py-2 font-medium">Role</th>
              {isAdmin && <th className="px-4 py-2 font-medium" />}
            </tr>
          </thead>
          <tbody>
            {members.map((m) => (
              <tr key={m.user_id} className="border-b border-border last:border-0">
                <td className="px-4 py-2 text-text">{m.email}</td>
                <td className="px-4 py-2">
                  {isAdmin && m.user_id !== user?.id && m.role !== "owner" ? (
                    <select
                      value={m.role}
                      onChange={(e) => handleRoleChange(m.user_id, e.target.value as Role)}
                      className="rounded-md border border-border bg-surface px-2 py-1 text-sm text-text"
                    >
                      {ASSIGNABLE_ROLES.map((r) => (
                        <option key={r} value={r}>
                          {r}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <span className="font-mono text-xs text-text-muted">{m.role}</span>
                  )}
                </td>
                {isAdmin && (
                  <td className="px-4 py-2 text-right">
                    {m.user_id !== user?.id && m.role !== "owner" && (
                      <button
                        type="button"
                        onClick={() => handleRemove(m.user_id)}
                        className="text-xs text-danger hover:underline"
                      >
                        Remove
                      </button>
                    )}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {isAdmin && (
        <>
          <div className="mt-10">
            <h2 className="text-sm font-semibold text-text">Invite someone</h2>
            <form onSubmit={handleInvite} className="mt-3 flex flex-wrap items-end gap-3">
              <label className="flex flex-col gap-1.5 text-sm">
                <span className="text-text-soft">Email</span>
                <input
                  type="email"
                  required
                  value={inviteEmail}
                  onChange={(e) => setInviteEmail(e.target.value)}
                  className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
                />
              </label>
              <label className="flex flex-col gap-1.5 text-sm">
                <span className="text-text-soft">Role</span>
                <select
                  value={inviteRole}
                  onChange={(e) => setInviteRole(e.target.value as Role)}
                  className="rounded-md border border-border bg-surface px-3 py-2 text-text"
                >
                  {ASSIGNABLE_ROLES.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="submit"
                disabled={inviting}
                className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-60"
              >
                {inviting ? "Sending…" : "Send invite"}
              </button>
            </form>
            {newInviteLink && (
              <p className="mt-3 break-all rounded-md border border-success/30 bg-success/10 px-3 py-2 text-sm text-success">
                Invite created — share this link:{" "}
                <span className="font-mono">{newInviteLink}</span>
              </p>
            )}
          </div>

          {invitations.length > 0 && (
            <div className="mt-8">
              <h2 className="text-sm font-semibold text-text">Pending invitations</h2>
              <ul className="mt-3 flex flex-col gap-2">
                {invitations.map((i) => (
                  <li
                    key={i.id}
                    className="flex items-center justify-between rounded-md border border-border bg-surface px-3 py-2 text-sm"
                  >
                    <span className="text-text-soft">{i.email}</span>
                    <span className="font-mono text-xs text-text-muted">{i.role}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </div>
  );
}
