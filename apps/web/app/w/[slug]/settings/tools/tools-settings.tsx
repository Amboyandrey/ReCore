"use client";

import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useRequireAuth } from "@/lib/auth-context";
import {
  createHttpTool,
  deleteTool,
  enableWebSearch,
  listTools,
  ToolError,
  updateTool,
  type HttpMethod,
  type Tool,
} from "@/lib/tool-client";
import { useWorkspaceFlags } from "@/lib/use-workspace-flags";
import { useWorkspaceBySlug } from "@/lib/workspace-context";

const HTTP_METHODS: HttpMethod[] = ["GET", "POST", "PUT", "PATCH", "DELETE"];

const PARAMETERS_TEMPLATE = JSON.stringify(
  { type: "object", properties: { query: { type: "string" } }, required: ["query"] },
  null,
  2
);

type HttpToolFormState = {
  name: string;
  description: string;
  parametersJson: string;
  method: HttpMethod;
  url: string;
  secretHeader: string;
  secretValue: string;
};

const EMPTY_FORM: HttpToolFormState = {
  name: "",
  description: "",
  parametersJson: PARAMETERS_TEMPLATE,
  method: "GET",
  url: "",
  secretHeader: "",
  secretValue: "",
};

// The tools settings page: enable the built-in web search, and register or manage third-party
// HTTP tools that a chat in this workspace can call. Open to any member, not just admins — the
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
  const [savingWebSearch, setSavingWebSearch] = useState(false);

  const [form, setForm] = useState<HttpToolFormState>(EMPTY_FORM);
  const [editingToolId, setEditingToolId] = useState<string | null>(null);
  const [savingTool, setSavingTool] = useState(false);

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
  const httpTools = useMemo(() => tools.filter((t) => t.kind === "http"), [tools]);

  async function handleEnableWebSearch(e: FormEvent) {
    e.preventDefault();
    if (!workspace || !apiKey.trim()) return;
    setSavingWebSearch(true);
    setError(null);
    try {
      const tool = await enableWebSearch(workspace.id, apiKey);
      setTools((prev) => [...prev.filter((t) => t.id !== tool.id), tool]);
      setApiKey("");
    } catch (err) {
      setError(err instanceof ToolError ? err.message : "Something went wrong.");
    } finally {
      setSavingWebSearch(false);
    }
  }

  async function handleToggleEnabled(tool: Tool) {
    if (!workspace) return;
    setError(null);
    try {
      const updated = await updateTool(workspace.id, tool.id, { enabled: !tool.enabled });
      setTools((prev) => prev.map((t) => (t.id === updated.id ? updated : t)));
    } catch (err) {
      setError(err instanceof ToolError ? err.message : "Something went wrong.");
    }
  }

  async function handleDelete(toolId: string) {
    if (!workspace) return;
    setError(null);
    try {
      await deleteTool(workspace.id, toolId);
      setTools((prev) => prev.filter((t) => t.id !== toolId));
      if (editingToolId === toolId) {
        setEditingToolId(null);
        setForm(EMPTY_FORM);
      }
    } catch (err) {
      setError(err instanceof ToolError ? err.message : "Something went wrong.");
    }
  }

  function startEditing(tool: Tool) {
    setEditingToolId(tool.id);
    setForm({
      name: tool.name,
      description: tool.description,
      parametersJson: JSON.stringify(tool.parameters, null, 2),
      method: tool.method ?? "GET",
      url: tool.url ?? "",
      secretHeader: tool.secret_header ?? "",
      secretValue: "",
    });
  }

  function cancelEditing() {
    setEditingToolId(null);
    setForm(EMPTY_FORM);
  }

  async function handleSubmitHttpTool(e: FormEvent) {
    e.preventDefault();
    if (!workspace) return;
    setError(null);

    let parameters: Record<string, unknown>;
    try {
      parameters = JSON.parse(form.parametersJson) as Record<string, unknown>;
    } catch {
      setError("Parameters must be valid JSON Schema.");
      return;
    }

    setSavingTool(true);
    try {
      if (editingToolId) {
        const updated = await updateTool(workspace.id, editingToolId, {
          name: form.name,
          description: form.description,
          parameters,
          method: form.method,
          url: form.url,
          secret_header: form.secretHeader.trim() || undefined,
          ...(form.secretValue.trim() ? { secret_value: form.secretValue } : {}),
        });
        setTools((prev) => prev.map((t) => (t.id === updated.id ? updated : t)));
      } else {
        const created = await createHttpTool(workspace.id, {
          name: form.name,
          description: form.description,
          parameters,
          method: form.method,
          url: form.url,
          secret_header: form.secretHeader.trim() || undefined,
          secret_value: form.secretValue.trim() || undefined,
        });
        setTools((prev) => [...prev, created]);
      }
      setEditingToolId(null);
      setForm(EMPTY_FORM);
    } catch (err) {
      setError(err instanceof ToolError ? err.message : "Something went wrong.");
    } finally {
      setSavingTool(false);
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
            disabled={savingWebSearch || !apiKey.trim()}
            className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-60"
          >
            {savingWebSearch ? "Saving…" : webSearch ? "Update key" : "Enable"}
          </button>
          {webSearch && (
            <button
              type="button"
              onClick={() => handleToggleEnabled(webSearch)}
              className="text-xs text-danger hover:underline"
            >
              {webSearch.enabled ? "Disable" : "Re-enable"}
            </button>
          )}
        </form>
      </div>

      <div className="mt-10">
        <h2 className="text-sm font-semibold text-text">Third-party HTTP tools</h2>
        <p className="mt-1 text-sm text-text-muted">
          Register any HTTP endpoint as a tool the model can call — its arguments become the
          request, and the response is fed back as the result.
        </p>

        {httpTools.length > 0 && (
          <ul className="mt-4 flex flex-col gap-2">
            {httpTools.map((t) => (
              <li key={t.id} className="rounded-md border border-border bg-surface px-3 py-2 text-sm">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-text">{t.name}</span>
                      <span className="rounded-full border border-border-strong px-2 py-0.5 font-mono text-xs text-text-soft">
                        {t.method}
                      </span>
                      <span
                        className={`rounded-full border px-2 py-0.5 text-xs ${
                          t.enabled
                            ? "border-accent/40 bg-accent/10 text-accent"
                            : "border-border text-text-muted"
                        }`}
                      >
                        {t.enabled ? "Enabled" : "Disabled"}
                      </span>
                    </div>
                    <p className="mt-0.5 truncate text-xs text-text-muted">{t.url}</p>
                    {t.description && (
                      <p className="mt-0.5 truncate text-xs text-text-soft">{t.description}</p>
                    )}
                  </div>
                  <div className="flex shrink-0 items-center gap-3">
                    <button
                      type="button"
                      onClick={() => handleToggleEnabled(t)}
                      className="text-xs text-text-soft hover:underline"
                    >
                      {t.enabled ? "Disable" : "Enable"}
                    </button>
                    <button
                      type="button"
                      onClick={() => startEditing(t)}
                      className="text-xs text-text-soft hover:underline"
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      onClick={() => handleDelete(t.id)}
                      className="text-xs text-danger hover:underline"
                    >
                      Delete
                    </button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}

        <form
          onSubmit={handleSubmitHttpTool}
          className="mt-4 flex flex-col gap-3 rounded-md border border-border bg-surface p-4"
        >
          <p className="text-xs font-semibold uppercase tracking-wide text-text-muted">
            {editingToolId ? "Edit tool" : "Register a tool"}
          </p>
          <div className="flex flex-wrap gap-3">
            <label className="flex flex-1 min-w-[10rem] flex-col gap-1.5 text-sm">
              <span className="text-text-soft">Name</span>
              <input
                type="text"
                required
                pattern="[a-zA-Z0-9_-]+"
                title="Letters, digits, underscores, and hyphens only — this is the function name the model calls."
                value={form.name}
                onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                placeholder="get_weather"
                className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
              />
            </label>
            <label className="flex flex-col gap-1.5 text-sm">
              <span className="text-text-soft">Method</span>
              <select
                value={form.method}
                onChange={(e) => setForm((f) => ({ ...f, method: e.target.value as HttpMethod }))}
                className="rounded-md border border-border bg-surface px-3 py-2 text-text"
              >
                {HTTP_METHODS.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-1 min-w-[14rem] flex-col gap-1.5 text-sm">
              <span className="text-text-soft">URL</span>
              <input
                type="url"
                required
                value={form.url}
                onChange={(e) => setForm((f) => ({ ...f, url: e.target.value }))}
                placeholder="https://api.example.com/weather"
                className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
              />
            </label>
          </div>

          <label className="flex flex-col gap-1.5 text-sm">
            <span className="text-text-soft">Description (what the model is told this tool does)</span>
            <input
              type="text"
              required
              value={form.description}
              onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
              placeholder="Look up the current weather for a city."
              className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
            />
          </label>

          <label className="flex flex-col gap-1.5 text-sm">
            <span className="text-text-soft">Parameters (JSON Schema)</span>
            <textarea
              required
              rows={6}
              value={form.parametersJson}
              onChange={(e) => setForm((f) => ({ ...f, parametersJson: e.target.value }))}
              className="rounded-md border border-border bg-surface px-3 py-2 font-mono text-xs text-text outline-none focus:border-accent"
            />
          </label>

          <div className="flex flex-wrap gap-3">
            <label className="flex flex-1 min-w-[10rem] flex-col gap-1.5 text-sm">
              <span className="text-text-soft">Secret header (optional)</span>
              <input
                type="text"
                value={form.secretHeader}
                onChange={(e) => setForm((f) => ({ ...f, secretHeader: e.target.value }))}
                placeholder="X-Api-Key"
                className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
              />
            </label>
            <label className="flex flex-1 min-w-[10rem] flex-col gap-1.5 text-sm">
              <span className="text-text-soft">
                Secret value {editingToolId ? "(leave blank to keep the current one)" : "(optional)"}
              </span>
              <input
                type="password"
                value={form.secretValue}
                onChange={(e) => setForm((f) => ({ ...f, secretValue: e.target.value }))}
                placeholder={
                  editingToolId
                    ? tools.find((t) => t.id === editingToolId)?.has_secret
                      ? "••••"
                      : ""
                    : ""
                }
                className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
              />
            </label>
          </div>

          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={savingTool}
              className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-60"
            >
              {savingTool ? "Saving…" : editingToolId ? "Save changes" : "Register tool"}
            </button>
            {editingToolId && (
              <button
                type="button"
                onClick={cancelEditing}
                className="text-xs text-text-soft hover:underline"
              >
                Cancel
              </button>
            )}
          </div>
        </form>
      </div>
    </div>
  );
}
