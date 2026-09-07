"use client";

import Link from "next/link";
import { useRequireAuth } from "@/lib/auth-context";
import { useWorkspaceBySlug } from "@/lib/workspace-context";

// The workspace landing page — a shell today; chat and settings fill it in over later phases.
export function WorkspaceHome({ slug }: { slug: string }) {
  const { loading: authLoading } = useRequireAuth();
  const { workspace, loading } = useWorkspaceBySlug(slug);

  if (authLoading || loading) {
    return <div className="mx-auto max-w-3xl px-6 py-16 text-sm text-text-muted">Loading…</div>;
  }

  if (!workspace) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-16">
        <p className="text-sm text-text-soft">
          You don&apos;t have access to this workspace, or it doesn&apos;t exist.
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-16">
      <h1 className="text-2xl font-semibold tracking-tight text-text">{workspace.name}</h1>
      <p className="mt-1 font-mono text-sm text-text-muted">
        /w/{workspace.slug} &middot; your role: {workspace.role}
      </p>

      <div className="mt-8 rounded-lg border border-border bg-surface p-6">
        <p className="text-sm text-text-soft">
          Chat is coming in a later phase. For now, manage who&apos;s here:
        </p>
        <Link
          href={`/w/${workspace.slug}/settings/members`}
          className="mt-3 inline-block text-sm text-accent"
        >
          Members &amp; invitations →
        </Link>
      </div>
    </div>
  );
}
