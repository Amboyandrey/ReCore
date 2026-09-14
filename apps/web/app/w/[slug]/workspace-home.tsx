"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRequireAuth } from "@/lib/auth-context";
import { listConversations, type Conversation } from "@/lib/chat-client";
import { useWorkspaceBySlug } from "@/lib/workspace-context";
import { listWorkflows, type Workflow } from "@/lib/workflow-client";

// The workspace landing page: jump into a recent conversation, start a new one, run an existing
// workflow, or manage settings.
export function WorkspaceHome({ slug }: { slug: string }) {
  const { loading: authLoading } = useRequireAuth();
  const { workspace, loading } = useWorkspaceBySlug(slug);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [loadingConversations, setLoadingConversations] = useState(true);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [loadingWorkflows, setLoadingWorkflows] = useState(true);

  useEffect(() => {
    const task = workspace ? listConversations(workspace.id) : Promise.resolve<Conversation[]>([]);
    task.then(setConversations).finally(() => setLoadingConversations(false));
  }, [workspace]);

  useEffect(() => {
    // Caught rather than left to propagate: listWorkflows 404s while the `workflows` flag is off
    // for this workspace, which should just mean "nothing to show here", not a console error.
    const task = workspace ? listWorkflows(workspace.id) : Promise.resolve<Workflow[]>([]);
    task
      .then(setWorkflows)
      .catch(() => setWorkflows([]))
      .finally(() => setLoadingWorkflows(false));
  }, [workspace]);

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
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-text">Conversations</h2>
          <Link href={`/w/${workspace.slug}/c/new`} className="text-sm text-accent">
            + New chat
          </Link>
        </div>

        {loadingConversations ? (
          <p className="mt-3 text-sm text-text-muted">Loading…</p>
        ) : conversations.length === 0 ? (
          <p className="mt-3 text-sm text-text-soft">
            No conversations yet — start one with any model you&apos;ve enabled.
          </p>
        ) : (
          <ul className="mt-3 flex flex-col gap-1">
            {conversations.slice(0, 5).map((c) => (
              <li key={c.id}>
                <Link
                  href={`/w/${workspace.slug}/c/${c.id}`}
                  className="block truncate rounded-md px-2 py-1.5 text-sm text-text-soft hover:bg-surface-sunk hover:text-text"
                >
                  {c.title}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="mt-6 rounded-lg border border-border bg-surface p-6">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-text">Workflows</h2>
          <Link href={`/w/${workspace.slug}/settings/workflows`} className="text-sm text-accent">
            Manage →
          </Link>
        </div>

        {loadingWorkflows ? (
          <p className="mt-3 text-sm text-text-muted">Loading…</p>
        ) : workflows.length === 0 ? (
          <p className="mt-3 text-sm text-text-soft">
            No workflows yet — create one to run assistant steps in the background.
          </p>
        ) : (
          <ul className="mt-3 flex flex-col gap-1">
            {workflows.slice(0, 5).map((w) => (
              <li key={w.id}>
                <Link
                  href={`/w/${workspace.slug}/settings/workflows/${w.id}/runs`}
                  className="block truncate rounded-md px-2 py-1.5 text-sm text-text-soft hover:bg-surface-sunk hover:text-text"
                >
                  {w.name}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="mt-6 rounded-lg border border-border bg-surface-sunk p-6">
        <p className="text-sm text-text-soft">Manage the workspace:</p>
        <div className="mt-3 flex flex-col gap-2">
          <Link href={`/w/${workspace.slug}/settings/members`} className="text-sm text-accent">
            Members &amp; invitations →
          </Link>
          <Link href={`/w/${workspace.slug}/settings/providers`} className="text-sm text-accent">
            LLM providers →
          </Link>
          <Link href={`/w/${workspace.slug}/settings/tools`} className="text-sm text-accent">
            Tools →
          </Link>
          <Link href={`/w/${workspace.slug}/settings/assistants`} className="text-sm text-accent">
            Assistants →
          </Link>
          <Link href={`/w/${workspace.slug}/settings/knowledge`} className="text-sm text-accent">
            Knowledge →
          </Link>
          <Link href={`/w/${workspace.slug}/settings/usage`} className="text-sm text-accent">
            Usage →
          </Link>
          <Link href={`/w/${workspace.slug}/settings/audit`} className="text-sm text-accent">
            Audit log →
          </Link>
        </div>
      </div>
    </div>
  );
}
