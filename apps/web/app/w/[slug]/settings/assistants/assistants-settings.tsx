"use client";

import { useEffect, useState, type FormEvent } from "react";
import {
  AssistantError,
  createAssistant,
  deleteAssistant,
  listAssistants,
  updateAssistant,
  type Assistant,
} from "@/lib/assistant-client";
import { useRequireAuth } from "@/lib/auth-context";
import { listModels, type EnabledModel } from "@/lib/provider-client";
import { listTools, type Tool } from "@/lib/tool-client";
import { useWorkspaceBySlug } from "@/lib/workspace-context";

type AssistantFormState = {
  name: string;
  instructions: string;
  modelId: string; // "" means "workspace default"
  toolIds: Set<string>;
};

const EMPTY_FORM: AssistantFormState = {
  name: "",
  instructions: "",
  modelId: "",
  toolIds: new Set(),
};

// The assistants settings page: save a name + required instructions + an optional preferred
// model + an optional set of tools. Open to any member, not just admins — the same floor
// registering a tool or sending a message already has, since an assistant is only ever usable
// inside a chat.
export function AssistantsSettings({ slug }: { slug: string }) {
  const { loading: authLoading } = useRequireAuth();
  const { workspace, loading: wsLoading } = useWorkspaceBySlug(slug);

  const [assistants, setAssistants] = useState<Assistant[]>([]);
  const [models, setModels] = useState<EnabledModel[]>([]);
  const [tools, setTools] = useState<Tool[]>([]);
  const [loadingData, setLoadingData] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [form, setForm] = useState<AssistantFormState>(EMPTY_FORM);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    // Deferred into a .then()/.catch() chain, never called directly at the effect's top level —
    // see workspace-context.tsx for why: calling setState synchronously there risks cascading
    // renders under Next's stricter react-hooks rules.
    if (!workspace) return;
    let cancelled = false;
    Promise.all([listAssistants(workspace.id), listModels(workspace.id), listTools(workspace.id)])
      .then(([fetchedAssistants, fetchedModels, fetchedTools]) => {
        if (cancelled) return;
        setAssistants(fetchedAssistants);
        setModels(fetchedModels);
        setTools(fetchedTools);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof AssistantError ? err.message : "Something went wrong.");
      })
      .finally(() => !cancelled && setLoadingData(false));
    return () => {
      cancelled = true;
    };
  }, [workspace]);

  function startEditing(assistant: Assistant) {
    setEditingId(assistant.id);
    setForm({
      name: assistant.name,
      instructions: assistant.instructions,
      modelId: assistant.model_id ?? "",
      toolIds: new Set(assistant.tool_ids),
    });
  }

  function cancelEditing() {
    setEditingId(null);
    setForm(EMPTY_FORM);
  }

  function toggleTool(toolId: string, checked: boolean) {
    setForm((f) => {
      const toolIds = new Set(f.toolIds);
      if (checked) toolIds.add(toolId);
      else toolIds.delete(toolId);
      return { ...f, toolIds };
    });
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!workspace) return;
    setError(null);
    setSaving(true);
    try {
      if (editingId) {
        const updated = await updateAssistant(workspace.id, editingId, {
          name: form.name,
          instructions: form.instructions,
          model_id: form.modelId || null,
          tool_ids: [...form.toolIds],
        });
        setAssistants((prev) => prev.map((a) => (a.id === updated.id ? updated : a)));
      } else {
        const created = await createAssistant(workspace.id, {
          name: form.name,
          instructions: form.instructions,
          model_id: form.modelId || undefined,
          tool_ids: [...form.toolIds],
        });
        setAssistants((prev) => [...prev, created]);
      }
      setEditingId(null);
      setForm(EMPTY_FORM);
    } catch (err) {
      setError(err instanceof AssistantError ? err.message : "Something went wrong.");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(assistantId: string) {
    if (!workspace) return;
    setError(null);
    try {
      await deleteAssistant(workspace.id, assistantId);
      setAssistants((prev) => prev.filter((a) => a.id !== assistantId));
      if (editingId === assistantId) cancelEditing();
    } catch (err) {
      setError(err instanceof AssistantError ? err.message : "Something went wrong.");
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

  const modelName = (modelId: string | null) =>
    (modelId && models.find((m) => m.id === modelId)?.display_name) || "Workspace default";

  return (
    <div className="mx-auto max-w-3xl px-6 py-16">
      <h1 className="text-2xl font-semibold tracking-tight text-text">Assistants</h1>
      <p className="mt-1 text-sm text-text-muted">{workspace.name}</p>
      <p className="mt-3 text-sm text-text-muted">
        A saved name, instructions, and (optionally) a preferred model and a set of tools. Chatting
        with an assistant means the model arrives already briefed, and equipped only with the
        tools you assign it.
      </p>

      {error && (
        <p className="mt-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      {assistants.length > 0 && (
        <ul className="mt-8 flex flex-col gap-2">
          {assistants.map((a) => (
            <li key={a.id} className="rounded-md border border-border bg-surface px-3 py-2 text-sm">
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-text">{a.name}</span>
                    <span className="rounded-full border border-border-strong px-2 py-0.5 font-mono text-xs text-text-soft">
                      {modelName(a.model_id)}
                    </span>
                    {a.tool_ids.length > 0 && (
                      <span className="rounded-full border border-accent/40 bg-accent/10 px-2 py-0.5 text-xs text-accent">
                        {a.tool_ids.length} tool{a.tool_ids.length === 1 ? "" : "s"}
                      </span>
                    )}
                  </div>
                  <p className="mt-0.5 truncate text-xs text-text-muted">{a.instructions}</p>
                </div>
                <div className="flex shrink-0 items-center gap-3">
                  <button
                    type="button"
                    onClick={() => startEditing(a)}
                    className="text-xs text-text-soft hover:underline"
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    onClick={() => handleDelete(a.id)}
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
        onSubmit={handleSubmit}
        className="mt-6 flex flex-col gap-3 rounded-md border border-border bg-surface p-4"
      >
        <p className="text-xs font-semibold uppercase tracking-wide text-text-muted">
          {editingId ? "Edit assistant" : "Create an assistant"}
        </p>
        <label className="flex flex-col gap-1.5 text-sm">
          <span className="text-text-soft">Name</span>
          <input
            type="text"
            required
            value={form.name}
            onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            placeholder="Support bot"
            className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
          />
        </label>

        <label className="flex flex-col gap-1.5 text-sm">
          <span className="text-text-soft">Instructions</span>
          <textarea
            required
            rows={4}
            value={form.instructions}
            onChange={(e) => setForm((f) => ({ ...f, instructions: e.target.value }))}
            placeholder="You are a support assistant. Be concise and always cite sources."
            className="rounded-md border border-border bg-surface px-3 py-2 text-sm text-text outline-none focus:border-accent"
          />
        </label>

        <label className="flex flex-col gap-1.5 text-sm">
          <span className="text-text-soft">Model</span>
          <select
            value={form.modelId}
            onChange={(e) => setForm((f) => ({ ...f, modelId: e.target.value }))}
            className="rounded-md border border-border bg-surface px-3 py-2 text-text"
          >
            <option value="">Workspace default</option>
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.display_name}
              </option>
            ))}
          </select>
        </label>

        <div className="flex flex-col gap-1.5 text-sm">
          <span className="text-text-soft">Tools (optional)</span>
          {tools.length === 0 ? (
            <p className="text-xs text-text-muted">
              No tools are registered in this workspace yet.
            </p>
          ) : (
            <div className="flex flex-col gap-1.5">
              {tools.map((t) => (
                <label key={t.id} className="flex items-center gap-2 text-xs text-text-soft">
                  <input
                    type="checkbox"
                    checked={form.toolIds.has(t.id)}
                    onChange={(e) => toggleTool(t.id, e.target.checked)}
                  />
                  <span className="font-mono text-text">{t.name}</span>
                  {!t.enabled && <span className="text-text-muted">(disabled)</span>}
                </label>
              ))}
            </div>
          )}
        </div>

        <div className="flex items-center gap-3">
          <button
            type="submit"
            disabled={saving}
            className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-60"
          >
            {saving ? "Saving…" : editingId ? "Save changes" : "Create assistant"}
          </button>
          {editingId && (
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
  );
}
