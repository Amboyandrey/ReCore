import { apiPublicUrl } from "./config";

export type MemoryScope = "curated" | "personal";

export type Memory = {
  id: string;
  memory: string;
  created_at: string | null;
};

export class MemoryError extends Error {}

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
  throw new MemoryError(body?.detail ?? "Something went wrong.");
}

// Sets or rotates the workspace's mem0 API key. Admin-only, same floor a provider credential has.
export async function setMemoryCredential(workspaceId: string, apiKey: string): Promise<boolean> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/memory/credential`, {
    method: "PUT",
    body: JSON.stringify({ api_key: apiKey }),
  });
  await throwIfNotOk(res);
  return ((await res.json()) as { has_key: boolean }).has_key;
}

// Whether the workspace has a mem0 key configured — never the key itself.
export async function getMemoryCredential(workspaceId: string): Promise<boolean> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/memory/credential`);
  await throwIfNotOk(res);
  return ((await res.json()) as { has_key: boolean }).has_key;
}

// Removes the workspace's mem0 key — memories already in mem0 aren't deleted by this.
export async function deleteMemoryCredential(workspaceId: string): Promise<void> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/memory/credential`, { method: "DELETE" });
  await throwIfNotOk(res);
}

// Lists one assistant's curated memories (visible to any member) or the caller's own personal
// ones (`scope: "personal"` always means the signed-in user — never anyone else's).
export async function listMemories(
  workspaceId: string,
  assistantId: string,
  scope: MemoryScope
): Promise<Memory[]> {
  const res = await api(
    `/api/v1/workspaces/${workspaceId}/assistants/${assistantId}/memories?scope=${scope}`
  );
  await throwIfNotOk(res);
  return (await res.json()) as Memory[];
}

// Teaches an assistant a curated fact, stored verbatim — restricted server-side to its creator or
// the workspace owner. mem0 processes this asynchronously, so it may not appear in a list right away.
export async function addCuratedMemory(
  workspaceId: string,
  assistantId: string,
  text: string
): Promise<void> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/assistants/${assistantId}/memories`, {
    method: "POST",
    body: JSON.stringify({ text }),
  });
  await throwIfNotOk(res);
}

// Deletes one memory. A curated one requires being the assistant's creator or the workspace
// owner; a personal one is always the caller's own.
export async function deleteMemory(
  workspaceId: string,
  assistantId: string,
  memoryId: string,
  scope: MemoryScope
): Promise<void> {
  const res = await api(
    `/api/v1/workspaces/${workspaceId}/assistants/${assistantId}/memories/${memoryId}?scope=${scope}`,
    { method: "DELETE" }
  );
  await throwIfNotOk(res);
}
