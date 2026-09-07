"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { useRequireAuth } from "@/lib/auth-context";
import { WorkspaceError } from "@/lib/workspace-client";
import { useWorkspaces } from "@/lib/workspace-context";

export default function NewWorkspacePage() {
  useRequireAuth();
  const { create } = useWorkspaces();
  const router = useRouter();
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const workspace = await create(name);
      router.push(`/w/${workspace.slug}`);
    } catch (err) {
      setError(err instanceof WorkspaceError ? err.message : "Something went wrong.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mx-auto max-w-sm px-6 py-16">
      <h1 className="text-2xl font-semibold tracking-tight text-text">Create a workspace</h1>
      <p className="mt-1 text-sm text-text-muted">
        Workspaces keep conversations, members, and API keys separate.
      </p>

      <form onSubmit={handleSubmit} className="mt-8 flex flex-col gap-4">
        <label className="flex flex-col gap-1.5 text-sm">
          <span className="text-text-soft">Name</span>
          <input
            type="text"
            required
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Acme Inc"
            className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
          />
        </label>

        {error && (
          <p className="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={submitting}
          className="mt-2 rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-60"
        >
          {submitting ? "Creating…" : "Create workspace"}
        </button>
      </form>
    </div>
  );
}
