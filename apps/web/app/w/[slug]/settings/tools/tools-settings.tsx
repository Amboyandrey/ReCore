"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useRequireAuth } from "@/lib/auth-context";
import { disableTool, enableWebSearch, listTools, ToolError, type Tool } from "@/lib/tool-client";
import { useWorkspaceFlags } from "@/lib/use-workspace-flags";
import { useWorkspaceBySlug } from "@/lib/workspace-context";

// The tools settings page: register the built-in web search (and, in a later phase, third-party
// HTTP tools) that a chat in this workspace can call. Open to any member, not just admins — the
// same floor sending a message already has, since a tool is only ever usable inside a chat.
export function ToolsSettings({ slug }: { slug: string }) {
  const { loading: authLoading } = useRequireAuth();
  const { workspace, loading: wsLoading } = useWorkspaceBySlug(slug);
  const { flags, loading: flagsLoading } = useWorkspaceFlags(workspace?.id);
  const toolsEnabled = flags.tools === true;

  const [tools, setTools] = useState<Tool[]>([]);
  const [loadingData, setLoadingData] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    // Deferred into a .then()/.catch() chain, never called directly at the effect's top level —
    // calling setState synchronously there risks cascading renders under Next's stricter
    // react-hooks rules (see workspace-context.tsx for the same reasoning).
    let cancelled = false;
    const task = workspace && toolsEnabled ? listTools(workspace.id) : Promise.resolve<Tool[]>([]);
    task
      .then((fetched) => {
        if (!cancelled) setTools(fetched);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ToolError ? err.message : "Something went wrong.");
      })
      .finally(() => !cancelled && setLoadingData(false));
    return () => {
      cancelled = true;
    };
  }, [workspace, toolsEnabled]);

  const webSearch = tools.find((t) => t.name === "web_search");

  async function handleEnableWebSearch(e: FormEvent) {
    e.preventDefault();
    if (!workspace || !apiKey.trim()) return;
    setSaving(true);
    setError(null);
    try {
      const tool = await enableWebSearch(workspace.id, apiKey);
      setTools((prev) => [...prev.filter((t) => t.id !== tool.id), tool]);
      setApiKey("");
    } catch (err) {
      setError(err instanceof ToolError ? err.message : "Something went wrong.");
    } finally {
      setSaving(false);
    }
  }

  async function handleDisable(toolId: string) {
    if (!workspace) return;
    setError(null);
    try {
      await disableTool(workspace.id, toolId);
      setTools((prev) => prev.map((t) => (t.id === toolId ? { ...t, enabled: false } : t)));
    } catch (err) {
      setError(err instanceof ToolError ? err.message : "Something went wrong.");
    }
  }

  if (authLoading || wsLoading || flagsLoading || loadingData) {
    return <div className="mx-auto max-w-3xl px-6 py-16 text-sm text-text-muted">Loading…</div>;
  }
  if (!workspace) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-16">
        <p className="text-sm text-text-soft">You don&apos;t have access to this workspace.</p>
      </div>
    );
  }
  if (!toolsEnabled) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-16">
        <h1 className="text-2xl font-semibold tracking-tight text-text">Tools</h1>
        <p className="mt-6 text-sm text-text-soft">
          This feature isn&apos;t enabled for your workspace yet.
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-16">
      <h1 className="text-2xl font-semibold tracking-tight text-text">Tools</h1>
      <p className="mt-1 text-sm text-text-muted">{workspace.name}</p>

      {error && (
        <p className="mt-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      <div className="mt-8">
        <div className="flex items-center gap-3">
          <h2 className="text-sm font-semibold text-text">Web search</h2>
          {webSearch && (
            <span
              className={`rounded-full border px-2 py-0.5 text-xs ${
                webSearch.enabled
                  ? "border-accent/40 bg-accent/10 text-accent"
                  : "border-border text-text-muted"
              }`}
            >
              {webSearch.enabled ? "Enabled" : "Disabled"}
            </span>
          )}
        </div>
        <p className="mt-1 text-sm text-text-muted">
          Lets a chat search the web for current information, backed by Tavily.
        </p>
        <form onSubmit={handleEnableWebSearch} className="mt-3 flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1.5 text-sm">
            <span className="text-text-soft">Tavily API key</span>
            <input
              type="password"
              required
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={webSearch ? "Enter a new key to rotate it" : "tvly-..."}
              className="w-64 rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
            />
          </label>
          <button
            type="submit"
            disabled={saving || !apiKey.trim()}
            className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-60"
          >
            {saving ? "Saving…" : webSearch ? "Update key" : "Enable"}
          </button>
          {webSearch?.enabled && (
            <button
              type="button"
              onClick={() => handleDisable(webSearch.id)}
              className="text-xs text-danger hover:underline"
            >
              Disable
            </button>
          )}
        </form>
      </div>
    </div>
  );
}
