"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useRequireAuth } from "@/lib/auth-context";
import { useWorkspaceFlags } from "@/lib/use-workspace-flags";
import {
  disableWebhook,
  enableWebhook,
  getWorkflow,
  listRuns,
  startRun,
  uploadRunAttachment,
  webhookUrl,
  WorkflowError,
  type Workflow,
  type WorkflowRun,
  type WorkflowRunStatus,
} from "@/lib/workflow-client";
import { useWorkspaceBySlug } from "@/lib/workspace-context";

const STATUS_LABEL: Record<WorkflowRunStatus, string> = {
  queued: "Queued",
  running: "Running…",
  waiting_approval: "Awaiting approval",
  succeeded: "Succeeded",
  failed: "Failed",
  rejected: "Rejected",
  canceled: "Canceled",
};

const STATUS_CLASS: Record<WorkflowRunStatus, string> = {
  queued: "border-border text-text-muted",
  running: "border-accent/40 bg-accent/10 text-accent",
  waiting_approval: "border-warning/40 bg-warning/10 text-warning",
  succeeded: "border-success/40 bg-success/10 text-success",
  failed: "border-danger/40 bg-danger/10 text-danger",
  rejected: "border-danger/40 bg-danger/10 text-danger",
  canceled: "border-border text-text-muted",
};

const ACTIVE_STATUSES: WorkflowRunStatus[] = ["queued", "running", "waiting_approval"];

// A workflow's run history: a form to start a new run, and a table of past ones — each linking
// to its own live/replayed detail page.
export function WorkflowRunsPage({ slug, workflowId }: { slug: string; workflowId: string }) {
  const { loading: authLoading } = useRequireAuth();
  const { workspace, loading: wsLoading } = useWorkspaceBySlug(slug);
  const { flags, loading: flagsLoading } = useWorkspaceFlags(workspace?.id);
  const workflowsEnabled = flags.workflows === true;

  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [runs, setRuns] = useState<WorkflowRun[]>([]);
  const [loadingData, setLoadingData] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [input, setInput] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [starting, setStarting] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [webhookBusy, setWebhookBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!workspace || !workflowsEnabled) {
      Promise.resolve().then(() => setLoadingData(false));
      return;
    }
    let cancelled = false;
    Promise.all([getWorkflow(workspace.id, workflowId), listRuns(workspace.id, workflowId)])
      .then(([w, r]) => {
        if (cancelled) return;
        setWorkflow(w);
        setRuns(r);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof WorkflowError ? err.message : "Something went wrong.");
      })
      .finally(() => !cancelled && setLoadingData(false));
    return () => {
      cancelled = true;
    };
  }, [workspace, workflowsEnabled, workflowId]);

  // Poll while any run is still active — a live event stream is more than this list needs; a
  // short poll keeps a status pill current without opening an SSE connection per row.
  useEffect(() => {
    if (!workspace || !workflowsEnabled) return;
    if (!runs.some((r) => ACTIVE_STATUSES.includes(r.status))) return;
    const timer = setInterval(() => {
      listRuns(workspace.id, workflowId).then(setRuns).catch(() => undefined);
    }, 5000);
    return () => clearInterval(timer);
  }, [workspace, workflowsEnabled, workflowId, runs]);

  // Uploads each chosen file first, then starts the run with their ids — the same
  // upload-ahead-of-send shape the chat composer follows.
  async function handleStart(e: FormEvent) {
    e.preventDefault();
    if (!workspace || (!input.trim() && files.length === 0)) return;
    setStarting(true);
    setError(null);
    try {
      const uploaded = await Promise.all(files.map((f) => uploadRunAttachment(workspace.id, workflowId, f)));
      const run = await startRun(workspace.id, workflowId, {
        input,
        attachment_ids: uploaded.map((a) => a.id),
      });
      setRuns((prev) => [run, ...prev]);
      setInput("");
      setFiles([]);
      if (fileInputRef.current) fileInputRef.current.value = "";
    } catch (err) {
      setError(err instanceof WorkflowError ? err.message : "Something went wrong.");
    } finally {
      setStarting(false);
    }
  }

  // Enables the webhook, or rotates its secret when it's already on.
  async function handleEnableWebhook() {
    if (!workspace || !workflow) return;
    setWebhookBusy(true);
    setError(null);
    try {
      const secret = await enableWebhook(workspace.id, workflowId);
      setWorkflow({ ...workflow, webhook_secret: secret });
      setCopied(false);
    } catch (err) {
      setError(err instanceof WorkflowError ? err.message : "Something went wrong.");
    } finally {
      setWebhookBusy(false);
    }
  }

  // Turns the webhook off — the old URL stops working immediately.
  async function handleDisableWebhook() {
    if (!workspace || !workflow) return;
    setWebhookBusy(true);
    setError(null);
    try {
      await disableWebhook(workspace.id, workflowId);
      setWorkflow({ ...workflow, webhook_secret: null });
    } catch (err) {
      setError(err instanceof WorkflowError ? err.message : "Something went wrong.");
    } finally {
      setWebhookBusy(false);
    }
  }

  // Copies the hook URL; the confirmation clears itself so the button reads "Copy" again.
  async function handleCopy(url: string) {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("Couldn't copy — select the URL and copy it by hand.");
    }
  }

  if (authLoading || wsLoading || flagsLoading || loadingData) {
    return <div className="mx-auto max-w-3xl px-6 py-16 text-sm text-text-muted">Loading…</div>;
  }
  if (!workspace || !workflowsEnabled || !workflow) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-16">
        <p className="text-sm text-text-soft">Not found.</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-16">
      <Link href={`/w/${slug}/settings/workflows`} className="text-xs text-accent hover:underline">
        ← Workflows
      </Link>
      <h1 className="mt-2 text-2xl font-semibold tracking-tight text-text">{workflow.name}</h1>
      {workflow.description && <p className="mt-1 text-sm text-text-muted">{workflow.description}</p>}

      {error && (
        <p className="mt-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      <form
        onSubmit={handleStart}
        className="mt-6 flex flex-col gap-3 rounded-md border border-border bg-surface p-4"
      >
        <p className="text-xs font-semibold uppercase tracking-wide text-text-muted">Run</p>
        <textarea
          rows={3}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="What should this run's first step start from? Leave empty to run from files alone."
          className="rounded-md border border-border bg-surface px-3 py-2 text-sm text-text outline-none focus:border-accent"
        />
        <div className="flex flex-wrap items-center gap-2">
          <label className="cursor-pointer rounded-md border border-border px-3 py-1.5 text-xs text-text-soft hover:border-border-strong">
            Attach files
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="hidden"
              onChange={(e) => setFiles((prev) => [...prev, ...Array.from(e.target.files ?? [])])}
            />
          </label>
          {files.map((f, i) => (
            <span
              key={`${f.name}-${i}`}
              className="flex items-center gap-1 rounded-full border border-border px-2 py-0.5 text-xs text-text-muted"
            >
              {f.name}
              <button
                type="button"
                aria-label={`Remove ${f.name}`}
                onClick={() => setFiles((prev) => prev.filter((_, j) => j !== i))}
                className="text-text-soft hover:text-text"
              >
                ×
              </button>
            </span>
          ))}
        </div>
        <p className="text-xs text-text-soft">
          Files go to every step that reads <code>{"{{input}}"}</code> — documents as their extracted text,
          images to vision-capable models.
        </p>
        <button
          type="submit"
          disabled={starting || (!input.trim() && files.length === 0)}
          className="self-start rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-60"
        >
          {starting ? "Starting…" : "Run"}
        </button>
      </form>

      <div className="mt-6 rounded-md border border-border bg-surface p-4">
        <div className="flex items-center justify-between gap-3">
          <p className="text-xs font-semibold uppercase tracking-wide text-text-muted">Webhook</p>
          {workflow.webhook_secret ? (
            <div className="flex gap-2">
              <button
                type="button"
                onClick={handleEnableWebhook}
                disabled={webhookBusy}
                className="rounded-md border border-border px-3 py-1.5 text-xs text-text-soft hover:border-border-strong disabled:opacity-60"
              >
                Rotate secret
              </button>
              <button
                type="button"
                onClick={handleDisableWebhook}
                disabled={webhookBusy}
                className="rounded-md border border-danger px-3 py-1.5 text-xs text-danger disabled:opacity-60"
              >
                Disable
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={handleEnableWebhook}
              disabled={webhookBusy}
              className="rounded-md border border-border px-3 py-1.5 text-xs text-text-soft hover:border-border-strong disabled:opacity-60"
            >
              {webhookBusy ? "Enabling…" : "Enable"}
            </button>
          )}
        </div>
        {workflow.webhook_secret ? (
          <div className="mt-3 flex flex-col gap-2">
            <p className="text-xs text-text-muted">
              Anyone with this URL can start a run — treat it like a password. POST JSON{" "}
              <code>{'{"input": "..."}'}</code>, or a multipart form with an optional <code>input</code> field and
              one or more <code>files</code> parts.
            </p>
            <div className="flex items-center gap-2">
              <code className="min-w-0 flex-1 truncate rounded-md bg-surface-sunk px-2 py-1 text-xs text-text">
                {webhookUrl(workspace.id, workflow.webhook_secret)}
              </code>
              <button
                type="button"
                onClick={() => handleCopy(webhookUrl(workspace.id, workflow.webhook_secret ?? ""))}
                className="shrink-0 rounded-md border border-border px-2 py-1 text-xs text-text-soft hover:border-border-strong"
              >
                {copied ? "Copied" : "Copy"}
              </button>
            </div>
            <pre className="overflow-x-auto rounded-md bg-surface-sunk p-2 text-xs text-text-muted">
              {`curl -X POST '${webhookUrl(workspace.id, workflow.webhook_secret)}' \\
  -H 'Content-Type: application/json' \\
  -d '{"input": "Hello from outside"}'`}
            </pre>
          </div>
        ) : (
          <p className="mt-2 text-xs text-text-muted">
            Off. Enable to get a URL that external systems can POST to in order to start a run.
          </p>
        )}
      </div>

      <div className="mt-8">
        <h2 className="text-sm font-semibold text-text">Run history</h2>
        {runs.length === 0 ? (
          <p className="mt-2 text-sm text-text-muted">No runs yet.</p>
        ) : (
          <ul className="mt-3 flex flex-col gap-2">
            {runs.map((r) => (
              <li key={r.id}>
                <Link
                  href={`/w/${slug}/settings/workflows/${workflowId}/runs/${r.id}`}
                  className="flex items-center justify-between gap-3 rounded-md border border-border bg-surface px-3 py-2 text-sm hover:border-border-strong"
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className={`rounded-full border px-2 py-0.5 text-xs ${STATUS_CLASS[r.status]}`}>
                        {STATUS_LABEL[r.status]}
                      </span>
                      <span className="rounded-full border border-border-strong px-2 py-0.5 text-xs text-text-soft">
                        {r.trigger}
                      </span>
                    </div>
                    <p className="mt-0.5 truncate text-xs text-text-muted">
                      {r.input || (r.attachments.length === 0 ? "(no input)" : "")}
                      {r.attachments.length > 0 && (
                        <span className="text-text-soft">
                          {r.input ? " · " : ""}
                          {r.attachments.map((a) => a.original_filename).join(", ")}
                        </span>
                      )}
                    </p>
                  </div>
                  <div className="shrink-0 text-right text-xs text-text-muted">
                    <p>${r.cost_usd.toFixed(4)}</p>
                    <p>{new Date(r.created_at).toLocaleString()}</p>
                  </div>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
