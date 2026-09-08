// Hands a draft chat's first message off to the real conversation page once it exists.
//
// A brand-new chat doesn't create a conversation row just by being opened — only sending an
// actual message does (see the draft composer in c/new). Creating the row and sending the
// message are two separate requests, though: the draft page mints the id, then navigates to
// `/c/{id}` and lets the real ChatThread instance there do the sending, so the streaming reply
// is never at risk of being interrupted by that route change. This in-memory, same-tab handoff
// is how the new page knows a message is already waiting for it. It's deliberately not persisted
// anywhere — a real page reload before the send fires just leaves an empty new conversation,
// same as if the send button were never clicked.
type PendingFirstMessage = { content: string; attachmentIds: string[] };

const pending = new Map<string, PendingFirstMessage>();

export function setPendingFirstMessage(conversationId: string, message: PendingFirstMessage): void {
  pending.set(conversationId, message);
}

export function takePendingFirstMessage(conversationId: string): PendingFirstMessage | null {
  const message = pending.get(conversationId) ?? null;
  pending.delete(conversationId);
  return message;
}
