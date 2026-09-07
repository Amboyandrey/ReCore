import { apiPublicUrl } from "./config";

export type ProviderId = "anthropic" | "openai" | "google" | "openai_compatible";

export type Credential = {
  id: string;
  provider: ProviderId;
  label: string;
  base_url: string | null;
  last4: string;
  disabled_at: string | null;
  created_at: string;
};

export type AvailableModel = { id: string; display_name: string; context_window: number | null };

export type EnabledModel = {
  id: string;
  credential_id: string;
  provider_model_id: string;
  display_name: string;
  context_window: number | null;
  cost_per_mtok_in: number | null;
  cost_per_mtok_out: number | null;
};

export class ProviderError extends Error {}

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
  throw new ProviderError(body?.detail ?? "Something went wrong.");
}

// Lists every credential registered for a workspace — never the keys themselves.
export async function listCredentials(workspaceId: string): Promise<Credential[]> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/credentials`);
  await throwIfNotOk(res);
  return (await res.json()) as Credential[];
}

// Validates a key against its provider and stores it, encrypted, if the provider accepts it.
export async function createCredential(
  workspaceId: string,
  body: { provider: ProviderId; label: string; api_key: string; base_url?: string }
): Promise<Credential> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/credentials`, {
    method: "POST",
    body: JSON.stringify(body),
  });
  await throwIfNotOk(res);
  return (await res.json()) as Credential;
}

// Removes a credential and every model enabled through it.
export async function deleteCredential(workspaceId: string, credentialId: string): Promise<void> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/credentials/${credentialId}`, {
    method: "DELETE",
  });
  await throwIfNotOk(res);
}

// Asks a credential's provider what models it can see, for the enable-a-model picker.
export async function listAvailableModels(
  workspaceId: string,
  credentialId: string
): Promise<AvailableModel[]> {
  const res = await api(
    `/api/v1/workspaces/${workspaceId}/credentials/${credentialId}/available-models`
  );
  await throwIfNotOk(res);
  return (await res.json()) as AvailableModel[];
}

// Enables (or, resubmitted for the same credential + model, updates) a model for chat.
export async function enableModel(
  workspaceId: string,
  body: {
    credential_id: string;
    provider_model_id: string;
    display_name: string;
    context_window?: number | null;
    cost_per_mtok_in?: number | null;
    cost_per_mtok_out?: number | null;
  }
): Promise<EnabledModel> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/models`, {
    method: "POST",
    body: JSON.stringify(body),
  });
  await throwIfNotOk(res);
  return (await res.json()) as EnabledModel;
}

// Lists every model enabled for chat in the workspace, with its pricing.
export async function listModels(workspaceId: string): Promise<EnabledModel[]> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/models`);
  await throwIfNotOk(res);
  return (await res.json()) as EnabledModel[];
}

// Removes a model from the enabled catalog (its pricing is kept if it's ever re-enabled).
export async function disableModel(workspaceId: string, modelId: string): Promise<void> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/models/${modelId}`, { method: "DELETE" });
  await throwIfNotOk(res);
}
