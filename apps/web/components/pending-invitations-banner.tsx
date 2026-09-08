"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import {
  acceptPendingInvitation,
  listPendingInvitationsForMe,
  MemberError,
  type PendingInvitation,
} from "@/lib/member-client";
import { useWorkspaces } from "@/lib/workspace-context";

// Welcomes a signed-in account to any workspace it's been invited to, whether or not it ever
// followed the original invite link — an invite sent before the account even existed still shows
// up here the moment that email signs in. Rendered once in the root layout, so it surfaces on
// whichever page a fresh login happens to land on.
export function PendingInvitationsBanner() {
  const { user } = useAuth();
  const { refresh } = useWorkspaces();
  const router = useRouter();
  const [invitations, setInvitations] = useState<PendingInvitation[]>([]);
  const [acceptingId, setAcceptingId] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Every branch deferred into a .then() callback, never called directly at the effect's top
    // level — same reasoning as workspace-context.tsx: calling setState synchronously there
    // risks cascading renders under Next's stricter react-hooks rules.
    const task = user ? listPendingInvitationsForMe() : Promise.resolve<PendingInvitation[]>([]);
    task.then(setInvitations).catch(() => {}); // best-effort — shouldn't block the rest of the app
  }, [user]);

  const visible = invitations.filter((i) => !dismissed.has(i.id));
  if (visible.length === 0) return null;

  async function handleAccept(id: string) {
    setAcceptingId(id);
    setError(null);
    try {
      const workspace = await acceptPendingInvitation(id);
      setInvitations((prev) => prev.filter((i) => i.id !== id));
      await refresh();
      router.push(`/w/${workspace.slug}`);
    } catch (err) {
      setError(err instanceof MemberError ? err.message : "Something went wrong.");
    } finally {
      setAcceptingId(null);
    }
  }

  return (
    <div className="border-b border-accent/30 bg-accent/10">
      <div className="mx-auto flex max-w-5xl flex-col gap-2 px-6 py-3">
        {error && <p className="text-sm text-danger">{error}</p>}
        {visible.map((i) => (
          <div key={i.id} className="flex flex-wrap items-center justify-between gap-2 text-sm">
            <span className="text-text">
              You&apos;ve been invited to join <strong>{i.workspace_name}</strong> as{" "}
              <span className="font-mono text-xs">{i.role}</span>.
            </span>
            <div className="flex items-center gap-3">
              <button
                type="button"
                onClick={() => handleAccept(i.id)}
                disabled={acceptingId === i.id}
                className="rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-accent-contrast disabled:opacity-60"
              >
                {acceptingId === i.id ? "Joining…" : "Accept"}
              </button>
              <button
                type="button"
                onClick={() => setDismissed((prev) => new Set(prev).add(i.id))}
                className="text-xs text-text-muted hover:text-text"
              >
                Dismiss
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
