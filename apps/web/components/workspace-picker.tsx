"use client";

import Link from "next/link";
import { useAuth } from "@/lib/auth-context";
import { useWorkspaces } from "@/lib/workspace-context";

// Replaces the placeholder "build order" card that used to sit on the landing page: once signed
// in, this is the first thing there is to do — jump back into a workspace already worked in, or
// get pointed at creating the first one. Renders nothing for a signed-out visitor; the page above
// it (system status, the pitch) still makes sense with no account at all.
export function WorkspacePicker() {
  const { user, loading: authLoading } = useAuth();
  const { workspaces, loading: workspacesLoading } = useWorkspaces();

  if (!user || authLoading) return null;

  return (
    <div className="mt-6 rounded-lg border border-border bg-surface p-6">
      <h2 className="font-mono text-xs uppercase tracking-wider text-text-muted">Your workspaces</h2>
      {workspacesLoading ? (
        <p className="mt-4 text-sm text-text-muted">Loading…</p>
      ) : workspaces.length === 0 ? (
        <>
          <p className="mt-3 text-sm text-text-soft">
            You don&apos;t belong to a workspace yet — create one to start chatting.
          </p>
          <Link
            href="/workspaces/new"
            className="mt-4 inline-block rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-contrast"
          >
            Create a workspace
          </Link>
        </>
      ) : (
        <>
          <div className="mt-4 flex flex-wrap gap-2">
            {workspaces.map((w) => (
              <Link
                key={w.id}
                href={`/w/${w.slug}`}
                className="rounded-full border border-border px-3 py-1.5 text-sm text-text-soft hover:border-border-strong hover:text-text"
              >
                {w.name}
              </Link>
            ))}
          </div>
          <Link href="/workspaces/new" className="mt-4 inline-block text-sm text-accent">
            + New workspace
          </Link>
        </>
      )}
    </div>
  );
}
