import { apiPublicUrl } from "./config";

export type Assistant = {
  id: string;
  name: string;
  instructions: string;
  model_id: string | null;
  tool_ids: string[];
  memory_enabled: boolean;
  // Other assistants this one may hand a task to during chat, offered to its model as
  // `ask_<name>` tools (see the `delegation` flag) — resolved fresh on every send, one level
  // deep only (a delegate's own delegate_ids are never followed).
  delegate_ids: string[];
  // Connectors (see the `knowledge` flag) this assistant retrieves from before replying — a
  // connector must be READY to actually contribute at chat time, but stays assigned regardless.
  connector_ids: string[];
  created_by: string;
  created_at: string;
};

export type AssistantCreateInput = {
  name: string;
  instructions: string;
  model_id?: string;
  tool_ids?: string[];
  memory_enabled?: boolean;
  delegate_ids?: string[];
  connector_ids?: string[];
};

// Only the fields present change (see AssistantUpdate's `exclude_unset` on the API side) —
// sending `model_id: null` explicitly clears an assistant's preferred model, and `tool_ids: []`
// explicitly unassigns every tool, while omitting either key leaves it as it was.
export type AssistantUpdateInput = {
  name?: string;
  instructions?: string;
  model_id?: string | null;
  tool_ids?: string[];
  memory_enabled?: boolean;
  delegate_ids?: string[];
  connector_ids?: string[];
};

export class AssistantError extends Error {}

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
  throw new AssistantError(body?.detail ?? "Something went wrong.");
}

// Saves a new assistant: a name, required instructions, and an optional preferred model and tools.
export async function createAssistant(
  workspaceId: string,
  input: AssistantCreateInput
): Promise<Assistant> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/assistants`, {
    method: "POST",
    body: JSON.stringify(input),
  });
  await throwIfNotOk(res);
  return (await res.json()) as Assistant;
}

// Lists every assistant saved in the workspace.
export async function listAssistants(workspaceId: string): Promise<Assistant[]> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/assistants`);
  await throwIfNotOk(res);
  return (await res.json()) as Assistant[];
}

// Edits an assistant's fields or replaces its assigned tools — only the fields sent change.
export async function updateAssistant(
  workspaceId: string,
  assistantId: string,
  changes: AssistantUpdateInput
): Promise<Assistant> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/assistants/${assistantId}`, {
    method: "PATCH",
    body: JSON.stringify(changes),
  });
  await throwIfNotOk(res);
  return (await res.json()) as Assistant;
}

// Permanently removes an assistant — conversations that used it fall back to plain chat.
export async function deleteAssistant(workspaceId: string, assistantId: string): Promise<void> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/assistants/${assistantId}`, {
    method: "DELETE",
  });
  await throwIfNotOk(res);
}
