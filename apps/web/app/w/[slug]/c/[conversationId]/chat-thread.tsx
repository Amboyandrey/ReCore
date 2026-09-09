"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { useRequireAuth } from "@/lib/auth-context";
import { AttachmentError, listAttachments, uploadAttachment, type Attachment } from "@/lib/attachment-client";
import {
  ChatError,
  deleteConversation,
  getActiveGeneration,
  getConversation,
  listConversations,
  listMessages,
  resumeGeneration,
  sendMessage,
  stopGeneration,
  updateConversation,
  type Conversation,
  type Message,
} from "@/lib/chat-client";
import { setLastModelId } from "@/lib/last-model";
import { takePendingFirstMessage } from "@/lib/pending-first-message";
import { listModels, type EnabledModel } from "@/lib/provider-client";
import { listToolInvocations, type ToolInvocation } from "@/lib/tool-client";
import { useWorkspaceFlags } from "@/lib/use-workspace-flags";
import { useWorkspaceBySlug } from "@/lib/workspace-context";
import { ConversationSidebar } from "@/components/conversation-sidebar";
import { PendingAttachmentChips, SentAttachmentChips, hasBlockedImage } from "@/components/attachment-chips";

// Groups a conversation's attachments by the message they were sent with — what lets a message
// already sitting in history show it had a file attached, not just the composer at send time.
function groupByMessageId(attachments: Attachment[]): Record<string, Attachment[]> {
  const grouped: Record<string, Attachment[]> = {};
  for (const attachment of attachments) {
    if (!attachment.message_id) continue;
    (grouped[attachment.message_id] ??= []).push(attachment);
  }
  return grouped;
}

// Same grouping, for tool calls — a ToolInvocation always has a message_id (it's only ever
// recorded once the final assistant message exists), unlike an attachment's, which starts null.
function groupToolInvocationsByMessageId(invocations: ToolInvocation[]): Record<string, ToolInvocation[]> {
  const grouped: Record<string, ToolInvocation[]> = {};
  for (const invocation of invocations) {
    (grouped[invocation.message_id] ??= []).push(invocation);
  }
  return grouped;
}

// One tool call's activity, live (ok still null, mid-run) or from history (ok already settled).
type ToolActivity = { name: string; ok: boolean | null };

function ToolActivityChip({ name, ok }: ToolActivity) {
  return (
    <div
      className={`max-w-[75%] rounded-md border px-2 py-1 text-xs ${
        ok === false
          ? "border-danger/40 bg-danger/10 text-danger"
          : "border-border bg-surface-sunk text-text-muted"
      }`}
    >
      🔧 <span className="font-medium">{name}</span> — {ok === null ? "running…" : ok ? "done" : "failed"}
    </div>
  );
}

// A locally-synthesized user turn shown the instant it's sent, before the server confirms it —
// swapped for the real persisted list once the reply finishes.
function pendingUserMessage(content: string): Message {
  return {
    id: `pending-${Date.now()}`,
    role: "user",
    content,
    tokens_in: null,
    tokens_out: null,
    cost_usd: null,
    finish_reason: null,
    error: null,
    created_at: new Date().toISOString(),
  };
}

export function ChatThread({ slug, conversationId }: { slug: string; conversationId: string }) {
  const { user, loading: authLoading } = useRequireAuth();
  const { workspace, loading: wsLoading } = useWorkspaceBySlug(slug);
  const { flags } = useWorkspaceFlags(workspace?.id);
  const router = useRouter();
  const attachmentsEnabled = flags.attachments === true;
  const toolsEnabled = flags.tools === true;

  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [siblings, setSiblings] = useState<Conversation[]>([]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [loadingData, setLoadingData] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [streamingText, setStreamingText] = useState("");
  const [activeGenerationId, setActiveGenerationId] = useState<string | null>(null);
  const [pendingAttachments, setPendingAttachments] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(false);
  // Every attachment in this conversation, keyed by the message it was sent with — so a message
  // already in history can show it had a file attached, not just the composer at send time.
  const [attachmentsByMessageId, setAttachmentsByMessageId] = useState<Record<string, Attachment[]>>(
    {}
  );
  // Every tool call made in this conversation, keyed by the (assistant) message it belongs to —
  // same shape as attachmentsByMessageId, for the same reason: a message already in history
  // should still show a tool was called, not just the composer while it's streaming live.
  const [toolInvocationsByMessageId, setToolInvocationsByMessageId] = useState<
    Record<string, ToolInvocation[]>
  >({});
  // Live tool activity for the reply currently streaming in — cleared once that reply finishes
  // and the real, persisted invocations (fetched into toolInvocationsByMessageId above) take over.
  const [liveToolActivity, setLiveToolActivity] = useState<ToolActivity[]>([]);
  // The workspace's enabled models — for the model switcher, and to look up whether the current
  // one accepts images (Conversation only carries a model_id, not the model's own fields).
  const [models, setModels] = useState<EnabledModel[]>([]);
  const [modelSupportsVision, setModelSupportsVision] = useState(false);
  const [switchingModel, setSwitchingModel] = useState(false);
  const [togglingShared, setTogglingShared] = useState(false);

  const bottomRef = useRef<HTMLDivElement>(null);

  // The actual send, independent of the composer form — also how a draft chat's first message
  // (queued by the c/new page before it navigated here, see lib/pending-first-message.ts) gets
  // sent the instant this page mounts, without the user having to retype or re-click anything.
  // Wrapped in useCallback (stable as long as workspace/conversationId don't change) so the load
  // effect below can call it without either re-running on every render or lying about its deps.
  const sendChat = useCallback(
    async (content: string, attachmentIds: string[]) => {
      if (!workspace) return;
      setSending(true);
      setStreamingText("");
      setLiveToolActivity([]);
      setError(null);
      setMessages((prev) => [...prev, pendingUserMessage(content)]);

      // Best-effort: grab the generation id shortly after starting, so Stop has something to call.
      getActiveGeneration(workspace.id, conversationId).then((id) => id && setActiveGenerationId(id));

      try {
        for await (const evt of sendMessage(
          workspace.id,
          conversationId,
          content,
          undefined,
          attachmentIds
        )) {
          if (evt.event === "delta") setStreamingText((prev) => prev + evt.data.text);
          else if (evt.event === "tool_call") {
            setLiveToolActivity((prev) => [...prev, { name: evt.data.name, ok: null }]);
          } else if (evt.event === "tool_result") {
            setLiveToolActivity((prev) => {
              const index = prev.findLastIndex((a) => a.name === evt.data.name && a.ok === null);
              if (index === -1) return prev;
              const next = [...prev];
              next[index] = { name: evt.data.name, ok: evt.data.ok };
              return next;
            });
          }
        }
      } catch (err) {
        setError(err instanceof ChatError ? err.message : "Something went wrong.");
      } finally {
        const [msgs, attachments, invocations] = await Promise.all([
          listMessages(workspace.id, conversationId),
          attachmentsEnabled ? listAttachments(workspace.id, conversationId) : Promise.resolve([]),
          toolsEnabled ? listToolInvocations(workspace.id, conversationId) : Promise.resolve([]),
        ]);
        setMessages(msgs);
        setAttachmentsByMessageId(groupByMessageId(attachments));
        setToolInvocationsByMessageId(groupToolInvocationsByMessageId(invocations));
        setStreamingText("");
        setLiveToolActivity([]);
        setActiveGenerationId(null);
        setSending(false);
      }
    },
    [workspace, conversationId, attachmentsEnabled, toolsEnabled]
  );

  // Initial load, then — the only way a page reload can discover a reply was mid-stream — check
  // for and resume an in-flight generation. Every setState here happens strictly after an
  // `await`, so nothing here runs synchronously within the effect itself.
  useEffect(() => {
    if (!workspace) return;
    let cancelled = false;

    async function load() {
      if (!workspace) return;
      try {
        const [conv, convs, msgs, activeId, fetchedModels, attachments, invocations] = await Promise.all([
          getConversation(workspace.id, conversationId),
          listConversations(workspace.id),
          listMessages(workspace.id, conversationId),
          getActiveGeneration(workspace.id, conversationId),
          listModels(workspace.id),
          attachmentsEnabled ? listAttachments(workspace.id, conversationId) : Promise.resolve([]),
          toolsEnabled ? listToolInvocations(workspace.id, conversationId) : Promise.resolve([]),
        ]);
        if (cancelled) return;
        setConversation(conv);
        setSiblings(convs);
        setMessages(msgs);
        setAttachmentsByMessageId(groupByMessageId(attachments));
        setToolInvocationsByMessageId(groupToolInvocationsByMessageId(invocations));
        setModels(fetchedModels);
        setModelSupportsVision(
          fetchedModels.find((m) => m.id === conv.model_id)?.supports_vision ?? false
        );
        setLoadingData(false);

        // A draft chat's first message, queued by c/new right before it navigated here (see
        // lib/pending-first-message.ts) — sent now, the instant this conversation actually
        // exists, rather than making the user retype or re-click Send.
        const pending = takePendingFirstMessage(conversationId);
        if (pending) {
          if (cancelled) return;
          await sendChat(pending.content, pending.attachmentIds);
          return;
        }

        if (activeId) {
          setActiveGenerationId(activeId);
          for await (const evt of resumeGeneration(workspace.id, conversationId, activeId)) {
            if (cancelled) return;
            if (evt.event === "delta") setStreamingText((prev) => prev + evt.data.text);
            else if (evt.event === "tool_call") {
              setLiveToolActivity((prev) => [...prev, { name: evt.data.name, ok: null }]);
            } else if (evt.event === "tool_result") {
              setLiveToolActivity((prev) => {
                const index = prev.findLastIndex((a) => a.name === evt.data.name && a.ok === null);
                if (index === -1) return prev;
                const next = [...prev];
                next[index] = { name: evt.data.name, ok: evt.data.ok };
                return next;
              });
            }
          }
          if (cancelled) return;
          const [resumedMessages, resumedInvocations] = await Promise.all([
            listMessages(workspace.id, conversationId),
            toolsEnabled ? listToolInvocations(workspace.id, conversationId) : Promise.resolve([]),
          ]);
          setMessages(resumedMessages);
          setToolInvocationsByMessageId(groupToolInvocationsByMessageId(resumedInvocations));
          setStreamingText("");
          setLiveToolActivity([]);
          setActiveGenerationId(null);
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof ChatError ? err.message : "Something went wrong.");
        setLoadingData(false);
      }
    }

    load();
    return () => {
      cancelled = true;
    };
  }, [workspace, conversationId, sendChat, attachmentsEnabled, toolsEnabled]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, streamingText]);

  async function handleSend(e: FormEvent) {
    e.preventDefault();
    if (!workspace || !input.trim() || sending || hasBlockedImage(pendingAttachments, modelSupportsVision))
      return;
    const content = input;
    const attachmentIds = pendingAttachments.map((a) => a.id);
    setInput("");
    setPendingAttachments([]);
    await sendChat(content, attachmentIds);
  }

  async function handleFileSelect(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = ""; // let the same file be picked again later
    if (!workspace || !file) return;
    setUploading(true);
    setError(null);
    try {
      const attachment = await uploadAttachment(workspace.id, conversationId, file);
      setPendingAttachments((prev) => [...prev, attachment]);
    } catch (err) {
      setError(err instanceof AttachmentError ? err.message : "Something went wrong.");
    } finally {
      setUploading(false);
    }
  }

  function handleRemoveAttachment(id: string) {
    setPendingAttachments((prev) => prev.filter((a) => a.id !== id));
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends (with or without Ctrl/Cmd, for anyone's old muscle memory); only Shift+Enter
    // falls through to the textarea's own default behavior and inserts a newline.
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      e.currentTarget.form?.requestSubmit();
    }
  }

  async function handleStop() {
    if (!workspace || !activeGenerationId) return;
    await stopGeneration(workspace.id, conversationId, activeGenerationId);
  }

  async function handleDeleteConversation(id: string) {
    if (!workspace) return;
    try {
      await deleteConversation(workspace.id, id);
      setSiblings((prev) => prev.filter((c) => c.id !== id));
      // The conversation on screen just deleted itself out from under this page — nothing left
      // to show here, so hop to a fresh chat instead of leaving a dead 404'd thread visible.
      if (id === conversationId) router.replace(`/w/${slug}/c/new`);
    } catch (err) {
      setError(err instanceof ChatError ? err.message : "Something went wrong.");
    }
  }

  // Switches which model this conversation talks to, mid-session — already-sent history isn't
  // resent to the new model, only the next turn goes to it.
  async function handleModelChange(modelId: string) {
    if (!workspace || modelId === conversation?.model_id) return;
    setSwitchingModel(true);
    setError(null);
    try {
      const updated = await updateConversation(workspace.id, conversationId, { model_id: modelId });
      setConversation(updated);
      setModelSupportsVision(models.find((m) => m.id === modelId)?.supports_vision ?? false);
      setLastModelId(workspace.id, modelId);
    } catch (err) {
      setError(err instanceof ChatError ? err.message : "Something went wrong.");
    } finally {
      setSwitchingModel(false);
    }
  }

  // Only the conversation's owner may share or unshare it — enforced server-side too; this just
  // keeps the UI from offering a control that would 403.
  async function handleToggleShared(shared: boolean) {
    if (!workspace) return;
    setTogglingShared(true);
    setError(null);
    try {
      setConversation(await updateConversation(workspace.id, conversationId, { shared }));
    } catch (err) {
      setError(err instanceof ChatError ? err.message : "Something went wrong.");
    } finally {
      setTogglingShared(false);
    }
  }

  if (authLoading || wsLoading || loadingData) {
    return <div className="mx-auto max-w-5xl px-6 py-16 text-sm text-text-muted">Loading…</div>;
  }
  if (!workspace || !conversation) {
    return (
      <div className="mx-auto max-w-5xl px-6 py-16">
        <p className="text-sm text-text-soft">Conversation not found.</p>
      </div>
    );
  }

  const blockedImage = hasBlockedImage(pendingAttachments, modelSupportsVision);

  const isOwner = conversation.user_id === user?.id;

  return (
    <div className="mx-auto flex max-w-5xl gap-6 px-6 py-8">
      <ConversationSidebar
        slug={slug}
        currentUserId={user?.id}
        activeConversationId={conversationId}
        conversations={siblings}
        onDelete={handleDeleteConversation}
      />

      <div className="flex min-h-[70vh] flex-1 flex-col">
        <div className="flex items-center justify-between gap-3">
          <h1 className="truncate text-lg font-semibold text-text">{conversation.title}</h1>
          <div className="flex shrink-0 items-center gap-2">
            {isOwner ? (
              <button
                type="button"
                onClick={() => handleToggleShared(!conversation.shared)}
                disabled={togglingShared}
                title={
                  conversation.shared
                    ? "Visible to the whole workspace — click to make private"
                    : "Private to you — click to share with the workspace"
                }
                className={`rounded-md border px-2 py-1 text-xs disabled:opacity-60 ${
                  conversation.shared
                    ? "border-accent/40 bg-accent/10 text-accent"
                    : "border-border text-text-soft hover:border-border-strong"
                }`}
              >
                {conversation.shared ? "Shared" : "Private"}
              </button>
            ) : (
              conversation.shared && (
                <span className="rounded-md border border-accent/40 bg-accent/10 px-2 py-1 text-xs text-accent">
                  Shared with you
                </span>
              )
            )}
            {models.length > 0 && (
              <select
                value={conversation.model_id}
                onChange={(e) => handleModelChange(e.target.value)}
                disabled={switchingModel || sending}
                className="rounded-md border border-border bg-surface px-2 py-1 text-xs text-text-soft outline-none focus:border-accent disabled:opacity-60"
              >
                {models.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.display_name}
                  </option>
                ))}
              </select>
            )}
          </div>
        </div>

        {error && (
          <p className="mt-3 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
            {error}
          </p>
        )}

        <div className="mt-4 flex-1 space-y-4 overflow-y-auto">
          {messages.map((m) => (
            <div
              key={m.id}
              className={`flex flex-col gap-1 ${m.role === "user" ? "items-end" : "items-start"}`}
            >
              {(toolInvocationsByMessageId[m.id] ?? []).map((invocation) => (
                <ToolActivityChip
                  key={invocation.id}
                  name={invocation.name}
                  ok={invocation.status === "success"}
                />
              ))}
              <div
                className={`max-w-[75%] whitespace-pre-wrap rounded-lg px-3 py-2 text-sm ${
                  m.role === "user"
                    ? "bg-accent text-accent-contrast"
                    : "border border-border bg-surface text-text"
                }`}
              >
                {m.content}
                {m.error && <p className="mt-1 text-xs text-danger">{m.error}</p>}
              </div>
              <SentAttachmentChips attachments={attachmentsByMessageId[m.id] ?? []} />
            </div>
          ))}
          {(liveToolActivity.length > 0 || streamingText) && (
            <div className="flex flex-col items-start gap-1">
              {liveToolActivity.map((activity, index) => (
                <ToolActivityChip key={index} name={activity.name} ok={activity.ok} />
              ))}
              {streamingText && (
                <div className="max-w-[75%] whitespace-pre-wrap rounded-lg border border-border bg-surface px-3 py-2 text-sm text-text">
                  {streamingText}
                  <span className="ml-0.5 inline-block h-3 w-1.5 animate-pulse bg-text-muted align-middle" />
                </div>
              )}
            </div>
          )}
          <div ref={bottomRef} />
        </div>

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
          {attachmentsEnabled && (
            <label className="cursor-pointer rounded-md border border-border px-3 py-2 text-sm text-text-soft hover:border-border-strong">
              {uploading ? "…" : "+ File"}
              <input type="file" onChange={handleFileSelect} disabled={uploading} className="hidden" />
            </label>
          )}
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            rows={2}
            placeholder="Message… (Enter to send, Shift+Enter for a new line)"
            className="flex-1 resize-none rounded-md border border-border bg-surface px-3 py-2 text-sm text-text outline-none focus:border-accent"
          />
          {sending && activeGenerationId ? (
            <button
              type="button"
              onClick={handleStop}
              className="rounded-md border border-danger px-3 py-2 text-sm text-danger"
            >
              Stop
            </button>
          ) : (
            <button
              type="submit"
              disabled={sending || !input.trim() || blockedImage}
              className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-60"
            >
              Send
            </button>
          )}
        </form>
      </div>
    </div>
  );
}
