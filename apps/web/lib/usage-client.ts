import { apiPublicUrl } from "./config";

export type UsageByModel = {
  model_id: string;
  display_name: string;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  message_count: number;
};

export type UsageByMember = {
  user_id: string;
  email: string;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  message_count: number;
};

export type UsageByDay = {
  day: string;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  message_count: number;
};

export type UsageSummary = {
  by_model: UsageByModel[];
  by_member: UsageByMember[];
  by_day: UsageByDay[];
};

export class UsageError extends Error {}

async function throwIfNotOk(res: Response): Promise<void> {
  if (res.ok) return;
  const body = (await res.json().catch(() => null)) as { detail?: string } | null;
  throw new UsageError(body?.detail ?? "Something went wrong.");
}

// Tokens and spend by model, member, and day, over the trailing `days` (default 30).
export async function getUsage(workspaceId: string, days = 30): Promise<UsageSummary> {
  const res = await fetch(`${apiPublicUrl}/api/v1/workspaces/${workspaceId}/usage?days=${days}`, {
    credentials: "include",
  });
  await throwIfNotOk(res);
  return (await res.json()) as UsageSummary;
}
