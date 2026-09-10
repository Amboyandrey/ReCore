"use client";

import Link from "next/link";
import { useEffect, useState, type FormEvent } from "react";
import { listAssistants, type Assistant } from "@/lib/assistant-client";
import { useRequireAuth } from "@/lib/auth-context";
import {
  addCuratedMemory,
  deleteMemory,
  listMemories,
  MemoryError,
  type Memory,
  type MemoryScope,
} from "@/lib/memory-client";
import { useWorkspaceFlags } from "@/lib/use-workspace-flags";
import { useWorkspaceBySlug } from "@/lib/workspace-context";

// One assistant's memories: its shared, creator-curated facts ("Assistant") and — visible only
// to the signed-in viewer, never anyone else's — what it's learned about them from their own
// chats ("Yours"). Curated add/delete is restricted server-side to the assistant's creator or the
// workspace owner; this page just mirrors that so the controls it shows make sense.
export function AssistantMemories({ slug, assistantId }: { slug: string; assistantId: string }) {
  const { user, loading: authLoading } = useRequireAuth();
  const { workspace, loading: wsLoading } = useWorkspaceBySlug(slug);
  const { flags, loading: flagsLoading } = useWorkspaceFlags(workspace?.id);
  const memoryFeatureEnabled = flags.memory === true;

  const [assistant, setAssistant] = useState<Assistant | null | undefined>(undefined); // undefined = not loaded yet
  const [tab, setTab] = useState<MemoryScope>("curated");
  const [memories, setMemories] = useState<Memory[]>([]);
  const [loadingMemories, setLoadingMemories] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [newFact, setNewFact] = useState("");
  const [adding, setAdding] = useState(false);
  const [justQueued, setJustQueued] = useState(false);

  const isOwner = workspace?.role === "owner";
  const canManageCurated = Boolean(assistant && (isOwner || assistant.created_by === user?.id));

  useEffect(() => {
    if (!workspace) return;
    let cancelled = false;
    listAssistants(workspace.id).then((all) => {
      if (!cancelled) setAssistant(all.find((a) => a.id === assistantId) ?? null);
    });
    return () => {
      cancelled = true;
    };
  }, [workspace, assistantId]);

  useEffect(() => {
    // Deferred into a .then()/.catch()/.finally() chain, never called directly at the effect's
    // top level — see workspace-context.tsx for why: calling setState synchronously there risks
    // cascading renders under Next's stricter react-hooks rules. A tab switch therefore swaps the
    // list in place once the new one loads, rather than flashing back to a loading state first.
    if (!workspace || !memoryFeatureEnabled) return;
    let cancelled = false;
    listMemories(workspace.id, assistantId, tab)
      .then((rows) => {
        if (!cancelled) setMemories(rows);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof MemoryError ? err.message : "Something went wrong.");
      })
      .finally(() => !cancelled && setLoadingMemories(false));
    return () => {
      cancelled = true;
    };
  }, [workspace, assistantId, tab, memoryFeatureEnabled]);

  async function handleAdd(e: FormEvent) {
    e.preventDefault();
    if (!workspace || !newFact.trim()) return;
    setAdding(true);
    setError(null);
    setJustQueued(false);
    try {
      await addCuratedMemory(workspace.id, assistantId, newFact);
      setNewFact("");
      setJustQueued(true);
    } catch (err) {
      setError(err instanceof MemoryError ? err.message : "Something went wrong.");
    } finally {
      setAdding(false);
    }
  }

  async function handleDelete(memoryId: string) {
    if (!workspace) return;
    setError(null);
    try {
      await deleteMemory(workspace.id, assistantId, memoryId, tab);
      setMemories((prev) => prev.filter((m) => m.id !== memoryId));
    } catch (err) {
      setError(err instanceof MemoryError ? err.message : "Something went wrong.");
    }
  }

  if (authLoading || wsLoading || flagsLoading || assistant === undefined) {
    return <div className="mx-auto max-w-3xl px-6 py-16 text-sm text-text-muted">Loading…</div>;
  }
  if (!workspace) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-16">
        <p className="text-sm text-text-soft">You don&apos;t have access to this workspace.</p>
      </div>
    );
  }
  if (!memoryFeatureEnabled) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-16">
        <h1 className="text-2xl font-semibold tracking-tight text-text">Memories</h1>
        <p className="mt-6 text-sm text-text-soft">
          This feature isn&apos;t enabled for your workspace yet.
        </p>
      </div>
    );
  }
  if (!assistant) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-16">
        <p className="text-sm text-text-soft">Assistant not found.</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-16">
      <Link
        href={`/w/${slug}/settings/assistants`}
        className="text-xs text-text-soft hover:underline"
      >
        ← Assistants
      </Link>
      <h1 className="mt-2 text-2xl font-semibold tracking-tight text-text">
        {assistant.name} — memories
      </h1>
      <p className="mt-1 text-sm text-text-muted">{workspace.name}</p>

      {error && (
        <p className="mt-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      <div className="mt-6 flex gap-2 border-b border-border">
        <button
          type="button"
          onClick={() => setTab("curated")}
          className={`px-3 py-2 text-sm ${
            tab === "curated"
              ? "border-b-2 border-accent text-text"
              : "text-text-soft hover:text-text"
          }`}
        >
          Assistant
        </button>
        <button
          type="button"
          onClick={() => setTab("personal")}
          className={`px-3 py-2 text-sm ${
            tab === "personal"
              ? "border-b-2 border-accent text-text"
              : "text-text-soft hover:text-text"
          }`}
        >
          Yours
        </button>
      </div>

      {tab === "curated" ? (
        <p className="mt-3 text-sm text-text-muted">
          Facts taught to this assistant directly — shared by everyone who chats with it.
        </p>
      ) : (
        <p className="mt-3 text-sm text-text-muted">
          What this assistant has learned about you specifically, from your own chats with it.
          Private to you — nobody else in this workspace can see or delete these, workspace owners
          included.
        </p>
      )}

      {loadingMemories ? (
        <p className="mt-4 text-sm text-text-muted">Loading…</p>
      ) : memories.length === 0 ? (
        <p className="mt-4 text-sm text-text-muted">No memories here yet.</p>
      ) : (
        <ul className="mt-4 flex flex-col gap-2">
          {memories.map((m) => (
            <li
              key={m.id}
              className="flex items-start justify-between gap-3 rounded-md border border-border bg-surface px-3 py-2 text-sm"
            >
              <span className="text-text">{m.memory}</span>
              {(tab === "personal" || canManageCurated) && (
                <button
                  type="button"
                  onClick={() => handleDelete(m.id)}
                  className="shrink-0 text-xs text-danger hover:underline"
                >
                  Delete
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {tab === "curated" && canManageCurated && (
        <form
          onSubmit={handleAdd}
          className="mt-6 flex flex-col gap-2 rounded-md border border-border bg-surface p-4"
        >
          <p className="text-xs font-semibold uppercase tracking-wide text-text-muted">
            Teach it a fact
          </p>
          <textarea
            required
            rows={2}
            value={newFact}
            onChange={(e) => setNewFact(e.target.value)}
            placeholder="Our support hours are 9am–6pm ET, Monday to Friday."
            className="rounded-md border border-border bg-surface px-3 py-2 text-sm text-text outline-none focus:border-accent"
          />
          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={adding || !newFact.trim()}
              className="self-start rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-60"
            >
              {adding ? "Saving…" : "Add"}
            </button>
            {justQueued && (
              <span className="text-xs text-text-muted">
                Queued — mem0 processes new memories in the background, so it may take a moment to
                show up above.
              </span>
            )}
          </div>
        </form>
      )}
    </div>
  );
}
