import { apiPublicUrl } from "./config";

export type ToolKind = "builtin" | "http";

export type Tool = {
  id: string;
  name: string;
  description: string;
  kind: ToolKind;
  enabled: boolean;
  created_at: string;
};

export type ToolInvocation = {
  id: string;
  message_id: string;
  tool_id: string | null;
  name: string;
  arguments: Record<string, unknown>;
  result: string | null;
  status: "success" | "error";
  error: string | null;
  created_at: string;
};

export class ToolError extends Error {}

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
  throw new ToolError(body?.detail ?? "Something went wrong.");
}

// Turns on the built-in web search tool for a workspace, or rotates its stored Tavily key.
export async function enableWebSearch(workspaceId: string, apiKey: string): Promise<Tool> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/tools/web-search`, {
    method: "POST",
    body: JSON.stringify({ api_key: apiKey }),
  });
  await throwIfNotOk(res);
  return (await res.json()) as Tool;
}

// Lists every tool registered in the workspace.
export async function listTools(workspaceId: string): Promise<Tool[]> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/tools`);
  await throwIfNotOk(res);
  return (await res.json()) as Tool[];
}

// Turns a tool off — its configuration (and any secret) is kept for a later re-enable.
export async function disableTool(workspaceId: string, toolId: string): Promise<void> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/tools/${toolId}`, { method: "DELETE" });
  await throwIfNotOk(res);
}

// Lists every tool call made in a conversation — what lets a reloaded chat thread show one was
// made on an earlier turn, not just live while it's streaming in.
export async function listToolInvocations(
  workspaceId: string,
  conversationId: string
): Promise<ToolInvocation[]> {
  const res = await api(
    `/api/v1/workspaces/${workspaceId}/conversations/${conversationId}/tool-invocations`
  );
  await throwIfNotOk(res);
  return (await res.json()) as ToolInvocation[];
}
