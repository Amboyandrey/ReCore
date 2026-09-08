import { apiPublicUrl } from "./config";

export type Attachment = {
  id: string;
  conversation_id: string;
  message_id: string | null;
  original_filename: string;
  mime: string;
  size: number;
  extract_status: "pending" | "done" | "failed" | "unsupported" | "passthrough";
  extracted_text: string | null;
  created_at: string;
};

export class AttachmentError extends Error {}

async function throwIfNotOk(res: Response): Promise<void> {
  if (res.ok) return;
  const body = (await res.json().catch(() => null)) as { detail?: string } | null;
  throw new AttachmentError(body?.detail ?? "Something went wrong.");
}

// Uploads a file into a conversation — 404s entirely while the `attachments` flag is off.
export async function uploadAttachment(
  workspaceId: string,
  conversationId: string,
  file: File
): Promise<Attachment> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(
    `${apiPublicUrl}/api/v1/workspaces/${workspaceId}/conversations/${conversationId}/attachments`,
    { method: "POST", credentials: "include", body: form }
  );
  await throwIfNotOk(res);
  return (await res.json()) as Attachment;
}

// Lists every attachment uploaded into a conversation.
export async function listAttachments(workspaceId: string, conversationId: string): Promise<Attachment[]> {
  const res = await fetch(
    `${apiPublicUrl}/api/v1/workspaces/${workspaceId}/conversations/${conversationId}/attachments`,
    { credentials: "include" }
  );
  await throwIfNotOk(res);
  return (await res.json()) as Attachment[];
}
