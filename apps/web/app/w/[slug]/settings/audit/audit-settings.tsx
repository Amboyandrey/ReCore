"use client";

import { useEffect, useState } from "react";
import { useRequireAuth } from "@/lib/auth-context";
import { AuditError, listAuditLogs, type AuditLogEntry } from "@/lib/audit-client";
import { useWorkspaceBySlug } from "@/lib/workspace-context";

const PAGE_SIZE = 25;

// The audit trail — owner-only, per ARCHITECTURE.md's route table. Cursor-paginated: "Load more"
// asks for whatever's older than the last row already shown, never an OFFSET.
export function AuditSettings({ slug }: { slug: string }) {
  const { loading: authLoading } = useRequireAuth();
  const { workspace, loading: wsLoading } = useWorkspaceBySlug(slug);
  const isOwner = workspace?.role === "owner";

  const [entries, setEntries] = useState<AuditLogEntry[]>([]);
  const [loadingData, setLoadingData] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Deferred into .then()/.finally() — see workspace-context.tsx for why this shape is required:
  // even an early-return branch's setState is flagged if it's reachable synchronously in the
  // effect body, so the "nothing to fetch" case is folded into the same promise chain too.
  useEffect(() => {
    const task =
      workspace && isOwner
        ? listAuditLogs(workspace.id, { limit: PAGE_SIZE })
        : Promise.resolve<AuditLogEntry[]>([]);
    task
      .then((page) => {
        setEntries(page);
        setHasMore(page.length === PAGE_SIZE);
      })
      .catch((err) => setError(err instanceof AuditError ? err.message : "Something went wrong."))
      .finally(() => setLoadingData(false));
  }, [workspace, isOwner]);

  async function handleLoadMore() {
    if (!workspace || entries.length === 0) return;
    setLoadingMore(true);
    try {
      const page = await listAuditLogs(workspace.id, {
        limit: PAGE_SIZE,
        before: entries[entries.length - 1].created_at,
      });
      setEntries((prev) => [...prev, ...page]);
      setHasMore(page.length === PAGE_SIZE);
    } catch (err) {
      setError(err instanceof AuditError ? err.message : "Something went wrong.");
    } finally {
      setLoadingMore(false);
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
  if (!isOwner) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-16">
        <p className="text-sm text-text-soft">Only the workspace owner can view the audit log.</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-16">
      <h1 className="text-2xl font-semibold tracking-tight text-text">Audit log</h1>
      <p className="mt-1 text-sm text-text-muted">{workspace.name}</p>

      {error && (
        <p className="mt-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      {entries.length === 0 ? (
        <p className="mt-8 text-sm text-text-soft">Nothing recorded yet.</p>
      ) : (
        <ul className="mt-6 flex flex-col gap-2">
          {entries.map((e) => (
            <li key={e.id} className="rounded-md border border-border bg-surface px-3 py-2 text-sm">
              <div className="flex items-center justify-between">
                <span className="font-mono text-xs text-accent">{e.action}</span>
                <span className="text-xs text-text-muted">{new Date(e.created_at).toLocaleString()}</span>
              </div>
              <p className="mt-1 text-xs text-text-soft">
                {e.target_type}:{e.target_id} &middot; from {e.ip}
              </p>
              {e.event_metadata && (
                <pre className="mt-1 overflow-x-auto rounded bg-surface-sunk px-2 py-1 font-mono text-xs text-text-muted">
                  {JSON.stringify(e.event_metadata)}
                </pre>
              )}
            </li>
          ))}
        </ul>
      )}

      {hasMore && entries.length > 0 && (
        <button
          type="button"
          onClick={handleLoadMore}
          disabled={loadingMore}
          className="mt-4 rounded-md border border-border px-3 py-2 text-sm text-text-soft disabled:opacity-60"
        >
          {loadingMore ? "Loading…" : "Load more"}
        </button>
      )}
    </div>
  );
}
