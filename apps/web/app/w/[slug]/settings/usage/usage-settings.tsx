"use client";

import { useEffect, useState } from "react";
import { useRequireAuth } from "@/lib/auth-context";
import { getUsage, UsageError, type UsageSummary } from "@/lib/usage-client";
import { useWorkspaceBySlug } from "@/lib/workspace-context";

const WINDOWS = [7, 30, 90] as const;

function formatCost(cost: number): string {
  return `$${cost.toFixed(4)}`;
}

// Tokens and spend, broken down three ways — the "spend by model and member" phase-7 demo.
export function UsageSettings({ slug }: { slug: string }) {
  const { loading: authLoading } = useRequireAuth();
  const { workspace, loading: wsLoading } = useWorkspaceBySlug(slug);
  const isAdmin = workspace?.role === "admin" || workspace?.role === "owner";

  const [days, setDays] = useState<(typeof WINDOWS)[number]>(30);
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [loadingData, setLoadingData] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Deferred into .then()/.finally() — see workspace-context.tsx for why this shape is required:
  // even an early-return branch's setState is flagged if it's reachable synchronously in the
  // effect body, so the "nothing to fetch" case is folded into the same promise chain too.
  useEffect(() => {
    const task = workspace && isAdmin ? getUsage(workspace.id, days) : Promise.resolve(null);
    task
      .then(setUsage)
      .catch((err) => setError(err instanceof UsageError ? err.message : "Something went wrong."))
      .finally(() => setLoadingData(false));
  }, [workspace, isAdmin, days]);

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
  if (!isAdmin) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-16">
        <p className="text-sm text-text-soft">Only workspace admins can view usage.</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-16">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-text">Usage</h1>
          <p className="mt-1 text-sm text-text-muted">{workspace.name}</p>
        </div>
        <div className="flex gap-1 rounded-md border border-border p-0.5">
          {WINDOWS.map((w) => (
            <button
              key={w}
              type="button"
              onClick={() => setDays(w)}
              className={`rounded px-2 py-1 text-xs ${
                w === days ? "bg-accent text-accent-contrast" : "text-text-soft"
              }`}
            >
              {w}d
            </button>
          ))}
        </div>
      </div>

      {error && (
        <p className="mt-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      {usage && usage.by_model.length === 0 ? (
        <p className="mt-8 text-sm text-text-soft">No usage recorded in this window yet.</p>
      ) : (
        usage && (
          <>
            <Section title="By model">
              <Table
                rows={usage.by_model.map((r) => [
                  r.display_name,
                  String(r.tokens_in + r.tokens_out),
                  formatCost(r.cost_usd),
                  String(r.message_count),
                ])}
              />
            </Section>
            <Section title="By member">
              <Table
                rows={usage.by_member.map((r) => [
                  r.email,
                  String(r.tokens_in + r.tokens_out),
                  formatCost(r.cost_usd),
                  String(r.message_count),
                ])}
              />
            </Section>
            <Section title="By day">
              <Table
                rows={usage.by_day.map((r) => [
                  new Date(r.day).toLocaleDateString(),
                  String(r.tokens_in + r.tokens_out),
                  formatCost(r.cost_usd),
                  String(r.message_count),
                ])}
              />
            </Section>
          </>
        )
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mt-8">
      <h2 className="text-sm font-semibold text-text">{title}</h2>
      <div className="mt-3">{children}</div>
    </div>
  );
}

function Table({ rows }: { rows: string[][] }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full min-w-[480px] text-sm">
        <thead>
          <tr className="border-b border-border bg-surface-sunk text-left text-xs uppercase tracking-wide text-text-muted">
            <th className="px-4 py-2 font-medium">Name</th>
            <th className="px-4 py-2 font-medium">Tokens</th>
            <th className="px-4 py-2 font-medium">Cost</th>
            <th className="px-4 py-2 font-medium">Messages</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td className="px-4 py-3 text-text-muted" colSpan={4}>
                Nothing here yet.
              </td>
            </tr>
          ) : (
            rows.map((row, i) => (
              <tr key={i} className="border-b border-border last:border-0">
                {row.map((cell, j) => (
                  <td key={j} className="px-4 py-2 text-text">
                    {cell}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}
