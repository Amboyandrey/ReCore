import { apiInternalUrl } from "./config";

export type ApiStatus =
  | { reachable: true; postgres: string; redis: string }
  | { reachable: false; error: string };

// Ask the API's readiness endpoint whether it and its dependencies are actually up.
export async function getApiStatus(): Promise<ApiStatus> {
  try {
    const res = await fetch(`${apiInternalUrl}/api/v1/health/ready`, { cache: "no-store" });
    if (!res.ok) return { reachable: false, error: `API responded with ${res.status}` };
    const body = (await res.json()) as { postgres: string; redis: string };
    return { reachable: true, postgres: body.postgres, redis: body.redis };
  } catch {
    return { reachable: false, error: "Could not reach the API" };
  }
}
