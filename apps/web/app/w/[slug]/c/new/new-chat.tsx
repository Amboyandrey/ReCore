"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { useRequireAuth } from "@/lib/auth-context";
import { listAssistants, type Assistant } from "@/lib/assistant-client";
import { AttachmentError, uploadAttachment, type Attachment } from "@/lib/attachment-client";
import {
  ChatError,
  createConversation,
  deleteConversation,
  listConversations,
  updateConversation,
  type Conversation,
} from "@/lib/chat-client";
import { getLastModelId, setLastModelId } from "@/lib/last-model";
import { setPendingFirstMessage } from "@/lib/pending-first-message";
import { listModels, type EnabledModel } from "@/lib/provider-client";
import { useWorkspaceFlags } from "@/lib/use-workspace-flags";
import { useWorkspaceBySlug } from "@/lib/workspace-context";
import { ConversationSidebar } from "@/components/conversation-sidebar";
import { PendingAttachmentChips, hasBlockedImage } from "@/components/attachment-chips";

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
// conversation row exists until either a file is attached or the first message is sent, unlike
// the old flow where clicking "+ New chat" created (and usually immediately abandoned) one right
// away. Attaching a file is the one thing that can't wait for Send: uploads need a real
// conversation_id to attach to, so it lazily creates the conversation right then instead — see
// ensureConversation() below.
export function NewChat({ slug }: { slug: string }) {
  const { user, loading: authLoading } = useRequireAuth();
  const { workspace, loading: wsLoading } = useWorkspaceBySlug(slug);
  const { flags } = useWorkspaceFlags(workspace?.id);
  const router = useRouter();
  const attachmentsEnabled = flags.attachments === true;

  const [siblings, setSiblings] = useState<Conversation[]>([]);
  const [models, setModels] = useState<EnabledModel[]>([]);
  const [modelId, setModelId] = useState("");
  const [assistants, setAssistants] = useState<Assistant[]>([]);
  const [assistantId, setAssistantId] = useState(""); // "" means no assistant
  const [loadingData, setLoadingData] = useState(true);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [pendingAttachments, setPendingAttachments] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The conversation this draft lazily becomes, the moment it needs to actually exist (a file
  // attached, or Send pressed) — refs, not state, because ensureConversation() reads and writes
  // them synchronously across calls that can overlap (e.g. clicking "+File" twice quickly)
  // without waiting for a re-render.
  const conversationIdRef = useRef<string | null>(null);
  const creatingRef = useRef<Promise<string> | null>(null);

  const modelSupportsVision = models.find((m) => m.id === modelId)?.supports_vision ?? false;

  useEffect(() => {
    if (!workspace) return;
    let cancelled = false;
    Promise.all([listConversations(workspace.id), listModels(workspace.id), listAssistants(workspace.id)])
      .then(([convs, fetchedModels, fetchedAssistants]) => {
        if (cancelled) return;
        setSiblings(convs);
        setModels(fetchedModels);
        setAssistants(fetchedAssistants);
        const initial = pickDefaultModel(workspace.id, fetchedModels);
        if (initial) setModelId(initial.id);
      })
      .finally(() => !cancelled && setLoadingData(false));
    return () => {
      cancelled = true;
    };
  }, [workspace]);

  // Creates the real conversation on first need, and only once — a second caller while creation
  // is still in flight gets the same promise rather than triggering a second POST.
  async function ensureConversation(): Promise<string> {
    if (conversationIdRef.current) return conversationIdRef.current;
    if (!workspace || !modelId) throw new Error("Pick a model first.");
    if (!creatingRef.current) {
      creatingRef.current = createConversation(workspace.id, modelId, assistantId || undefined)
        .then((conversation) => {
          conversationIdRef.current = conversation.id;
          setLastModelId(workspace.id, modelId);
          return conversation.id;
        })
        .catch((err) => {
          creatingRef.current = null; // let a retry actually retry, not replay the same rejection
          throw err;
        });
    }
    return creatingRef.current;
  }

  async function handleModelChange(newModelId: string) {
    setModelId(newModelId);
    // The conversation already exists (a file was attached before the model was changed) — keep
    // it in sync rather than letting the dropdown silently disagree with what's persisted.
    if (workspace && conversationIdRef.current) {
      try {
        await updateConversation(workspace.id, conversationIdRef.current, { model_id: newModelId });
      } catch (err) {
        setError(err instanceof ChatError ? err.message : "Something went wrong.");
      }
    }
  }

  // Picking an assistant pre-fills the model picker from its preferred model, same starting-guess
  // role pickDefaultModel already plays — still just a suggestion the model dropdown can override.
  async function handleAssistantChange(newAssistantId: string) {
    setAssistantId(newAssistantId);
    const assistant = assistants.find((a) => a.id === newAssistantId);
    if (assistant?.model_id && models.some((m) => m.id === assistant.model_id)) {
      setModelId(assistant.model_id);
    }
    if (workspace && conversationIdRef.current) {
      try {
        await updateConversation(workspace.id, conversationIdRef.current, {
          assistant_id: newAssistantId || null,
        });
      } catch (err) {
        setError(err instanceof ChatError ? err.message : "Something went wrong.");
      }
    }
  }

  async function handleSend(e: FormEvent) {
    e.preventDefault();
    if (
      !workspace ||
      !input.trim() ||
      !modelId ||
      sending ||
      hasBlockedImage(pendingAttachments, modelSupportsVision)
    ) {
      return;
    }
    setSending(true);
    setError(null);
    try {
      const conversationId = await ensureConversation();
      // Handed off, not sent from here: the real chat page does the actual sending once it
      // mounts at the new URL, so an in-flight reply is never at risk of being interrupted by
      // this navigation. See lib/pending-first-message.ts.
      setPendingFirstMessage(conversationId, {
        content: input,
        attachmentIds: pendingAttachments.map((a) => a.id),
      });
      router.replace(`/w/${slug}/c/${conversationId}`);
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

  async function handleFileSelect(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = ""; // let the same file be picked again later
    if (!workspace || !file || !modelId) return;
    setUploading(true);
    setError(null);
    try {
      const conversationId = await ensureConversation();
      const attachment = await uploadAttachment(workspace.id, conversationId, file);
      setPendingAttachments((prev) => [...prev, attachment]);
    } catch (err) {
      setError(
        err instanceof AttachmentError || err instanceof ChatError
          ? err.message
          : "Something went wrong."
      );
    } finally {
      setUploading(false);
    }
  }

  function handleRemoveAttachment(id: string) {
    setPendingAttachments((prev) => prev.filter((a) => a.id !== id));
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

  const blockedImage = hasBlockedImage(pendingAttachments, modelSupportsVision);

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

            {attachmentsEnabled && (
              <PendingAttachmentChips
                attachments={pendingAttachments}
                modelSupportsVision={modelSupportsVision}
                onRemove={handleRemoveAttachment}
              />
            )}

            {blockedImage && (
              <p className="mt-2 text-xs text-danger">
                This model can&apos;t read images. Remove the image or switch to a vision-capable model.
              </p>
            )}

            <form onSubmit={handleSend} className="mt-4 flex items-end gap-2">
              {assistants.length > 0 && (
                <select
                  value={assistantId}
                  onChange={(e) => handleAssistantChange(e.target.value)}
                  disabled={sending}
                  title="Assistant"
                  className="shrink-0 rounded-md border border-border bg-surface px-2 py-2 text-xs text-text-soft outline-none focus:border-accent disabled:opacity-60"
                >
                  <option value="">No assistant</option>
                  {assistants.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name}
                    </option>
                  ))}
                </select>
              )}
              <select
                value={modelId}
                onChange={(e) => handleModelChange(e.target.value)}
                disabled={sending}
                className="shrink-0 rounded-md border border-border bg-surface px-2 py-2 text-xs text-text-soft outline-none focus:border-accent disabled:opacity-60"
              >
                {models.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.display_name}
                  </option>
                ))}
              </select>
              {attachmentsEnabled && (
                <label className="cursor-pointer rounded-md border border-border px-3 py-2 text-sm text-text-soft hover:border-border-strong">
                  {uploading ? "…" : "+ File"}
                  <input
                    type="file"
                    onChange={handleFileSelect}
                    disabled={uploading}
                    className="hidden"
                  />
                </label>
              )}
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
                disabled={sending || !input.trim() || blockedImage}
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
