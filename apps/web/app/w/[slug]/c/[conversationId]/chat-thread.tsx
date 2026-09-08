"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { useRequireAuth } from "@/lib/auth-context";
import { AttachmentError, uploadAttachment, type Attachment } from "@/lib/attachment-client";
import {
  ChatError,
  getActiveGeneration,
  getConversation,
  listConversations,
  listMessages,
  resumeGeneration,
  sendMessage,
  stopGeneration,
  type Conversation,
  type Message,
} from "@/lib/chat-client";
import { useWorkspaceFlags } from "@/lib/use-workspace-flags";
import { useWorkspaceBySlug } from "@/lib/workspace-context";

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
  const { loading: authLoading } = useRequireAuth();
  const { workspace, loading: wsLoading } = useWorkspaceBySlug(slug);
  const { flags } = useWorkspaceFlags(workspace?.id);
  const attachmentsEnabled = flags.attachments === true;

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

  const bottomRef = useRef<HTMLDivElement>(null);

  // Initial load, then — the only way a page reload can discover a reply was mid-stream — check
  // for and resume an in-flight generation. Every setState here happens strictly after an
  // `await`, so nothing here runs synchronously within the effect itself.
  useEffect(() => {
    if (!workspace) return;
    let cancelled = false;

    async function load() {
      if (!workspace) return;
      try {
        const [conv, convs, msgs, activeId] = await Promise.all([
          getConversation(workspace.id, conversationId),
          listConversations(workspace.id),
          listMessages(workspace.id, conversationId),
          getActiveGeneration(workspace.id, conversationId),
        ]);
        if (cancelled) return;
        setConversation(conv);
        setSiblings(convs);
        setMessages(msgs);
        setLoadingData(false);

        if (activeId) {
          setActiveGenerationId(activeId);
          for await (const evt of resumeGeneration(workspace.id, conversationId, activeId)) {
            if (cancelled) return;
            if (evt.event === "delta") setStreamingText((prev) => prev + evt.data.text);
          }
          if (cancelled) return;
          setMessages(await listMessages(workspace.id, conversationId));
          setStreamingText("");
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
  }, [workspace, conversationId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, streamingText]);

  async function handleSend(e: FormEvent) {
    e.preventDefault();
    if (!workspace || !input.trim() || sending) return;
    const content = input;
    const attachmentIds = pendingAttachments.map((a) => a.id);
    setInput("");
    setPendingAttachments([]);
    setSending(true);
    setStreamingText("");
    setError(null);
    setMessages((prev) => [...prev, pendingUserMessage(content)]);

    // Best-effort: grab the generation id shortly after starting, so Stop has something to call.
    getActiveGeneration(workspace.id, conversationId).then((id) => id && setActiveGenerationId(id));

    try {
      for await (const evt of sendMessage(workspace.id, conversationId, content, undefined, attachmentIds)) {
        if (evt.event === "delta") setStreamingText((prev) => prev + evt.data.text);
      }
    } catch (err) {
      setError(err instanceof ChatError ? err.message : "Something went wrong.");
    } finally {
      setMessages(await listMessages(workspace.id, conversationId));
      setStreamingText("");
      setActiveGenerationId(null);
      setSending(false);
    }
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
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      e.currentTarget.form?.requestSubmit();
    }
  }

  async function handleStop() {
    if (!workspace || !activeGenerationId) return;
    await stopGeneration(workspace.id, conversationId, activeGenerationId);
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

  return (
    <div className="mx-auto flex max-w-5xl gap-6 px-6 py-8">
      <aside className="hidden w-56 shrink-0 sm:block">
        <Link
          href={`/w/${slug}/c/new`}
          className="block rounded-md border border-border px-3 py-2 text-center text-sm text-accent"
        >
          + New chat
        </Link>
        <ul className="mt-4 flex flex-col gap-1">
          {siblings.map((c) => (
            <li key={c.id}>
              <Link
                href={`/w/${slug}/c/${c.id}`}
                className={`block truncate rounded-md px-2 py-1.5 text-sm ${
                  c.id === conversationId
                    ? "bg-surface-sunk text-text"
                    : "text-text-soft hover:bg-surface-sunk"
                }`}
              >
                {c.title}
              </Link>
            </li>
          ))}
        </ul>
      </aside>

      <div className="flex min-h-[70vh] flex-1 flex-col">
        <h1 className="truncate text-lg font-semibold text-text">{conversation.title}</h1>

        {error && (
          <p className="mt-3 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
            {error}
          </p>
        )}

        <div className="mt-4 flex-1 space-y-4 overflow-y-auto">
          {messages.map((m) => (
            <div key={m.id} className={m.role === "user" ? "flex justify-end" : "flex justify-start"}>
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
            </div>
          ))}
          {streamingText && (
            <div className="flex justify-start">
              <div className="max-w-[75%] whitespace-pre-wrap rounded-lg border border-border bg-surface px-3 py-2 text-sm text-text">
                {streamingText}
                <span className="ml-0.5 inline-block h-3 w-1.5 animate-pulse bg-text-muted align-middle" />
              </div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        {attachmentsEnabled && pendingAttachments.length > 0 && (
          <ul className="mt-4 flex flex-wrap gap-2">
            {pendingAttachments.map((a) => (
              <li
                key={a.id}
                className="flex items-center gap-2 rounded-full border border-border bg-surface px-3 py-1 text-xs text-text-soft"
              >
                <span className="max-w-[12rem] truncate">{a.original_filename}</span>
                <button
                  type="button"
                  onClick={() => handleRemoveAttachment(a.id)}
                  className="text-text-muted hover:text-danger"
                  aria-label={`Remove ${a.original_filename}`}
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
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
            placeholder="Message… (⌘+Enter to send)"
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
              disabled={sending || !input.trim()}
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
