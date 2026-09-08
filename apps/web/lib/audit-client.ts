import { apiPublicUrl } from "./config";

export type AuditLogEntry = {
  id: string;
  actor_id: string;
  action: string;
  target_type: string;
  target_id: string;
  ip: string;
  event_metadata: Record<string, unknown> | null;
  created_at: string;
};

export class AuditError extends Error {}

async function throwIfNotOk(res: Response): Promise<void> {
  if (res.ok) return;
  const body = (await res.json().catch(() => null)) as { detail?: string } | null;
  throw new AuditError(body?.detail ?? "Something went wrong.");
}

// One page of a workspace's audit trail, newest first. Pass a previous page's last row's
// `created_at` as `before` to fetch the page after it — a plain indexed range scan, not an OFFSET.
export async function listAuditLogs(
  workspaceId: string,
  options: { limit?: number; before?: string } = {}
): Promise<AuditLogEntry[]> {
  const params = new URLSearchParams();
  if (options.limit) params.set("limit", String(options.limit));
  if (options.before) params.set("before", options.before);
  const res = await fetch(
    `${apiPublicUrl}/api/v1/workspaces/${workspaceId}/audit-logs?${params.toString()}`,
    { credentials: "include" }
  );
  await throwIfNotOk(res);
  return (await res.json()) as AuditLogEntry[];
}
