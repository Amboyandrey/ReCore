"use client";

import { useEffect, useState, type FormEvent } from "react";
import {
  createMcpServer,
  deleteMcpServer,
  listMcpServers,
  syncMcpServer,
  ToolError,
  type McpServer,
  type Tool,
} from "@/lib/tool-client";

type McpFormState = { name: string; url: string; authHeader: string; authValue: string };

const EMPTY_FORM: McpFormState = { name: "", url: "", authHeader: "Authorization", authValue: "" };

function message(err: unknown): string {
  return err instanceof ToolError ? err.message : "Something went wrong.";
}

// Connect remote MCP servers and choose which of their tools a chat is offered. The server's
// tools live in the parent's tool list, so every server change asks the parent to reload it.
export function McpServersSection({
  workspaceId,
  tools,
  onToggleTool,
  onToolsChanged,
  onError,
}: {
  workspaceId: string;
  tools: Tool[];
  onToggleTool: (tool: Tool) => void;
  onToolsChanged: () => Promise<void>;
  onError: (message: string | null) => void;
}) {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [form, setForm] = useState<McpFormState>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [busyServerId, setBusyServerId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listMcpServers(workspaceId)
      .then((fetched) => {
        if (!cancelled) setServers(fetched);
      })
      .catch((err) => {
        if (!cancelled) onError(message(err));
      });
    return () => {
      cancelled = true;
    };
  }, [workspaceId, onError]);

  async function handleConnect(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    onError(null);
    try {
      const hasAuth = form.authValue.trim() !== "";
      const created = await createMcpServer(workspaceId, {
        name: form.name.trim(),
        url: form.url.trim(),
        ...(hasAuth ? { auth_header: form.authHeader, auth_value: form.authValue } : {}),
      });
      setServers((prev) => [...prev, created]);
      setForm(EMPTY_FORM);
      await onToolsChanged();
    } catch (err) {
      onError(message(err));
    } finally {
      setSaving(false);
    }
  }

  async function handleSync(serverId: string) {
    setBusyServerId(serverId);
    onError(null);
    try {
      const synced = await syncMcpServer(workspaceId, serverId);
      setServers((prev) => prev.map((s) => (s.id === synced.id ? synced : s)));
      await onToolsChanged();
    } catch (err) {
      onError(message(err));
    } finally {
      setBusyServerId(null);
    }
  }

  async function handleDelete(serverId: string) {
    setBusyServerId(serverId);
    onError(null);
    try {
      await deleteMcpServer(workspaceId, serverId);
      setServers((prev) => prev.filter((s) => s.id !== serverId));
      await onToolsChanged();
    } catch (err) {
      onError(message(err));
    } finally {
      setBusyServerId(null);
    }
  }

  return (
    <div className="mt-10">
      <h2 className="text-sm font-semibold text-text">MCP servers</h2>
      <p className="mt-1 text-sm text-text-muted">
        Connect a remote MCP server over Streamable HTTP. Its tools are imported disabled — enable
        the ones a chat should be offered.
      </p>

      {servers.length > 0 && (
        <ul className="mt-4 flex flex-col gap-2">
          {servers.map((s) => {
            const serverTools = tools.filter((t) => t.mcp_server_id === s.id);
            const busy = busyServerId === s.id;
            return (
              <li key={s.id} className="rounded-md border border-border bg-surface px-3 py-2 text-sm">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <span className="font-mono text-text">{s.name}</span>
                    <p className="mt-0.5 truncate text-xs text-text-muted">{s.url}</p>
                  </div>
                  <div className="flex shrink-0 items-center gap-3">
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => handleSync(s.id)}
                      className="text-xs text-text-soft hover:underline disabled:opacity-60"
                    >
                      {busy ? "Working…" : "Sync"}
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => handleDelete(s.id)}
                      className="text-xs text-danger hover:underline disabled:opacity-60"
                    >
                      Delete
                    </button>
                  </div>
                </div>
                {serverTools.length === 0 ? (
                  <p className="mt-2 text-xs text-text-muted">This server offers no tools.</p>
                ) : (
                  <ul className="mt-2 flex flex-col gap-1 border-t border-border pt-2">
                    {serverTools.map((t) => (
                      <li key={t.id} className="flex items-center justify-between gap-3">
                        <div className="min-w-0">
                          <span className="font-mono text-xs text-text">{t.name}</span>
                          {t.description && (
                            <p className="truncate text-xs text-text-soft">{t.description}</p>
                          )}
                        </div>
                        <button
                          type="button"
                          onClick={() => onToggleTool(t)}
                          className={`shrink-0 rounded-full border px-2 py-0.5 text-xs ${
                            t.enabled
                              ? "border-accent/40 bg-accent/10 text-accent"
                              : "border-border text-text-muted"
                          }`}
                        >
                          {t.enabled ? "Enabled" : "Disabled"}
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      )}

      <form
        onSubmit={handleConnect}
        className="mt-4 flex flex-col gap-3 rounded-md border border-border bg-surface p-4"
      >
        <p className="text-xs font-semibold uppercase tracking-wide text-text-muted">
          Connect a server
        </p>
        <div className="flex flex-wrap gap-3">
          <label className="flex min-w-[10rem] flex-col gap-1.5 text-sm">
            <span className="text-text-soft">Name</span>
            <input
              type="text"
              required
              maxLength={32}
              pattern="[a-zA-Z0-9_-]+"
              title="Letters, digits, underscores, and hyphens only — this prefixes every tool name."
              value={form.name}
              onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
              placeholder="github"
              className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
            />
          </label>
          <label className="flex flex-1 min-w-[14rem] flex-col gap-1.5 text-sm">
            <span className="text-text-soft">URL</span>
            <input
              type="url"
              required
              value={form.url}
              onChange={(e) => setForm((f) => ({ ...f, url: e.target.value }))}
              placeholder="https://mcp.example.com/mcp"
              className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
            />
          </label>
        </div>
        <div className="flex flex-wrap gap-3">
          <label className="flex flex-1 min-w-[10rem] flex-col gap-1.5 text-sm">
            <span className="text-text-soft">Auth header</span>
            <input
              type="text"
              value={form.authHeader}
              onChange={(e) => setForm((f) => ({ ...f, authHeader: e.target.value }))}
              className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
            />
          </label>
          <label className="flex flex-1 min-w-[10rem] flex-col gap-1.5 text-sm">
            <span className="text-text-soft">Auth value (optional)</span>
            <input
              type="password"
              value={form.authValue}
              onChange={(e) => setForm((f) => ({ ...f, authValue: e.target.value }))}
              placeholder="Bearer ..."
              className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
            />
          </label>
        </div>
        <div>
          <button
            type="submit"
            disabled={saving}
            className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-60"
          >
            {saving ? "Connecting…" : "Connect server"}
          </button>
        </div>
      </form>
    </div>
  );
}
