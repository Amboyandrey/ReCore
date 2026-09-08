"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent, type KeyboardEvent } from "react";
import { useRequireAuth } from "@/lib/auth-context";
import {
  ChatError,
  createConversation,
  deleteConversation,
  listConversations,
  type Conversation,
} from "@/lib/chat-client";
import { getLastModelId, setLastModelId } from "@/lib/last-model";
import { setPendingFirstMessage } from "@/lib/pending-first-message";
import { listModels, type EnabledModel } from "@/lib/provider-client";
import { useWorkspaceBySlug } from "@/lib/workspace-context";
import { ConversationSidebar } from "@/components/conversation-sidebar";

// Picks which model a brand-new chat should start on: whichever one this workspace used last
// (remembered across sessions), falling back to the first enabled, undisabled model. Just a
// starting guess — the same model dropdown a real conversation has is right here too.
function pickDefaultModel(workspaceId: string, models: EnabledModel[]): EnabledModel | null {
  if (models.length === 0) return null;
  const lastId = getLastModelId(workspaceId);
  const last = lastId && models.find((m) => m.id === lastId && m.provider_enabled);
  return last || models.find((m) => m.provider_enabled) || models[0];
}

// A brand-new chat's composer. Nothing is saved to the database just by landing here — no
// conversation row exists until this first message is actually sent, unlike the old flow where
// clicking "+ New chat" created (and usually immediately abandoned) one right away.
export function NewChat({ slug }: { slug: string }) {
  const { user, loading: authLoading } = useRequireAuth();
  const { workspace, loading: wsLoading } = useWorkspaceBySlug(slug);
  const router = useRouter();

  const [siblings, setSiblings] = useState<Conversation[]>([]);
  const [models, setModels] = useState<EnabledModel[]>([]);
  const [modelId, setModelId] = useState("");
  const [loadingData, setLoadingData] = useState(true);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!workspace) return;
    let cancelled = false;
    Promise.all([listConversations(workspace.id), listModels(workspace.id)])
      .then(([convs, fetchedModels]) => {
        if (cancelled) return;
        setSiblings(convs);
        setModels(fetchedModels);
        const initial = pickDefaultModel(workspace.id, fetchedModels);
        if (initial) setModelId(initial.id);
      })
      .finally(() => !cancelled && setLoadingData(false));
    return () => {
      cancelled = true;
    };
  }, [workspace]);

  async function handleSend(e: FormEvent) {
    e.preventDefault();
    if (!workspace || !input.trim() || !modelId || sending) return;
    setSending(true);
    setError(null);
    try {
      const conversation = await createConversation(workspace.id, modelId);
      setLastModelId(workspace.id, modelId);
      // Handed off, not sent from here: the real chat page does the actual sending once it
      // mounts at the new URL, so an in-flight reply is never at risk of being interrupted by
      // this navigation. See lib/pending-first-message.ts.
      setPendingFirstMessage(conversation.id, { content: input, attachmentIds: [] });
      router.replace(`/w/${slug}/c/${conversation.id}`);
    } catch (err) {
      setError(err instanceof ChatError ? err.message : "Something went wrong.");
      setSending(false);
    }
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      e.currentTarget.form?.requestSubmit();
    }
  }

  async function handleDeleteConversation(id: string) {
    if (!workspace) return;
    try {
      await deleteConversation(workspace.id, id);
      setSiblings((prev) => prev.filter((c) => c.id !== id));
    } catch (err) {
      setError(err instanceof ChatError ? err.message : "Something went wrong.");
    }
  }

  if (authLoading || wsLoading || loadingData) {
    return <div className="mx-auto max-w-5xl px-6 py-16 text-sm text-text-muted">Loading…</div>;
  }
  if (!workspace) {
    return (
      <div className="mx-auto max-w-5xl px-6 py-16">
        <p className="text-sm text-text-soft">You don&apos;t have access to this workspace.</p>
      </div>
    );
  }

  return (
    <div className="mx-auto flex max-w-5xl gap-6 px-6 py-8">
      <ConversationSidebar
        slug={slug}
        currentUserId={user?.id}
        activeConversationId={null}
        conversations={siblings}
        onDelete={handleDeleteConversation}
      />

      <div className="flex min-h-[70vh] flex-1 flex-col">
        <h1 className="text-lg font-semibold text-text">New chat</h1>

        {error && (
          <p className="mt-3 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
            {error}
          </p>
        )}

        {models.length === 0 ? (
          <p className="mt-6 text-sm text-text-soft">
            No models are enabled yet.{" "}
            <Link href={`/w/${slug}/settings/providers`} className="text-accent">
              Enable one
            </Link>{" "}
            first.
          </p>
        ) : (
          <>
            <div className="flex-1" />
            <form onSubmit={handleSend} className="mt-4 flex items-end gap-2">
              <select
                value={modelId}
                onChange={(e) => setModelId(e.target.value)}
                disabled={sending}
                className="shrink-0 rounded-md border border-border bg-surface px-2 py-2 text-xs text-text-soft outline-none focus:border-accent disabled:opacity-60"
              >
                {models.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.display_name}
                  </option>
                ))}
              </select>
              <textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                rows={2}
                autoFocus
                placeholder="Message… (Enter to send, Shift+Enter for a new line)"
                className="flex-1 resize-none rounded-md border border-border bg-surface px-3 py-2 text-sm text-text outline-none focus:border-accent"
              />
              <button
                type="submit"
                disabled={sending || !input.trim()}
                className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-60"
              >
                {sending ? "Starting…" : "Send"}
              </button>
            </form>
          </>
        )}
      </div>
    </div>
  );
}
