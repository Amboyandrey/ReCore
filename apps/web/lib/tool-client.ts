import { apiPublicUrl } from "./config";

export type ToolKind = "builtin" | "http" | "mcp";
export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export type Tool = {
  id: string;
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  kind: ToolKind;
  enabled: boolean;
  method: HttpMethod | null;
  url: string | null;
  secret_header: string | null;
  has_secret: boolean;
  mcp_server_id: string | null;
  created_at: string;
};

export type HttpToolInput = {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  method: HttpMethod;
  url: string;
  secret_header?: string;
  secret_value?: string;
};

// Every field is optional — only the ones present change (see ToolUpdate's `exclude_unset` on
// the API side). `enabled` applies to any tool; the rest are HTTP-tool-only.
export type ToolUpdateInput = {
  enabled?: boolean;
  name?: string;
  description?: string;
  parameters?: Record<string, unknown>;
  method?: HttpMethod;
  url?: string;
  secret_header?: string;
  secret_value?: string;
};

export type McpServer = {
  id: string;
  name: string;
  url: string;
  auth_header: string | null;
  has_secret: boolean;
  last_synced_at: string | null;
  created_at: string;
};

export type McpServerInput = {
  name: string;
  url: string;
  auth_header?: string;
  auth_value?: string;
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
  images: { id: string; mime: string }[];
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

// Registers a third-party HTTP tool — the URL is checked against the SSRF guard immediately.
export async function createHttpTool(workspaceId: string, input: HttpToolInput): Promise<Tool> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/tools`, {
    method: "POST",
    body: JSON.stringify(input),
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

// Toggles a tool on/off, or edits an HTTP tool's configuration — only the fields in `changes`
// are touched, and a tool's own secret (if any) is kept unless `secret_value` is sent too.
export async function updateTool(
  workspaceId: string,
  toolId: string,
  changes: ToolUpdateInput
): Promise<Tool> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/tools/${toolId}`, {
    method: "PATCH",
    body: JSON.stringify(changes),
  });
  await throwIfNotOk(res);
  return (await res.json()) as Tool;
}

// Permanently removes a tool — unlike updateTool({ enabled: false }), there's no way back short
// of registering it again from scratch.
export async function deleteTool(workspaceId: string, toolId: string): Promise<void> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/tools/${toolId}`, { method: "DELETE" });
  await throwIfNotOk(res);
}

// Connects a remote MCP server and imports its tools, disabled until someone turns them on.
export async function createMcpServer(workspaceId: string, input: McpServerInput): Promise<McpServer> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/mcp-servers`, {
    method: "POST",
    body: JSON.stringify(input),
  });
  await throwIfNotOk(res);
  return (await res.json()) as McpServer;
}

// Lists every MCP server connected to the workspace.
export async function listMcpServers(workspaceId: string): Promise<McpServer[]> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/mcp-servers`);
  await throwIfNotOk(res);
  return (await res.json()) as McpServer[];
}

// Re-reads a server's tool list, picking up added, changed, and removed tools.
export async function syncMcpServer(workspaceId: string, serverId: string): Promise<McpServer> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/mcp-servers/${serverId}/sync`, {
    method: "POST",
  });
  await throwIfNotOk(res);
  return (await res.json()) as McpServer;
}

// Removes a server along with every tool it contributed.
export async function deleteMcpServer(workspaceId: string, serverId: string): Promise<void> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/mcp-servers/${serverId}`, {
    method: "DELETE",
  });
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
