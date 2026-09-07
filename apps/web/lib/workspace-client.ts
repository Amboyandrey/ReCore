import { apiPublicUrl } from "./config";

export type Role = "viewer" | "member" | "admin" | "owner";

export type WorkspaceSummary = {
  id: string;
  slug: string;
  name: string;
  role: Role;
  created_at: string;
};

export class WorkspaceError extends Error {}

async function workspaceFetch(path: string, init: RequestInit = {}): Promise<Response> {
  return fetch(`${apiPublicUrl}${path}`, {
    ...init,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init.headers },
  });
}

async function throwIfNotOk(res: Response): Promise<void> {
  if (res.ok) return;
  const body = (await res.json().catch(() => null)) as { detail?: string } | null;
  throw new WorkspaceError(body?.detail ?? "Something went wrong.");
}

// Lists every workspace the signed-in user belongs to, with their role in each.
export async function listWorkspaces(): Promise<WorkspaceSummary[]> {
  const res = await workspaceFetch("/api/v1/workspaces");
  await throwIfNotOk(res);
  return (await res.json()) as WorkspaceSummary[];
}

// Creates a workspace and makes the caller its owner.
export async function createWorkspace(name: string): Promise<WorkspaceSummary> {
  const res = await workspaceFetch("/api/v1/workspaces", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
  await throwIfNotOk(res);
  return (await res.json()) as WorkspaceSummary;
}
