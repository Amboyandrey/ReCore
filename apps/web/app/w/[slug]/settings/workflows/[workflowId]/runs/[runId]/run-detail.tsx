"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRequireAuth } from "@/lib/auth-context";
import { useWorkspaceFlags } from "@/lib/use-workspace-flags";
import {
  cancelRun,
  getRun,
  streamRunEvents,
  WorkflowError,
  type WorkflowRunDetail,
  type WorkflowRunStatus,
  type WorkflowStepRunStatus,
} from "@/lib/workflow-client";
import { useWorkspaceBySlug } from "@/lib/workspace-context";

const RUN_STATUS_LABEL: Record<WorkflowRunStatus, string> = {
  queued: "Queued",
  running: "Running…",
  waiting_approval: "Awaiting approval",
  succeeded: "Succeeded",
  failed: "Failed",
  rejected: "Rejected",
  canceled: "Canceled",
};

const STEP_STATUS_LABEL: Record<WorkflowStepRunStatus, string> = {
  pending: "Pending",
  running: "Running…",
  waiting_approval: "Awaiting approval",
  succeeded: "Succeeded",
  failed: "Failed",
  skipped: "Skipped",
};

const STEP_STATUS_CLASS: Record<WorkflowStepRunStatus, string> = {
  pending: "border-border text-text-muted",
  running: "border-accent/40 bg-accent/10 text-accent",
  waiting_approval: "border-warning/40 bg-warning/10 text-warning",
  succeeded: "border-success/40 bg-success/10 text-success",
  failed: "border-danger/40 bg-danger/10 text-danger",
  skipped: "border-border text-text-muted",
};

const ACTIVE_STATUSES: WorkflowRunStatus[] = ["queued", "running", "waiting_approval"];

// A single run's step-by-step timeline — live via SSE while the run is active, refetching the
// authoritative persisted row at every step boundary rather than trying to reconstruct state
// purely from events (see chat-thread.tsx's own "refetch after done" pattern for the same reason).
export function WorkflowRunDetailPage({
  slug,
  workflowId,
  runId,
}: {
  slug: string;
  workflowId: string;
  runId: string;
}) {
  const { loading: authLoading } = useRequireAuth();
  const { workspace, loading: wsLoading } = useWorkspaceBySlug(slug);
  const { loading: flagsLoading } = useWorkspaceFlags(workspace?.id);

  const [run, setRun] = useState<WorkflowRunDetail | null>(null);
  const [loadingData, setLoadingData] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [canceling, setCanceling] = useState(false);

  useEffect(() => {
    if (!workspace) return;
    let cancelled = false;

    async function load() {
      if (!workspace) return;
      try {
        const initial = await getRun(workspace.id, runId);
        if (cancelled) return;
        setRun(initial);
        setLoadingData(false);

        if (!ACTIVE_STATUSES.includes(initial.status)) return;
        for await (const evt of streamRunEvents(workspace.id, runId)) {
          if (cancelled) return;
          if (evt.event === "step_done" || evt.event === "step_failed") {
            const refreshed = await getRun(workspace.id, runId);
            if (!cancelled) setRun(refreshed);
          }
        }
        if (cancelled) return;
        const final = await getRun(workspace.id, runId);
        if (!cancelled) setRun(final);
      } catch (err) {
        if (!cancelled) setError(err instanceof WorkflowError ? err.message : "Something went wrong.");
        setLoadingData(false);
      }
    }

    load();
    return () => {
      cancelled = true;
    };
  }, [workspace, runId]);

  async function handleCancel() {
    if (!workspace) return;
    setCanceling(true);
    setError(null);
    try {
      await cancelRun(workspace.id, runId);
      setRun(await getRun(workspace.id, runId));
    } catch (err) {
      setError(err instanceof WorkflowError ? err.message : "Something went wrong.");
    } finally {
      setCanceling(false);
    }
  }

  if (authLoading || wsLoading || flagsLoading || loadingData) {
    return <div className="mx-auto max-w-3xl px-6 py-16 text-sm text-text-muted">Loading…</div>;
  }
  if (!workspace || !run) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-16">
        <p className="text-sm text-text-soft">Not found.</p>
      </div>
    );
  }

  const isActive = ACTIVE_STATUSES.includes(run.status);

  return (
    <div className="mx-auto max-w-3xl px-6 py-16">
      <Link
        href={`/w/${slug}/settings/workflows/${workflowId}/runs`}
        className="text-xs text-accent hover:underline"
      >
        ← Runs
      </Link>
      <div className="mt-2 flex items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold tracking-tight text-text">
          {RUN_STATUS_LABEL[run.status]}
        </h1>
        {isActive && (
          <button
            type="button"
            onClick={handleCancel}
            disabled={canceling}
            className="rounded-md border border-danger px-3 py-1.5 text-xs text-danger disabled:opacity-60"
          >
            {canceling ? "Canceling…" : "Cancel"}
          </button>
        )}
      </div>
      {run.input && <p className="mt-1 whitespace-pre-wrap text-sm text-text-muted">{run.input}</p>}
      {run.attachments.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2">
          {run.attachments.map((a) => (
            <span
              key={a.id}
              title={`${a.mime} · ${(a.size / 1024).toFixed(1)} KB`}
              className="rounded-full border border-border px-2 py-0.5 text-xs text-text-muted"
            >
              {a.original_filename}
            </span>
          ))}
        </div>
      )}
      {run.error && (
        <p className="mt-3 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {run.error}
        </p>
      )}
      {error && (
        <p className="mt-3 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      <div className="mt-8 flex flex-col gap-4">
        {run.steps.map((step) => (
          <div key={step.id} className="rounded-md border border-border bg-surface p-4">
            <div className="flex items-center gap-2">
              <span
                className={`rounded-full border px-2 py-0.5 text-xs ${STEP_STATUS_CLASS[step.status]}`}
              >
                {STEP_STATUS_LABEL[step.status]}
              </span>
              <span className="text-sm text-text">{step.name}</span>
              {step.cost_usd > 0 && (
                <span className="text-xs text-text-muted">${step.cost_usd.toFixed(4)}</span>
              )}
            </div>

            {step.prompt && (
              <details className="mt-2">
                <summary className="cursor-pointer text-xs text-text-soft">Prompt sent</summary>
                <p className="mt-1 whitespace-pre-wrap rounded-md bg-surface-sunk p-2 text-xs text-text-muted">
                  {step.prompt}
                </p>
              </details>
            )}

            {step.output && (
              <p className="mt-2 whitespace-pre-wrap text-sm text-text">{step.output}</p>
            )}
            {step.error && <p className="mt-2 text-sm text-danger">{step.error}</p>}

            {step.invocations.length > 0 && (
              <div className="mt-2 flex flex-col gap-1">
                {step.invocations.map((inv, i) => (
                  <div
                    key={i}
                    className={`rounded-md border px-2 py-1 text-xs ${
                      inv.status === "error"
                        ? "border-danger/40 bg-danger/10 text-danger"
                        : "border-border bg-surface-sunk text-text-muted"
                    }`}
                  >
                    🔧 <span className="font-medium">{inv.name}</span> —{" "}
                    {inv.status === "success" ? "done" : "failed"}
                  </div>
                ))}
              </div>
            )}

            {step.sources.length > 0 && (
              <div className="mt-2 flex flex-col gap-1">
                {step.sources.map((s) => (
                  <div key={s.ordinal} className="rounded-md border border-border px-2 py-1 text-xs">
                    <span className="font-medium text-text">{s.label}</span>
                    <p className="mt-0.5 line-clamp-2 text-text-muted">{s.snippet}</p>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
