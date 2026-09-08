import { apiPublicUrl } from "./config";

export type FlagScope = "user" | "workspace";

export type Flag = {
  id: string;
  key: string;
  description: string;
  type: "boolean";
  default_value: boolean;
  rollout_percentage: number | null;
  archived_at: string | null;
  created_at: string;
};

export type FlagOverride = {
  id: string;
  flag_id: string;
  scope: FlagScope;
  scope_id: string;
  value: boolean;
  created_at: string;
};

// What `/flags/evaluate` returns: every flag's resolved value for the caller in one workspace.
export type ResolvedFlags = Record<string, boolean>;

export class FlagError extends Error {}

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
  throw new FlagError(body?.detail ?? "Something went wrong.");
}

// Resolves every flag for the caller in one workspace — a synchronous-feeling read once cached.
export async function evaluateFlags(workspaceId: string): Promise<ResolvedFlags> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/flags/evaluate`);
  await throwIfNotOk(res);
  return (await res.json()) as ResolvedFlags;
}

// Lists every non-archived flag definition — superuser only.
export async function listFlags(): Promise<Flag[]> {
  const res = await api("/api/v1/admin/flags");
  await throwIfNotOk(res);
  return (await res.json()) as Flag[];
}

// Defines a new flag.
export async function createFlag(body: {
  key: string;
  description: string;
  default_value: boolean;
  rollout_percentage?: number | null;
}): Promise<Flag> {
  const res = await api("/api/v1/admin/flags", { method: "POST", body: JSON.stringify(body) });
  await throwIfNotOk(res);
  return (await res.json()) as Flag;
}

// Edits a flag — only the fields present in `changes` are touched (see services/flags.py).
export async function updateFlag(
  flagId: string,
  changes: Partial<{
    description: string;
    default_value: boolean;
    rollout_percentage: number | null;
    archived: boolean;
  }>
): Promise<Flag> {
  const res = await api(`/api/v1/admin/flags/${flagId}`, {
    method: "PATCH",
    body: JSON.stringify(changes),
  });
  await throwIfNotOk(res);
  return (await res.json()) as Flag;
}

// Lists a flag's overrides.
export async function listOverrides(flagId: string): Promise<FlagOverride[]> {
  const res = await api(`/api/v1/admin/flags/${flagId}/overrides`);
  await throwIfNotOk(res);
  return (await res.json()) as FlagOverride[];
}

// Pins a flag to a value for one user or workspace — the killswitch and per-tenant demo lever.
export async function setOverride(
  flagId: string,
  body: { scope: FlagScope; scope_id: string; value: boolean }
): Promise<FlagOverride> {
  const res = await api(`/api/v1/admin/flags/${flagId}/overrides`, {
    method: "POST",
    body: JSON.stringify(body),
  });
  await throwIfNotOk(res);
  return (await res.json()) as FlagOverride;
}

// Removes an override, falling its target back to the rollout/default resolution.
export async function deleteOverride(flagId: string, overrideId: string): Promise<void> {
  const res = await api(`/api/v1/admin/flags/${flagId}/overrides/${overrideId}`, { method: "DELETE" });
  await throwIfNotOk(res);
}
