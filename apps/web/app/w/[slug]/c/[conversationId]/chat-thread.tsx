"use client";

import { useRouter } from "next/navigation";
import { Fragment, useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { useRequireAuth } from "@/lib/auth-context";
import { listAssistants, type Assistant } from "@/lib/assistant-client";
import { AttachmentError, listAttachments, uploadAttachment, type Attachment } from "@/lib/attachment-client";
import {
  ChatError,
  deleteConversation,
  getActiveGeneration,
  getConversation,
  listMessages,
  resumeGeneration,
  sendMessage,
  stopGeneration,
  updateConversation,
  type Conversation,
  type LiveSource,
  type Message,
} from "@/lib/chat-client";
import { listMessageSources, type MessageSource } from "@/lib/knowledge-client";
import { setLastModelId } from "@/lib/last-model";
import { takePendingFirstMessage } from "@/lib/pending-first-message";
import { listModels, type EnabledModel } from "@/lib/provider-client";
import { listToolInvocations, type ToolInvocation } from "@/lib/tool-client";
import { useWorkspaceFlags } from "@/lib/use-workspace-flags";
import { useWorkspaceBySlug } from "@/lib/workspace-context";
import { ConversationSidebar } from "@/components/conversation-sidebar";
import { SourcesSidebar } from "@/components/sources-sidebar";
import { PendingAttachmentChips, SentAttachmentChips, hasBlockedImage } from "@/components/attachment-chips";
import { ImageThumbnails } from "@/components/image-thumbnails";
import { MarkdownMessage } from "@/components/markdown-message";

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

// Same grouping, for knowledge sources — a MessageSource always has a message_id (it's only ever
// recorded once the final assistant message exists), same shape ToolInvocation follows above.
function groupSourcesByMessageId(sources: MessageSource[]): Record<string, MessageSource[]> {
  const grouped: Record<string, MessageSource[]> = {};
  for (const source of sources) {
    (grouped[source.message_id] ??= []).push(source);
  }
  return grouped;
}

// Pairs each assistant reply with the user question right before it, newest first — what lets
// the sources sidebar label a group of sources by the question they answered, rather than two
// groups citing the same document reading as one confusing duplicate list.
function assistantTurnsNewestFirst(messages: Message[]): { messageId: string; label: string }[] {
  const turns: { messageId: string; label: string }[] = [];
  let lastUserContent = "";
  for (const m of messages) {
    if (m.role === "user") lastUserContent = m.content;
    else if (m.role === "assistant") turns.push({ messageId: m.id, label: lastUserContent });
  }
  return turns.reverse();
}

// One tool call's activity, live (ok still null, mid-run) or from history (ok already settled).
type ToolActivity = { name: string; ok: boolean | null; imageCount?: number };

function ToolActivityChip({ name, ok, imageCount = 0 }: ToolActivity) {
  return (
    <div
      className={`max-w-[75%] rounded-md border px-2 py-1 text-xs ${
        ok === false
          ? "border-danger/40 bg-danger/10 text-danger"
          : "border-border bg-surface-sunk text-text-muted"
      }`}
    >
      🔧 <span className="font-medium">{name}</span> — {ok === null ? "running…" : ok ? "done" : "failed"}
      {imageCount > 0 && ` · ${imageCount} image${imageCount === 1 ? "" : "s"}`}
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
  const knowledgeEnabled = flags.knowledge === true;

  const [conversation, setConversation] = useState<Conversation | null>(null);
  // Bumped whenever this conversation's ordering in the sidebar might have changed (a message
  // just landed, moving it to the top) — the sidebar owns its own fetch entirely, so this is the
  // one signal this page sends it to reset back to a fresh first page instead of going stale.
  const [sidebarRefreshKey, setSidebarRefreshKey] = useState(0);
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
  // Every knowledge source folded into a reply in this conversation, keyed by the assistant
  // message it belongs to — same shape toolInvocationsByMessageId follows, for the same reason.
  const [sourcesByMessageId, setSourcesByMessageId] = useState<Record<string, MessageSource[]>>({});
  // Live sources for the reply currently streaming in, from the `sources` SSE event — cleared
  // once that reply finishes and the persisted rows (fetched into sourcesByMessageId) take over.
  const [liveSources, setLiveSources] = useState<LiveSource[]>([]);
  // The workspace's enabled models — for the model switcher, and to look up whether the current
  // one accepts images (Conversation only carries a model_id, not the model's own fields).
  const [models, setModels] = useState<EnabledModel[]>([]);
  const [modelSupportsVision, setModelSupportsVision] = useState(false);
  const [switchingModel, setSwitchingModel] = useState(false);
  const [togglingShared, setTogglingShared] = useState(false);
  // The workspace's saved assistants, for the badge showing which one is answering and the
  // switcher beside the model picker.
  const [assistants, setAssistants] = useState<Assistant[]>([]);
  const [switchingAssistant, setSwitchingAssistant] = useState(false);

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
      setLiveSources([]);
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
              next[index] = { name: evt.data.name, ok: evt.data.ok, imageCount: evt.data.image_count };
              return next;
            });
          } else if (evt.event === "sources") {
            setLiveSources(evt.data.sources);
          }
        }
      } catch (err) {
        setError(err instanceof ChatError ? err.message : "Something went wrong.");
      } finally {
        const [msgs, attachments, invocations, sources] = await Promise.all([
          listMessages(workspace.id, conversationId),
          attachmentsEnabled ? listAttachments(workspace.id, conversationId) : Promise.resolve([]),
          toolsEnabled ? listToolInvocations(workspace.id, conversationId) : Promise.resolve([]),
          knowledgeEnabled ? listMessageSources(workspace.id, conversationId) : Promise.resolve([]),
        ]);
        setMessages(msgs);
        setAttachmentsByMessageId(groupByMessageId(attachments));
        setToolInvocationsByMessageId(groupToolInvocationsByMessageId(invocations));
        setSourcesByMessageId(groupSourcesByMessageId(sources));
        setStreamingText("");
        setLiveToolActivity([]);
        setLiveSources([]);
        setActiveGenerationId(null);
        setSending(false);
        setSidebarRefreshKey((prev) => prev + 1);
      }
    },
    [workspace, conversationId, attachmentsEnabled, toolsEnabled, knowledgeEnabled]
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
        const [conv, msgs, activeId, fetchedModels, fetchedAssistants, attachments, invocations, sources] =
          await Promise.all([
            getConversation(workspace.id, conversationId),
            listMessages(workspace.id, conversationId),
            getActiveGeneration(workspace.id, conversationId),
            listModels(workspace.id),
            listAssistants(workspace.id),
            attachmentsEnabled ? listAttachments(workspace.id, conversationId) : Promise.resolve([]),
            toolsEnabled ? listToolInvocations(workspace.id, conversationId) : Promise.resolve([]),
            knowledgeEnabled ? listMessageSources(workspace.id, conversationId) : Promise.resolve([]),
          ]);
        if (cancelled) return;
        setConversation(conv);
        setMessages(msgs);
        setAttachmentsByMessageId(groupByMessageId(attachments));
        setToolInvocationsByMessageId(groupToolInvocationsByMessageId(invocations));
        setSourcesByMessageId(groupSourcesByMessageId(sources));
        setModels(fetchedModels);
        setAssistants(fetchedAssistants);
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
                next[index] = { name: evt.data.name, ok: evt.data.ok, imageCount: evt.data.image_count };
                return next;
              });
            } else if (evt.event === "sources") {
              setLiveSources(evt.data.sources);
            }
          }
          if (cancelled) return;
          const [resumedMessages, resumedInvocations, resumedSources] = await Promise.all([
            listMessages(workspace.id, conversationId),
            toolsEnabled ? listToolInvocations(workspace.id, conversationId) : Promise.resolve([]),
            knowledgeEnabled ? listMessageSources(workspace.id, conversationId) : Promise.resolve([]),
          ]);
          setMessages(resumedMessages);
          setToolInvocationsByMessageId(groupToolInvocationsByMessageId(resumedInvocations));
          setSourcesByMessageId(groupSourcesByMessageId(resumedSources));
          setStreamingText("");
          setLiveToolActivity([]);
          setLiveSources([]);
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
  }, [workspace, conversationId, sendChat, attachmentsEnabled, toolsEnabled, knowledgeEnabled]);

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

  // Switches (or, choosing "No assistant", clears) which assistant governs this conversation —
  // resolved live on the very next send, same as the model switch above.
  async function handleAssistantChange(assistantId: string) {
    if (!workspace || assistantId === (conversation?.assistant_id ?? "")) return;
    setSwitchingAssistant(true);
    setError(null);
    try {
      const updated = await updateConversation(workspace.id, conversationId, {
        assistant_id: assistantId || null,
      });
      setConversation(updated);
    } catch (err) {
      setError(err instanceof ChatError ? err.message : "Something went wrong.");
    } finally {
      setSwitchingAssistant(false);
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
    <div className="flex w-full">
      <ConversationSidebar
        slug={slug}
        workspaceId={workspace.id}
        currentUserId={user?.id}
        activeConversationId={conversationId}
        refreshKey={sidebarRefreshKey}
        onDelete={handleDeleteConversation}
      />

      <div className="flex min-h-screen min-w-0 flex-1 flex-col">
        <div className="sticky top-0 z-40 flex h-16 items-center border-b border-border bg-surface px-6">
        <div className="mx-auto flex w-full max-w-3xl items-center justify-between gap-3">
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
            {assistants.length > 0 && (
              <select
                value={conversation.assistant_id ?? ""}
                onChange={(e) => handleAssistantChange(e.target.value)}
                disabled={switchingAssistant || sending}
                title="Assistant"
                className="rounded-md border border-border bg-surface px-2 py-1 text-xs text-text-soft outline-none focus:border-accent disabled:opacity-60"
              >
                <option value="">No assistant</option>
                {assistants.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name}
                  </option>
                ))}
              </select>
            )}
            {assistants.find((a) => a.id === conversation.assistant_id)?.memory_enabled && (
              <span
                title="This assistant recalls curated facts and what it's learned about you"
                className="rounded-full border border-accent/40 bg-accent/10 px-2 py-0.5 text-xs text-accent"
              >
                Memory on
              </span>
            )}
            {knowledgeEnabled &&
              (assistants.find((a) => a.id === conversation.assistant_id)?.connector_ids.length ?? 0) >
                0 && (
                <span
                  title="This assistant retrieves from its assigned connectors before replying"
                  className="rounded-full border border-accent/40 bg-accent/10 px-2 py-0.5 text-xs text-accent"
                >
                  Knowledge on
                </span>
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
        </div>

        <div className="mx-auto w-full max-w-3xl flex-1 px-6">
        {error && (
          <p className="mt-3 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
            {error}
          </p>
        )}

        <div className="space-y-4 py-4">
          {messages.map((m) => (
            <div
              key={m.id}
              className={`flex flex-col gap-1 ${m.role === "user" ? "items-end" : "items-start"}`}
            >
              {(toolInvocationsByMessageId[m.id] ?? []).map((invocation) => (
                <Fragment key={invocation.id}>
                  <ToolActivityChip name={invocation.name} ok={invocation.status === "success"} />
                  <ImageThumbnails
                    workspaceId={workspace.id}
                    images={invocation.images.map((image) => ({ id: image.id, alt: invocation.name }))}
                  />
                </Fragment>
              ))}
              <div
                className={`rounded-lg px-3 py-2 text-sm ${
                  m.role === "user"
                    ? "max-w-[75%] whitespace-pre-wrap bg-accent text-accent-contrast"
                    : "w-full border border-border bg-surface text-text"
                }`}
              >
                {m.role === "user" ? m.content : <MarkdownMessage content={m.content} />}
                {m.error && <p className="mt-1 text-xs text-danger">{m.error}</p>}
              </div>
              <SentAttachmentChips
                workspaceId={workspace.id}
                attachments={attachmentsByMessageId[m.id] ?? []}
              />
            </div>
          ))}
          {(liveToolActivity.length > 0 || streamingText) && (
            <div className="flex flex-col items-start gap-1">
              {liveToolActivity.map((activity, index) => (
                <ToolActivityChip key={index} name={activity.name} ok={activity.ok} />
              ))}
              {streamingText && (
                <div className="w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-text">
                  {/* The typing caret hangs off the last rendered block rather than sitting in
                      its own element: Markdown renders paragraphs and lists, so a sibling <span>
                      would land on a line of its own instead of where the text actually stops. */}
                  <MarkdownMessage
                    content={streamingText}
                    className="[&>:last-child]:after:ml-0.5 [&>:last-child]:after:inline-block [&>:last-child]:after:h-3 [&>:last-child]:after:w-1.5 [&>:last-child]:after:animate-pulse [&>:last-child]:after:bg-text-muted [&>:last-child]:after:align-middle [&>:last-child]:after:content-['']"
                  />
                </div>
              )}
            </div>
          )}
          <div ref={bottomRef} />
        </div>
        </div>

        <div className="sticky bottom-0 z-40 border-t border-border bg-surface px-6 pb-6 pt-3">
        <div className="mx-auto w-full max-w-3xl">
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

          <form onSubmit={handleSend} className="mt-2 flex items-end gap-2">
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
      </div>

      {knowledgeEnabled && (
        <SourcesSidebar
          turnsNewestFirst={assistantTurnsNewestFirst(messages)}
          sourcesByMessageId={sourcesByMessageId}
          liveSources={liveSources}
        />
      )}
    </div>
  );
}
