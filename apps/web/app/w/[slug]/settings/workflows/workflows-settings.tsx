"use client";

import Link from "next/link";
import { useEffect, useState, type FormEvent } from "react";
import { listAssistants, type Assistant } from "@/lib/assistant-client";
import { useRequireAuth } from "@/lib/auth-context";
import { listModels, type EnabledModel } from "@/lib/provider-client";
import { useWorkspaceFlags } from "@/lib/use-workspace-flags";
import {
  createWorkflow,
  deleteWorkflow,
  listWorkflows,
  updateWorkflow,
  WorkflowError,
  type Workflow,
  type WorkflowStepInput,
} from "@/lib/workflow-client";
import { useWorkspaceBySlug } from "@/lib/workspace-context";

type StepFormState = WorkflowStepInput;

const EMPTY_STEP: StepFormState = { key: "", name: "", assistant_id: "", prompt_template: "" };

type WorkflowFormState = {
  name: string;
  description: string;
  defaultModelId: string; // "" means "none"
  steps: StepFormState[];
};

const EMPTY_FORM: WorkflowFormState = {
  name: "",
  description: "",
  defaultModelId: "",
  steps: [{ ...EMPTY_STEP }],
};

// The Workflows (ReFlow) settings page: save an ordered chain of assistant steps that runs in
// the background on the worker — no chat turn. Open to any member, same floor an assistant or a
// knowledge connector already has.
export function WorkflowsSettingsPage({ slug }: { slug: string }) {
  const { loading: authLoading } = useRequireAuth();
  const { workspace, loading: wsLoading } = useWorkspaceBySlug(slug);
  const { flags, loading: flagsLoading } = useWorkspaceFlags(workspace?.id);
  const workflowsEnabled = flags.workflows === true;

  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [assistants, setAssistants] = useState<Assistant[]>([]);
  const [models, setModels] = useState<EnabledModel[]>([]);
  const [loadingData, setLoadingData] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [form, setForm] = useState<WorkflowFormState>(EMPTY_FORM);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    // Deferred into a .then()/.catch()/.finally() chain, never called directly at the effect's
    // top level — see workspace-context.tsx for why: calling setState synchronously there risks
    // cascading renders under Next's stricter react-hooks rules.
    if (!workspace || !workflowsEnabled) {
      Promise.resolve().then(() => setLoadingData(false));
      return;
    }
    let cancelled = false;
    Promise.all([listWorkflows(workspace.id), listAssistants(workspace.id), listModels(workspace.id)])
      .then(([w, a, m]) => {
        if (cancelled) return;
        setWorkflows(w);
        setAssistants(a);
        setModels(m);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof WorkflowError ? err.message : "Something went wrong.");
      })
      .finally(() => !cancelled && setLoadingData(false));
    return () => {
      cancelled = true;
    };
  }, [workspace, workflowsEnabled]);

  function startEditing(workflow: Workflow) {
    setEditingId(workflow.id);
    setForm({
      name: workflow.name,
      description: workflow.description,
      defaultModelId: workflow.default_model_id ?? "",
      steps: workflow.steps.map((s) => ({
        key: s.key,
        name: s.name,
        assistant_id: s.assistant_id ?? "",
        prompt_template: s.prompt_template,
        requires_approval: s.requires_approval,
      })),
    });
  }

  function cancelEditing() {
    setEditingId(null);
    setForm(EMPTY_FORM);
  }

  function updateStep(index: number, changes: Partial<StepFormState>) {
    setForm((f) => ({
      ...f,
      steps: f.steps.map((s, i) => (i === index ? { ...s, ...changes } : s)),
    }));
  }

  function addStep() {
    setForm((f) => ({ ...f, steps: [...f.steps, { ...EMPTY_STEP }] }));
  }

  function removeStep(index: number) {
    setForm((f) => ({ ...f, steps: f.steps.filter((_, i) => i !== index) }));
  }

  function moveStep(index: number, direction: -1 | 1) {
    setForm((f) => {
      const target = index + direction;
      if (target < 0 || target >= f.steps.length) return f;
      const steps = [...f.steps];
      [steps[index], steps[target]] = [steps[target], steps[index]];
      return { ...f, steps };
    });
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!workspace) return;
    setError(null);
    setSaving(true);
    try {
      const steps = form.steps.map((s) => ({ ...s, key: s.key.trim(), name: s.name.trim() }));
      if (editingId) {
        const updated = await updateWorkflow(workspace.id, editingId, {
          name: form.name,
          description: form.description,
          default_model_id: form.defaultModelId || null,
          steps,
        });
        setWorkflows((prev) => prev.map((w) => (w.id === updated.id ? updated : w)));
      } else {
        const created = await createWorkflow(workspace.id, {
          name: form.name,
          description: form.description,
          default_model_id: form.defaultModelId || null,
          steps,
        });
        setWorkflows((prev) => [...prev, created]);
      }
      setEditingId(null);
      setForm(EMPTY_FORM);
    } catch (err) {
      setError(err instanceof WorkflowError ? err.message : "Something went wrong.");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(workflowId: string) {
    if (!workspace) return;
    setError(null);
    try {
      await deleteWorkflow(workspace.id, workflowId);
      setWorkflows((prev) => prev.filter((w) => w.id !== workflowId));
      if (editingId === workflowId) cancelEditing();
    } catch (err) {
      setError(err instanceof WorkflowError ? err.message : "Something went wrong.");
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
  if (!workflowsEnabled) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-16">
        <h1 className="text-2xl font-semibold tracking-tight text-text">Workflows</h1>
        <p className="mt-6 text-sm text-text-soft">
          This feature isn&apos;t enabled for your workspace yet.
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-16">
      <h1 className="text-2xl font-semibold tracking-tight text-text">Workflows (ReFlow)</h1>
      <p className="mt-1 text-sm text-text-muted">{workspace.name}</p>
      <p className="mt-3 text-sm text-text-muted">
        An ordered chain of assistant steps that runs in the background — no chat turn. Each
        step&apos;s prompt can reference the run&apos;s own input (<code>{"{{input}}"}</code>) and
        an earlier step&apos;s output (<code>{"{{steps.<key>.output}}"}</code>).
      </p>

      {error && (
        <p className="mt-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      {workflows.length > 0 && (
        <ul className="mt-8 flex flex-col gap-2">
          {workflows.map((w) => (
            <li key={w.id} className="rounded-md border border-border bg-surface px-3 py-2 text-sm">
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-text">{w.name}</span>
                    <span className="rounded-full border border-border-strong px-2 py-0.5 text-xs text-text-soft">
                      {w.steps.length} step{w.steps.length === 1 ? "" : "s"}
                    </span>
                    {!w.enabled && (
                      <span className="rounded-full border border-border-strong px-2 py-0.5 text-xs text-text-muted">
                        Disabled
                      </span>
                    )}
                  </div>
                  {w.description && (
                    <p className="mt-0.5 truncate text-xs text-text-muted">{w.description}</p>
                  )}
                </div>
                <div className="flex shrink-0 items-center gap-3">
                  <Link
                    href={`/w/${slug}/settings/workflows/${w.id}/runs`}
                    className="text-xs text-accent hover:underline"
                  >
                    Runs →
                  </Link>
                  <button
                    type="button"
                    onClick={() => startEditing(w)}
                    className="text-xs text-text-soft hover:underline"
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    onClick={() => handleDelete(w.id)}
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
          {editingId ? "Edit workflow" : "Create a workflow"}
        </p>
        <label className="flex flex-col gap-1.5 text-sm">
          <span className="text-text-soft">Name</span>
          <input
            type="text"
            required
            value={form.name}
            onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            placeholder="Weekly digest"
            className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
          />
        </label>
        <label className="flex flex-col gap-1.5 text-sm">
          <span className="text-text-soft">Description (optional)</span>
          <input
            type="text"
            value={form.description}
            onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
            className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
          />
        </label>
        <label className="flex flex-col gap-1.5 text-sm">
          <span className="text-text-soft">Default model (optional)</span>
          <select
            value={form.defaultModelId}
            onChange={(e) => setForm((f) => ({ ...f, defaultModelId: e.target.value }))}
            className="rounded-md border border-border bg-surface px-3 py-2 text-text"
          >
            <option value="">None — every step must use an assistant with its own model</option>
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.display_name}
              </option>
            ))}
          </select>
        </label>

        <div className="mt-2 flex flex-col gap-3">
          <span className="text-sm text-text-soft">Steps</span>
          {form.steps.map((step, index) => (
            <div key={index} className="flex flex-col gap-2 rounded-md border border-border p-3">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold uppercase tracking-wide text-text-muted">
                  Step {index + 1}
                </span>
                <div className="flex items-center gap-2 text-xs">
                  <button
                    type="button"
                    onClick={() => moveStep(index, -1)}
                    disabled={index === 0}
                    className="text-text-soft hover:underline disabled:opacity-40"
                  >
                    ↑
                  </button>
                  <button
                    type="button"
                    onClick={() => moveStep(index, 1)}
                    disabled={index === form.steps.length - 1}
                    className="text-text-soft hover:underline disabled:opacity-40"
                  >
                    ↓
                  </button>
                  <button
                    type="button"
                    onClick={() => removeStep(index)}
                    disabled={form.steps.length === 1}
                    className="text-danger hover:underline disabled:opacity-40"
                  >
                    Remove
                  </button>
                </div>
              </div>
              <div className="flex flex-wrap gap-3">
                <label className="flex flex-1 flex-col gap-1.5 text-sm">
                  <span className="text-text-soft">Key</span>
                  <input
                    type="text"
                    required
                    pattern="^[a-z][a-z0-9_]*$"
                    title="Lowercase letters, digits, and underscores, starting with a letter"
                    value={step.key}
                    onChange={(e) => updateStep(index, { key: e.target.value })}
                    placeholder="research"
                    className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
                  />
                </label>
                <label className="flex flex-1 flex-col gap-1.5 text-sm">
                  <span className="text-text-soft">Name</span>
                  <input
                    type="text"
                    required
                    value={step.name}
                    onChange={(e) => updateStep(index, { name: e.target.value })}
                    placeholder="Research"
                    className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
                  />
                </label>
                <label className="flex flex-1 flex-col gap-1.5 text-sm">
                  <span className="text-text-soft">Assistant</span>
                  <select
                    required
                    value={step.assistant_id}
                    onChange={(e) => updateStep(index, { assistant_id: e.target.value })}
                    className="rounded-md border border-border bg-surface px-3 py-2 text-text"
                  >
                    <option value="" disabled>
                      Choose one
                    </option>
                    {assistants.map((a) => (
                      <option key={a.id} value={a.id}>
                        {a.name}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <label className="flex flex-col gap-1.5 text-sm">
                <span className="text-text-soft">Prompt template</span>
                <textarea
                  required
                  rows={2}
                  value={step.prompt_template}
                  onChange={(e) => updateStep(index, { prompt_template: e.target.value })}
                  placeholder={
                    index === 0
                      ? "{{input}}"
                      : `Summarize: {{steps.${form.steps[index - 1]?.key || "previous"}.output}}`
                  }
                  className="rounded-md border border-border bg-surface px-3 py-2 text-sm text-text outline-none focus:border-accent"
                />
              </label>
              <p className="text-xs text-text-muted">
                Available here: <code>{"{{input}}"}</code>
                {form.steps.slice(0, index).map((s) => (
                  <span key={s.key || `step-${index}`}>
                    {", "}
                    <code>{`{{steps.${s.key || "?"}.output}}`}</code>
                  </span>
                ))}
              </p>
              <label className="flex items-center gap-2 text-xs text-text-soft">
                <input
                  type="checkbox"
                  checked={step.requires_approval ?? false}
                  onChange={(e) => updateStep(index, { requires_approval: e.target.checked })}
                />
                <span>Needs approval before the next step runs</span>
              </label>
            </div>
          ))}
          <button
            type="button"
            onClick={addStep}
            className="self-start text-xs text-accent hover:underline"
          >
            + Add step
          </button>
        </div>

        <div className="mt-2 flex items-center gap-3">
          <button
            type="submit"
            disabled={saving}
            className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-60"
          >
            {saving ? "Saving…" : editingId ? "Save changes" : "Create workflow"}
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

      {assistants.length === 0 && (
        <p className="mt-4 text-xs text-text-muted">
          No assistants exist yet — create one on the{" "}
          <Link href={`/w/${slug}/settings/assistants`} className="text-accent">
            Assistants
          </Link>{" "}
          page first.
        </p>
      )}
    </div>
  );
}
