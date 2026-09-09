import { apiPublicUrl } from "./config";

export type Conversation = {
  id: string;
  user_id: string;
  title: string;
  model_id: string;
  // Set when an assistant governs this chat — its instructions and tools take over from this
  // conversation's own system_prompt and the workspace's full tool set (see the API's own docs).
  assistant_id: string | null;
  system_prompt: string | null;
  // Private to user_id until they opt it into being visible to the rest of the workspace.
  shared: boolean;
  created_at: string;
  updated_at: string;
};

export type Message = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  tokens_in: number | null;
  tokens_out: number | null;
  cost_usd: number | null;
  finish_reason: string | null;
  error: string | null;
  created_at: string;
};

export type SSEEvent =
  | { event: "delta"; data: { text: string } }
  | { event: "tool_call"; data: { name: string; arguments: Record<string, unknown> } }
  | { event: "tool_result"; data: { name: string; ok: boolean; content: string } }
  | { event: "done"; data: { finish_reason: string } }
  | { event: "error"; data: { message: string } };

export class ChatError extends Error {}

async function api(path: string, init: RequestInit = {}): Promise<Response> {
  return fetch(`${apiPublicUrl}${path}`, {
    ...init,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init.headers },
  });
}

async function throwIfNotOk(res: Response): Promise<void> {
  if (res.ok) return;
  const body = (await res.json().catch(() => null)) as { detail?: string } | null;
  throw new ChatError(body?.detail ?? "Something went wrong.");
}

// Parses a fetch Response's body as Server-Sent Events — used for both sending (POST, so the
// browser's native EventSource can't be used at all) and resuming (GET, kept on the same parser
// for one consistent code path rather than switching mechanisms per endpoint).
async function* consumeSSE(response: Response): AsyncGenerator<SSEEvent> {
  const reader = response.body?.getReader();
  if (!reader) return;
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let separator: number;
    while ((separator = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, separator);
      buffer = buffer.slice(separator + 2);
      let eventType = "";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) eventType = line.slice(6).trim();
        else if (line.startsWith("data:")) data = line.slice(5).trim();
      }
      if (!eventType || !data) continue;
      try {
        yield { event: eventType, data: JSON.parse(data) } as SSEEvent;
      } catch {
        continue; // a malformed block is dropped rather than crashing the whole stream
      }
    }
  }
}

// Lists a workspace's conversations, most recently active first.
export async function listConversations(workspaceId: string): Promise<Conversation[]> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/conversations`);
  await throwIfNotOk(res);
  return (await res.json()) as Conversation[];
}

// Starts a new conversation pinned to a model, optionally governed by one of the workspace's
// saved assistants.
export async function createConversation(
  workspaceId: string,
  modelId: string,
  assistantId?: string
): Promise<Conversation> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/conversations`, {
    method: "POST",
    body: JSON.stringify({ model_id: modelId, assistant_id: assistantId }),
  });
  await throwIfNotOk(res);
  return (await res.json()) as Conversation;
}

// Changes a conversation after it's started — only the fields passed are touched. `model_id`
// switches which enabled model it talks to mid-session (history already sent isn't resent to the
// new one); `shared` is its owner opting it into being visible to the rest of the workspace, or
// back out of it — only the owner may change that field. `assistant_id` switches (or, sent as
// `null`, clears) which assistant governs the chat.
export async function updateConversation(
  workspaceId: string,
  conversationId: string,
  changes: { model_id?: string; shared?: boolean; assistant_id?: string | null }
): Promise<Conversation> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/conversations/${conversationId}`, {
    method: "PATCH",
    body: JSON.stringify(changes),
  });
  await throwIfNotOk(res);
  return (await res.json()) as Conversation;
}

// Fetches one conversation.
export async function getConversation(workspaceId: string, conversationId: string): Promise<Conversation> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/conversations/${conversationId}`);
  await throwIfNotOk(res);
  return (await res.json()) as Conversation;
}

// Lists a conversation's messages, oldest first.
export async function listMessages(workspaceId: string, conversationId: string): Promise<Message[]> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/conversations/${conversationId}/messages`);
  await throwIfNotOk(res);
  return (await res.json()) as Message[];
}

// Reports the generation currently running for a conversation, if any — for a page that just
// loaded (no in-memory Last-Event-ID survives a refresh) to know whether there's a reply to resume.
export async function getActiveGeneration(
  workspaceId: string,
  conversationId: string
): Promise<string | null> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/conversations/${conversationId}/active-generation`);
  await throwIfNotOk(res);
  return ((await res.json()) as { generation_id: string | null }).generation_id;
}

// Sends a message and streams the reply. A repeated idempotencyKey on retry attaches to the
// original generation rather than sending (and billing for) a second one.
export async function* sendMessage(
  workspaceId: string,
  conversationId: string,
  content: string,
  idempotencyKey?: string,
  attachmentIds?: string[]
): AsyncGenerator<SSEEvent> {
  const headers: Record<string, string> = {};
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  const res = await api(`/api/v1/workspaces/${workspaceId}/conversations/${conversationId}/messages`, {
    method: "POST",
    headers,
    body: JSON.stringify({ content, attachment_ids: attachmentIds ?? [] }),
  });
  await throwIfNotOk(res);
  yield* consumeSSE(res);
}

// Resumes tailing a generation from the start — the frontend has no per-tab Last-Event-ID
// surviving a page reload, so it always replays the whole reply rather than guessing an offset.
export async function* resumeGeneration(
  workspaceId: string,
  conversationId: string,
  generationId: string
): AsyncGenerator<SSEEvent> {
  const res = await api(
    `/api/v1/workspaces/${workspaceId}/conversations/${conversationId}/generations/${generationId}`
  );
  await throwIfNotOk(res);
  yield* consumeSSE(res);
}

// Permanently deletes a conversation, its messages, attachments, and usage history.
export async function deleteConversation(workspaceId: string, conversationId: string): Promise<void> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/conversations/${conversationId}`, {
    method: "DELETE",
  });
  await throwIfNotOk(res);
}

// Signals a running generation to stop — takes effect between provider chunks, not instantly.
export async function stopGeneration(
  workspaceId: string,
  conversationId: string,
  generationId: string
): Promise<void> {
  const res = await api(
    `/api/v1/workspaces/${workspaceId}/conversations/${conversationId}/generations/${generationId}/stop`,
    { method: "POST" }
  );
  await throwIfNotOk(res);
}
