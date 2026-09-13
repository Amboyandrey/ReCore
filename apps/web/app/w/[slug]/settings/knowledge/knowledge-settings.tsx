"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { useRequireAuth } from "@/lib/auth-context";
import {
  addConnectorDocuments,
  createFileConnector,
  createWebsiteConnector,
  deleteConnector,
  deleteConnectorDocument,
  getConnector,
  getKnowledgeSettings,
  KnowledgeError,
  listConnectors,
  reindexConnector,
  setKnowledgeSettings,
  type Connector,
  type ConnectorDocument,
  type KnowledgeSettings,
} from "@/lib/knowledge-client";
import { listModels, type EnabledModel } from "@/lib/provider-client";
import { useWorkspaceFlags } from "@/lib/use-workspace-flags";
import { useWorkspaceBySlug } from "@/lib/workspace-context";

const STATUS_LABEL: Record<Connector["status"], string> = {
  pending: "Pending",
  indexing: "Indexing…",
  ready: "Ready",
  failed: "Failed",
};

const STATUS_CLASS: Record<Connector["status"], string> = {
  pending: "border-border text-text-muted",
  indexing: "border-accent/40 bg-accent/10 text-accent",
  ready: "border-success/40 bg-success/10 text-success",
  failed: "border-danger/40 bg-danger/10 text-danger",
};

// The Knowledge (ReStore) settings page: an admin picks one embedding model for the whole
// workspace, and any member registers connectors (websites to crawl, or uploaded files) that get
// indexed in the background and can be attached to assistants. Open to any member for connector
// management — the embedding model choice itself is admin-only, mirroring the mem0 key.
export function KnowledgeSettingsPage({ slug }: { slug: string }) {
  const { loading: authLoading } = useRequireAuth();
  const { workspace, loading: wsLoading } = useWorkspaceBySlug(slug);
  const { flags, loading: flagsLoading } = useWorkspaceFlags(workspace?.id);
  const knowledgeEnabled = flags.knowledge === true;
  const isAdmin = workspace?.role === "admin" || workspace?.role === "owner";

  const [settings, setSettings] = useState<KnowledgeSettings | null>(null);
  const [embeddingModels, setEmbeddingModels] = useState<EnabledModel[]>([]);
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [loadingData, setLoadingData] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [selectedModelId, setSelectedModelId] = useState("");
  const [savingSettings, setSavingSettings] = useState(false);

  const [websiteName, setWebsiteName] = useState("");
  const [websiteUrl, setWebsiteUrl] = useState("");
  const [maxPages, setMaxPages] = useState(30);
  const [creatingWebsite, setCreatingWebsite] = useState(false);

  const [fileConnectorName, setFileConnectorName] = useState("");
  const [creatingFileConnector, setCreatingFileConnector] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [documentsByConnector, setDocumentsByConnector] = useState<Record<string, ConnectorDocument[]>>({});
  const [addingDocsFor, setAddingDocsFor] = useState<string | null>(null);

  useEffect(() => {
    // Deferred into a .then()/.catch()/.finally() chain, never called directly at the effect's
    // top level (even in an early-return branch) — see workspace-context.tsx for why: calling
    // setState synchronously there risks cascading renders under Next's stricter react-hooks rules.
    if (!workspace || !knowledgeEnabled) {
      Promise.resolve().then(() => setLoadingData(false));
      return;
    }
    let cancelled = false;
    Promise.all([
      getKnowledgeSettings(workspace.id),
      isAdmin ? listModels(workspace.id, "embedding") : Promise.resolve<EnabledModel[]>([]),
      listConnectors(workspace.id),
    ])
      .then(([s, models, c]) => {
        if (cancelled) return;
        setSettings(s);
        setSelectedModelId(s.embedding_model_id ?? "");
        setEmbeddingModels(models);
        setConnectors(c);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof KnowledgeError ? err.message : "Something went wrong.");
      })
      .finally(() => !cancelled && setLoadingData(false));
    return () => {
      cancelled = true;
    };
  }, [workspace, knowledgeEnabled, isAdmin]);

  // Poll while anything is still indexing — the alternative (a live SSE feed) is more than a
  // status pill needs; a short poll keeps this page simple and still feels responsive.
  useEffect(() => {
    if (!workspace || !knowledgeEnabled) return;
    const hasPending = connectors.some((c) => c.status === "pending" || c.status === "indexing");
    if (!hasPending) return;
    const timer = setInterval(() => {
      listConnectors(workspace.id).then(setConnectors).catch(() => undefined);
    }, 5000);
    return () => clearInterval(timer);
  }, [workspace, knowledgeEnabled, connectors]);

  async function handleSaveSettings(e: FormEvent) {
    e.preventDefault();
    if (!workspace) return;
    setSavingSettings(true);
    setError(null);
    try {
      setSettings(await setKnowledgeSettings(workspace.id, selectedModelId || null));
    } catch (err) {
      setError(err instanceof KnowledgeError ? err.message : "Something went wrong.");
    } finally {
      setSavingSettings(false);
    }
  }

  async function handleCreateWebsite(e: FormEvent) {
    e.preventDefault();
    if (!workspace || !websiteName.trim() || !websiteUrl.trim()) return;
    setCreatingWebsite(true);
    setError(null);
    try {
      const connector = await createWebsiteConnector(workspace.id, {
        name: websiteName,
        url: websiteUrl,
        max_pages: maxPages,
      });
      setConnectors((prev) => [...prev, connector]);
      setWebsiteName("");
      setWebsiteUrl("");
      setMaxPages(30);
    } catch (err) {
      setError(err instanceof KnowledgeError ? err.message : "Something went wrong.");
    } finally {
      setCreatingWebsite(false);
    }
  }

  async function handleCreateFileConnector(e: FormEvent) {
    e.preventDefault();
    const files = fileInputRef.current?.files;
    if (!workspace || !fileConnectorName.trim() || !files || files.length === 0) return;
    setCreatingFileConnector(true);
    setError(null);
    try {
      const connector = await createFileConnector(workspace.id, fileConnectorName, Array.from(files));
      setConnectors((prev) => [...prev, connector]);
      setFileConnectorName("");
      if (fileInputRef.current) fileInputRef.current.value = "";
    } catch (err) {
      setError(err instanceof KnowledgeError ? err.message : "Something went wrong.");
    } finally {
      setCreatingFileConnector(false);
    }
  }

  async function handleToggleExpand(connector: Connector) {
    if (!workspace) return;
    if (expandedId === connector.id) {
      setExpandedId(null);
      return;
    }
    setExpandedId(connector.id);
    if (!documentsByConnector[connector.id]) {
      try {
        const detail = await getConnector(workspace.id, connector.id);
        setDocumentsByConnector((prev) => ({ ...prev, [connector.id]: detail.documents }));
      } catch (err) {
        setError(err instanceof KnowledgeError ? err.message : "Something went wrong.");
      }
    }
  }

  async function handleAddDocuments(connectorId: string, files: FileList | null) {
    if (!workspace || !files || files.length === 0) return;
    setAddingDocsFor(connectorId);
    setError(null);
    try {
      const detail = await addConnectorDocuments(workspace.id, connectorId, Array.from(files));
      setDocumentsByConnector((prev) => ({ ...prev, [connectorId]: detail.documents }));
      setConnectors((prev) => prev.map((c) => (c.id === connectorId ? detail : c)));
    } catch (err) {
      setError(err instanceof KnowledgeError ? err.message : "Something went wrong.");
    } finally {
      setAddingDocsFor(null);
    }
  }

  async function handleDeleteDocument(connectorId: string, documentId: string) {
    if (!workspace) return;
    setError(null);
    try {
      await deleteConnectorDocument(workspace.id, connectorId, documentId);
      setDocumentsByConnector((prev) => ({
        ...prev,
        [connectorId]: (prev[connectorId] ?? []).filter((d) => d.id !== documentId),
      }));
    } catch (err) {
      setError(err instanceof KnowledgeError ? err.message : "Something went wrong.");
    }
  }

  async function handleReindex(connectorId: string) {
    if (!workspace) return;
    setError(null);
    try {
      const updated = await reindexConnector(workspace.id, connectorId);
      setConnectors((prev) => prev.map((c) => (c.id === connectorId ? updated : c)));
    } catch (err) {
      setError(err instanceof KnowledgeError ? err.message : "Something went wrong.");
    }
  }

  async function handleDeleteConnector(connectorId: string) {
    if (!workspace) return;
    setError(null);
    try {
      await deleteConnector(workspace.id, connectorId);
      setConnectors((prev) => prev.filter((c) => c.id !== connectorId));
      if (expandedId === connectorId) setExpandedId(null);
    } catch (err) {
      setError(err instanceof KnowledgeError ? err.message : "Something went wrong.");
    }
  }

  if (authLoading || wsLoading || flagsLoading || loadingData) {
    return <div className="mx-auto max-w-3xl px-6 py-16 text-sm text-text-muted">Loading…</div>;
  }
  if (!workspace) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-16">
        <p className="text-sm text-text-soft">You don&apos;t have access to this workspace.</p>
      </div>
    );
  }
  if (!knowledgeEnabled) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-16">
        <h1 className="text-2xl font-semibold tracking-tight text-text">Knowledge</h1>
        <p className="mt-6 text-sm text-text-soft">
          This feature isn&apos;t enabled for your workspace yet.
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-16">
      <h1 className="text-2xl font-semibold tracking-tight text-text">Knowledge (ReStore)</h1>
      <p className="mt-1 text-sm text-text-muted">{workspace.name}</p>
      <p className="mt-3 text-sm text-text-muted">
        Connectors — websites to crawl or files to read — indexed in the background so an
        assistant can retrieve from them during chat.
      </p>

      {error && (
        <p className="mt-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      {isAdmin && (
        <div className="mt-8">
          <h2 className="text-sm font-semibold text-text">Embedding model</h2>
          <p className="mt-1 text-sm text-text-muted">
            One model for the whole workspace — every connector is indexed with it. Enable an
            embedding model on the{" "}
            <a href={`/w/${slug}/settings/providers`} className="text-accent">
              Providers
            </a>{" "}
            page first.
          </p>
          <form onSubmit={handleSaveSettings} className="mt-3 flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1.5 text-sm">
              <span className="text-text-soft">Model</span>
              <select
                value={selectedModelId}
                onChange={(e) => setSelectedModelId(e.target.value)}
                className="w-64 rounded-md border border-border bg-surface px-3 py-2 text-text"
              >
                <option value="">None chosen</option>
                {embeddingModels.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.display_name}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="submit"
              disabled={savingSettings}
              className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-60"
            >
              {savingSettings ? "Saving…" : "Save"}
            </button>
          </form>
        </div>
      )}

      {!settings?.embedding_model_id && (
        <p className="mt-6 text-sm text-text-soft">
          {isAdmin
            ? "Choose an embedding model above before adding connectors."
            : "An admin needs to choose an embedding model before connectors can be indexed."}
        </p>
      )}

      <div className="mt-10">
        <h2 className="text-sm font-semibold text-text">Connectors</h2>

        {connectors.length > 0 && (
          <ul className="mt-3 flex flex-col gap-2">
            {connectors.map((c) => {
              const documents = documentsByConnector[c.id];
              return (
                <li key={c.id} className="rounded-md border border-border bg-surface px-3 py-2 text-sm">
                  <div className="flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-text">{c.name}</span>
                        <span className="rounded-full border border-border-strong px-2 py-0.5 font-mono text-xs text-text-soft">
                          {c.kind}
                        </span>
                        <span className={`rounded-full border px-2 py-0.5 text-xs ${STATUS_CLASS[c.status]}`}>
                          {STATUS_LABEL[c.status]}
                        </span>
                        {c.needs_reindex && c.status !== "pending" && (
                          <span className="rounded-full border border-warning/40 bg-warning/10 px-2 py-0.5 text-xs text-warning">
                            Needs reindex
                          </span>
                        )}
                      </div>
                      <p className="mt-0.5 truncate text-xs text-text-muted">
                        {c.url ?? `${c.document_count} document${c.document_count === 1 ? "" : "s"}`} ·{" "}
                        {c.chunk_count} chunk{c.chunk_count === 1 ? "" : "s"}
                      </p>
                      {c.error && <p className="mt-0.5 truncate text-xs text-danger">{c.error}</p>}
                    </div>
                    <div className="flex shrink-0 items-center gap-3">
                      <button
                        type="button"
                        onClick={() => handleToggleExpand(c)}
                        className="text-xs text-text-soft hover:underline"
                      >
                        {expandedId === c.id ? "Hide" : "Details"}
                      </button>
                      <button
                        type="button"
                        onClick={() => handleReindex(c.id)}
                        className="text-xs text-text-soft hover:underline"
                      >
                        Reindex
                      </button>
                      <button
                        type="button"
                        onClick={() => handleDeleteConnector(c.id)}
                        className="text-xs text-danger hover:underline"
                      >
                        Delete
                      </button>
                    </div>
                  </div>

                  {expandedId === c.id && (
                    <div className="mt-3 border-t border-border pt-3">
                      {documents === undefined ? (
                        <p className="text-xs text-text-muted">Loading…</p>
                      ) : documents.length === 0 ? (
                        <p className="text-xs text-text-muted">No documents yet.</p>
                      ) : (
                        <ul className="flex flex-col gap-1.5">
                          {documents.map((d) => (
                            <li key={d.id} className="flex items-center justify-between gap-2 text-xs">
                              <span className="min-w-0 truncate text-text-soft">
                                {d.filename ?? d.source_url ?? d.title ?? "(untitled)"}
                                {d.status === "failed" && (
                                  <span className="ml-1.5 text-danger">— {d.error ?? "failed"}</span>
                                )}
                              </span>
                              {c.kind === "file" && (
                                <button
                                  type="button"
                                  onClick={() => handleDeleteDocument(c.id, d.id)}
                                  className="shrink-0 text-danger hover:underline"
                                >
                                  Delete
                                </button>
                              )}
                            </li>
                          ))}
                        </ul>
                      )}
                      {c.kind === "file" && (
                        <label className="mt-3 inline-block cursor-pointer text-xs text-accent hover:underline">
                          {addingDocsFor === c.id ? "Uploading…" : "+ Add files"}
                          <input
                            type="file"
                            multiple
                            disabled={addingDocsFor === c.id}
                            onChange={(e) => handleAddDocuments(c.id, e.target.files)}
                            className="hidden"
                          />
                        </label>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}

        <div className="mt-4 flex flex-col gap-4 sm:flex-row">
          <form
            onSubmit={handleCreateWebsite}
            className="flex flex-1 flex-col gap-3 rounded-md border border-border bg-surface p-4"
          >
            <p className="text-xs font-semibold uppercase tracking-wide text-text-muted">Add a website</p>
            <label className="flex flex-col gap-1.5 text-sm">
              <span className="text-text-soft">Name</span>
              <input
                type="text"
                required
                value={websiteName}
                onChange={(e) => setWebsiteName(e.target.value)}
                placeholder="Docs site"
                className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
              />
            </label>
            <label className="flex flex-col gap-1.5 text-sm">
              <span className="text-text-soft">Start URL</span>
              <input
                type="url"
                required
                value={websiteUrl}
                onChange={(e) => setWebsiteUrl(e.target.value)}
                placeholder="https://docs.example.com/"
                className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
              />
            </label>
            <label className="flex flex-col gap-1.5 text-sm">
              <span className="text-text-soft">Max pages</span>
              <input
                type="number"
                min={1}
                max={100}
                value={maxPages}
                onChange={(e) => setMaxPages(Number(e.target.value))}
                className="w-24 rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
              />
            </label>
            <button
              type="submit"
              disabled={creatingWebsite}
              className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-60"
            >
              {creatingWebsite ? "Adding…" : "Add website"}
            </button>
          </form>

          <form
            onSubmit={handleCreateFileConnector}
            className="flex flex-1 flex-col gap-3 rounded-md border border-border bg-surface p-4"
          >
            <p className="text-xs font-semibold uppercase tracking-wide text-text-muted">Add files</p>
            <label className="flex flex-col gap-1.5 text-sm">
              <span className="text-text-soft">Name</span>
              <input
                type="text"
                required
                value={fileConnectorName}
                onChange={(e) => setFileConnectorName(e.target.value)}
                placeholder="Onboarding docs"
                className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
              />
            </label>
            <label className="flex flex-col gap-1.5 text-sm">
              <span className="text-text-soft">Files</span>
              <input
                ref={fileInputRef}
                type="file"
                multiple
                required
                className="text-sm text-text-soft"
              />
            </label>
            <button
              type="submit"
              disabled={creatingFileConnector}
              className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-60"
            >
              {creatingFileConnector ? "Adding…" : "Add files"}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
