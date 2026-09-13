import { apiPublicUrl } from "./config";
import type { EnabledModel } from "./provider-client";

export type ConnectorKind = "website" | "file";
export type ConnectorStatus = "pending" | "indexing" | "ready" | "failed";
export type DocumentStatus = "pending" | "done" | "failed";

export type KnowledgeSettings = {
  embedding_model_id: string | null;
  embedding_model: EnabledModel | null;
};

export type Connector = {
  id: string;
  kind: ConnectorKind;
  name: string;
  url: string | null;
  max_pages: number;
  status: ConnectorStatus;
  error: string | null;
  embedding_model_id: string | null;
  embedding_dim: number | null;
  document_count: number;
  chunk_count: number;
  last_indexed_at: string | null;
  // True when the workspace's current embedding model differs from the one this connector was
  // actually indexed with (or it's never been indexed at all) — it's skipped at retrieval until
  // reindexed, so the UI should make that state visible rather than let it look silently stale.
  needs_reindex: boolean;
  created_by: string;
  created_at: string;
};

export type ConnectorDocument = {
  id: string;
  source_url: string | null;
  filename: string | null;
  mime: string;
  size: number;
  title: string | null;
  char_count: number;
  status: DocumentStatus;
  error: string | null;
  created_at: string;
};

export type ConnectorDetail = Connector & { documents: ConnectorDocument[] };

// One knowledge chunk folded into a reply — what the chat page's sources sidebar renders.
export type MessageSource = {
  id: string;
  message_id: string;
  connector_id: string | null;
  document_id: string | null;
  ordinal: number;
  label: string;
  url: string | null;
  snippet: string;
  score: number;
  created_at: string;
};

export class KnowledgeError extends Error {}

async function api(path: string, init: RequestInit = {}): Promise<Response> {
  return fetch(`${apiPublicUrl}${path}`, {
    ...init,
    credentials: "include",
    headers: init.body instanceof FormData ? init.headers : { "Content-Type": "application/json", ...init.headers },
  });
}

async function throwIfNotOk(res: Response): Promise<void> {
  if (res.ok) return;
  const body = (await res.json().catch(() => null)) as { detail?: string } | null;
  throw new KnowledgeError(body?.detail ?? "Something went wrong.");
}

// The workspace's chosen embedding model — every connector is indexed with it.
export async function getKnowledgeSettings(workspaceId: string): Promise<KnowledgeSettings> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/knowledge/settings`);
  await throwIfNotOk(res);
  return (await res.json()) as KnowledgeSettings;
}

// `null` clears the workspace's embedding model choice.
export async function setKnowledgeSettings(
  workspaceId: string,
  embeddingModelId: string | null
): Promise<KnowledgeSettings> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/knowledge/settings`, {
    method: "PUT",
    body: JSON.stringify({ embedding_model_id: embeddingModelId }),
  });
  await throwIfNotOk(res);
  return (await res.json()) as KnowledgeSettings;
}

export async function listConnectors(workspaceId: string): Promise<Connector[]> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/connectors`);
  await throwIfNotOk(res);
  return (await res.json()) as Connector[];
}

export async function getConnector(workspaceId: string, connectorId: string): Promise<ConnectorDetail> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/connectors/${connectorId}`);
  await throwIfNotOk(res);
  return (await res.json()) as ConnectorDetail;
}

// Registers a website connector — a same-host crawl from `url`, up to `maxPages` pages. Queued
// for indexing immediately; poll listConnectors/getConnector to watch its status.
export async function createWebsiteConnector(
  workspaceId: string,
  input: { name: string; url: string; max_pages?: number }
): Promise<Connector> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/connectors`, {
    method: "POST",
    body: JSON.stringify(input),
  });
  await throwIfNotOk(res);
  return (await res.json()) as Connector;
}

// Registers a file connector with one or more uploaded files, indexed immediately.
export async function createFileConnector(
  workspaceId: string,
  name: string,
  files: File[]
): Promise<Connector> {
  const form = new FormData();
  form.append("name", name);
  for (const file of files) form.append("files", file);
  const res = await api(`/api/v1/workspaces/${workspaceId}/connectors/files`, {
    method: "POST",
    body: form,
  });
  await throwIfNotOk(res);
  return (await res.json()) as Connector;
}

// Adds more files to an existing file connector — re-indexes the whole connector.
export async function addConnectorDocuments(
  workspaceId: string,
  connectorId: string,
  files: File[]
): Promise<ConnectorDetail> {
  const form = new FormData();
  for (const file of files) form.append("files", file);
  const res = await api(`/api/v1/workspaces/${workspaceId}/connectors/${connectorId}/documents`, {
    method: "POST",
    body: form,
  });
  await throwIfNotOk(res);
  return (await res.json()) as ConnectorDetail;
}

export async function deleteConnectorDocument(
  workspaceId: string,
  connectorId: string,
  documentId: string
): Promise<void> {
  const res = await api(
    `/api/v1/workspaces/${workspaceId}/connectors/${connectorId}/documents/${documentId}`,
    { method: "DELETE" }
  );
  await throwIfNotOk(res);
}

// Queues a fresh index of a connector — e.g. to pick up a website's changed content, or after
// the workspace's embedding model changed and this connector now "needs reindex".
export async function reindexConnector(workspaceId: string, connectorId: string): Promise<Connector> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/connectors/${connectorId}/reindex`, {
    method: "POST",
  });
  await throwIfNotOk(res);
  return (await res.json()) as Connector;
}

export async function deleteConnector(workspaceId: string, connectorId: string): Promise<void> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/connectors/${connectorId}`, {
    method: "DELETE",
  });
  await throwIfNotOk(res);
}

// Every knowledge source folded into any reply in a conversation — what lets a reloaded chat
// thread show a reply's sources, not just live while they stream in via the `sources` SSE event.
export async function listMessageSources(
  workspaceId: string,
  conversationId: string
): Promise<MessageSource[]> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/conversations/${conversationId}/sources`);
  await throwIfNotOk(res);
  return (await res.json()) as MessageSource[];
}
