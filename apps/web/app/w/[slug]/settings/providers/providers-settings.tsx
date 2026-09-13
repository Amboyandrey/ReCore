"use client";

import Link from "next/link";
import { useEffect, useState, type FormEvent } from "react";
import { useRequireAuth } from "@/lib/auth-context";
import {
  createCredential,
  deleteCredential,
  disableModel,
  enableModel,
  listAvailableModels,
  listCredentials,
  listModels,
  ProviderError,
  type AvailableModel,
  type Credential,
  type EnabledModel,
  type ProviderId,
} from "@/lib/provider-client";
import { useWorkspaceBySlug } from "@/lib/workspace-context";

const PROVIDERS: ProviderId[] = ["anthropic", "openai", "google", "openai_compatible"];
const PROVIDER_LABELS: Record<ProviderId, string> = {
  anthropic: "Anthropic",
  openai: "OpenAI",
  google: "Google",
  openai_compatible: "OpenAI-compatible",
};

// The LLM providers settings page: register API keys, browse what they can see, enable models.
export function ProvidersSettings({ slug }: { slug: string }) {
  const { loading: authLoading } = useRequireAuth();
  const { workspace, loading: wsLoading } = useWorkspaceBySlug(slug);
  const isAdmin = workspace?.role === "admin" || workspace?.role === "owner";

  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [models, setModels] = useState<EnabledModel[]>([]);
  // Embedding-kind models are fetched and shown separately — they don't belong in the chat
  // picker's own enabled-models table, and editing pricing/vision on them isn't supported here
  // (their pricing is set once at enable time; vision doesn't apply to embeddings at all).
  const [embeddingModels, setEmbeddingModels] = useState<EnabledModel[]>([]);
  const [loadingData, setLoadingData] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [provider, setProvider] = useState<ProviderId>("anthropic");
  const [label, setLabel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [adding, setAdding] = useState(false);

  const [browseCredentialId, setBrowseCredentialId] = useState("");
  const [available, setAvailable] = useState<AvailableModel[] | null>(null);
  const [browsing, setBrowsing] = useState(false);
  // Which of the currently-browsed available models the admin has checked "supports images" for
  // — read at enable time, since AvailableModel (what the provider reports) has no such field.
  const [visionChoices, setVisionChoices] = useState<Record<string, boolean>>({});
  // Same idea, for "enable this as an embedding model instead of a chat model" — mutually
  // exclusive with vision in practice (embedding models don't stream chat replies), but tracked
  // independently since nothing stops both being left unchecked (defaults to a chat model).
  const [embeddingChoices, setEmbeddingChoices] = useState<Record<string, boolean>>({});

  // Deferred entirely into .then()/.finally() — see workspace-context.tsx for why: calling
  // setState directly at an effect's top level (even in an early-return branch) risks cascading
  // renders under Next's stricter react-hooks rules.
  useEffect(() => {
    const task =
      workspace && isAdmin
        ? Promise.all([
            listCredentials(workspace.id),
            listModels(workspace.id),
            listModels(workspace.id, "embedding"),
          ])
        : Promise.resolve<[Credential[], EnabledModel[], EnabledModel[]]>([[], [], []]);
    task
      .then(([c, m, e]) => {
        setCredentials(c);
        setModels(m);
        setEmbeddingModels(e);
        setBrowseCredentialId((prev) => prev || c[0]?.id || "");
      })
      .finally(() => setLoadingData(false));
  }, [workspace, isAdmin]);

  async function handleAddCredential(e: FormEvent) {
    e.preventDefault();
    if (!workspace) return;
    setError(null);
    setAdding(true);
    try {
      const created = await createCredential(workspace.id, {
        provider,
        label,
        api_key: apiKey,
        base_url: provider === "openai_compatible" ? baseUrl : undefined,
      });
      setCredentials((prev) => [created, ...prev]);
      setLabel("");
      setApiKey("");
      setBaseUrl("");
      setBrowseCredentialId((prev) => prev || created.id);
    } catch (err) {
      setError(err instanceof ProviderError ? err.message : "Something went wrong.");
    } finally {
      setAdding(false);
    }
  }

  async function handleRemoveCredential(id: string) {
    if (!workspace) return;
    setError(null);
    try {
      await deleteCredential(workspace.id, id);
      setCredentials((prev) => prev.filter((c) => c.id !== id));
      setModels((prev) => prev.filter((m) => m.credential_id !== id));
      setEmbeddingModels((prev) => prev.filter((m) => m.credential_id !== id));
    } catch (err) {
      setError(err instanceof ProviderError ? err.message : "Something went wrong.");
    }
  }

  async function handleBrowseModels() {
    if (!workspace || !browseCredentialId) return;
    setError(null);
    setBrowsing(true);
    setAvailable(null);
    try {
      setAvailable(await listAvailableModels(workspace.id, browseCredentialId));
    } catch (err) {
      setError(err instanceof ProviderError ? err.message : "Something went wrong.");
    } finally {
      setBrowsing(false);
    }
  }

  async function handleEnable(m: AvailableModel) {
    if (!workspace || !browseCredentialId) return;
    setError(null);
    const isEmbedding = embeddingChoices[m.id] ?? false;
    try {
      const enabled = await enableModel(workspace.id, {
        credential_id: browseCredentialId,
        provider_model_id: m.id,
        display_name: m.display_name,
        context_window: m.context_window,
        supports_vision: visionChoices[m.id] ?? false,
        kind: isEmbedding ? "embedding" : "chat",
      });
      if (isEmbedding) {
        setEmbeddingModels((prev) => [...prev.filter((x) => x.id !== enabled.id), enabled]);
      } else {
        setModels((prev) => [...prev.filter((x) => x.id !== enabled.id), enabled]);
      }
    } catch (err) {
      setError(err instanceof ProviderError ? err.message : "Something went wrong.");
    }
  }

  async function handlePricingChange(
    m: EnabledModel,
    field: "cost_per_mtok_in" | "cost_per_mtok_out",
    value: string
  ) {
    if (!workspace) return;
    const numeric = value === "" ? null : Number(value);
    try {
      const updated = await enableModel(workspace.id, {
        credential_id: m.credential_id,
        provider_model_id: m.provider_model_id,
        display_name: m.display_name,
        context_window: m.context_window,
        cost_per_mtok_in: field === "cost_per_mtok_in" ? numeric : m.cost_per_mtok_in,
        cost_per_mtok_out: field === "cost_per_mtok_out" ? numeric : m.cost_per_mtok_out,
        // enable_model() overwrites every field on a re-enable, this pricing edit included — omit
        // this and a price edit would silently turn vision back off.
        supports_vision: m.supports_vision,
      });
      setModels((prev) => prev.map((x) => (x.id === updated.id ? updated : x)));
    } catch (err) {
      setError(err instanceof ProviderError ? err.message : "Something went wrong.");
    }
  }

  async function handleVisionChange(m: EnabledModel, supportsVision: boolean) {
    if (!workspace) return;
    try {
      const updated = await enableModel(workspace.id, {
        credential_id: m.credential_id,
        provider_model_id: m.provider_model_id,
        display_name: m.display_name,
        context_window: m.context_window,
        cost_per_mtok_in: m.cost_per_mtok_in,
        cost_per_mtok_out: m.cost_per_mtok_out,
        supports_vision: supportsVision,
      });
      setModels((prev) => prev.map((x) => (x.id === updated.id ? updated : x)));
    } catch (err) {
      setError(err instanceof ProviderError ? err.message : "Something went wrong.");
    }
  }

  async function handleDisable(modelId: string) {
    if (!workspace) return;
    setError(null);
    try {
      await disableModel(workspace.id, modelId);
      setModels((prev) => prev.filter((m) => m.id !== modelId));
      setEmbeddingModels((prev) => prev.filter((m) => m.id !== modelId));
    } catch (err) {
      setError(err instanceof ProviderError ? err.message : "Something went wrong.");
    }
  }

  if (authLoading || wsLoading || loadingData) {
    return <div className="mx-auto max-w-3xl px-6 py-16 text-sm text-text-muted">Loading…</div>;
  }

  if (!workspace) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-16">
        <p className="text-sm text-text-soft">You don&apos;t have access to this workspace.</p>
      </div>
    );
  }

  if (!isAdmin) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-16">
        <p className="text-sm text-text-soft">Only workspace admins can manage LLM providers.</p>
      </div>
    );
  }

  const enabledKeys = new Set(
    [...models, ...embeddingModels].map((m) => `${m.credential_id}:${m.provider_model_id}`)
  );

  return (
    <div className="mx-auto max-w-3xl px-6 py-16">
      <h1 className="text-2xl font-semibold tracking-tight text-text">LLM providers</h1>
      <p className="mt-1 text-sm text-text-muted">{workspace.name}</p>

      {error && (
        <p className="mt-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      <div className="mt-8">
        <h2 className="text-sm font-semibold text-text">Provider keys</h2>
        {credentials.length > 0 && (
          <ul className="mt-3 flex flex-col gap-2">
            {credentials.map((c) => (
              <li
                key={c.id}
                className="flex items-center justify-between rounded-md border border-border bg-surface px-3 py-2 text-sm"
              >
                <div className="flex items-center gap-3">
                  <span className="rounded-full border border-border-strong px-2 py-0.5 font-mono text-xs text-text-soft">
                    {PROVIDER_LABELS[c.provider]}
                  </span>
                  <span className="text-text">{c.label}</span>
                  <span className="font-mono text-xs text-text-muted">•••• {c.last4}</span>
                </div>
                <button
                  type="button"
                  onClick={() => handleRemoveCredential(c.id)}
                  className="text-xs text-danger hover:underline"
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
        )}

        <form onSubmit={handleAddCredential} className="mt-4 flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1.5 text-sm">
            <span className="text-text-soft">Provider</span>
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value as ProviderId)}
              className="rounded-md border border-border bg-surface px-3 py-2 text-text"
            >
              {PROVIDERS.map((p) => (
                <option key={p} value={p}>
                  {PROVIDER_LABELS[p]}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1.5 text-sm">
            <span className="text-text-soft">Label</span>
            <input
              type="text"
              required
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="Production"
              className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
            />
          </label>
          <label className="flex flex-col gap-1.5 text-sm">
            <span className="text-text-soft">API key</span>
            <input
              type="password"
              required
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
            />
          </label>
          {provider === "openai_compatible" && (
            <label className="flex flex-col gap-1.5 text-sm">
              <span className="text-text-soft">Base URL</span>
              <input
                type="url"
                required
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="http://localhost:11434/v1"
                className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
              />
            </label>
          )}
          <button
            type="submit"
            disabled={adding}
            className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-60"
          >
            {adding ? "Validating…" : "Add key"}
          </button>
        </form>
      </div>

      {credentials.length > 0 && (
        <div className="mt-10">
          <h2 className="text-sm font-semibold text-text">Enable a model</h2>
          <div className="mt-3 flex items-end gap-3">
            <label className="flex flex-col gap-1.5 text-sm">
              <span className="text-text-soft">Credential</span>
              <select
                value={browseCredentialId}
                onChange={(e) => {
                  setBrowseCredentialId(e.target.value);
                  setAvailable(null);
                }}
                className="rounded-md border border-border bg-surface px-3 py-2 text-text"
              >
                {credentials.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.label}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              onClick={handleBrowseModels}
              disabled={browsing}
              className="rounded-md border border-border px-3 py-2 text-sm text-text-soft disabled:opacity-60"
            >
              {browsing ? "Loading…" : "Browse models"}
            </button>
          </div>

          {available && (
            <ul className="mt-3 flex flex-col gap-2">
              {available.length === 0 && (
                <li className="text-sm text-text-muted">No models reported by this provider.</li>
              )}
              {available.map((m) => {
                const alreadyEnabled = enabledKeys.has(`${browseCredentialId}:${m.id}`);
                return (
                  <li
                    key={m.id}
                    className="flex items-center justify-between rounded-md border border-border bg-surface px-3 py-2 text-sm"
                  >
                    <div>
                      <span className="text-text">{m.display_name}</span>
                      {m.context_window && (
                        <span className="ml-2 font-mono text-xs text-text-muted">
                          {m.context_window.toLocaleString()} ctx
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-3">
                      <label className="flex items-center gap-1.5 text-xs text-text-soft">
                        <input
                          type="checkbox"
                          checked={visionChoices[m.id] ?? false}
                          disabled={alreadyEnabled}
                          onChange={(e) =>
                            setVisionChoices((prev) => ({ ...prev, [m.id]: e.target.checked }))
                          }
                        />
                        Supports images
                      </label>
                      <label className="flex items-center gap-1.5 text-xs text-text-soft">
                        <input
                          type="checkbox"
                          checked={embeddingChoices[m.id] ?? false}
                          disabled={alreadyEnabled}
                          onChange={(e) =>
                            setEmbeddingChoices((prev) => ({ ...prev, [m.id]: e.target.checked }))
                          }
                        />
                        Embedding model
                      </label>
                      <button
                        type="button"
                        onClick={() => handleEnable(m)}
                        disabled={alreadyEnabled}
                        className="text-xs text-accent hover:underline disabled:text-text-muted disabled:no-underline"
                      >
                        {alreadyEnabled ? "Enabled" : "Enable"}
                      </button>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}

      {models.length > 0 && (
        <div className="mt-10">
          <h2 className="text-sm font-semibold text-text">Enabled models</h2>
          <div className="mt-3 overflow-x-auto rounded-lg border border-border">
            <table className="w-full min-w-[560px] text-sm">
              <thead>
                <tr className="border-b border-border bg-surface-sunk text-left text-xs uppercase tracking-wide text-text-muted">
                  <th className="px-4 py-2 font-medium">Model</th>
                  <th className="px-4 py-2 font-medium">$ / Mtok in</th>
                  <th className="px-4 py-2 font-medium">$ / Mtok out</th>
                  <th className="px-4 py-2 font-medium">Vision</th>
                  <th className="px-4 py-2 font-medium" />
                </tr>
              </thead>
              <tbody>
                {models.map((m) => (
                  <tr key={m.id} className="border-b border-border last:border-0">
                    <td className="px-4 py-2 text-text">{m.display_name}</td>
                    <td className="px-4 py-2">
                      <input
                        type="number"
                        step="0.01"
                        min="0"
                        defaultValue={m.cost_per_mtok_in ?? ""}
                        onBlur={(e) => handlePricingChange(m, "cost_per_mtok_in", e.target.value)}
                        className="w-20 rounded-md border border-border bg-surface px-2 py-1 text-sm text-text"
                      />
                    </td>
                    <td className="px-4 py-2">
                      <input
                        type="number"
                        step="0.01"
                        min="0"
                        defaultValue={m.cost_per_mtok_out ?? ""}
                        onBlur={(e) => handlePricingChange(m, "cost_per_mtok_out", e.target.value)}
                        className="w-20 rounded-md border border-border bg-surface px-2 py-1 text-sm text-text"
                      />
                    </td>
                    <td className="px-4 py-2">
                      <input
                        type="checkbox"
                        checked={m.supports_vision}
                        onChange={(e) => handleVisionChange(m, e.target.checked)}
                      />
                    </td>
                    <td className="px-4 py-2 text-right">
                      <button
                        type="button"
                        onClick={() => handleDisable(m.id)}
                        className="text-xs text-danger hover:underline"
                      >
                        Disable
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {embeddingModels.length > 0 && (
        <div className="mt-10">
          <h2 className="text-sm font-semibold text-text">Embedding models</h2>
          <p className="mt-1 text-sm text-text-muted">
            Turn text into vectors for Knowledge (ReStore) connectors — chosen from these in{" "}
            <Link href={`/w/${slug}/settings/knowledge`} className="text-accent">
              Knowledge settings
            </Link>
            , not used for chat.
          </p>
          <ul className="mt-3 flex flex-col gap-2">
            {embeddingModels.map((m) => (
              <li
                key={m.id}
                className="flex items-center justify-between rounded-md border border-border bg-surface px-3 py-2 text-sm"
              >
                <span className="text-text">{m.display_name}</span>
                <button
                  type="button"
                  onClick={() => handleDisable(m.id)}
                  className="text-xs text-danger hover:underline"
                >
                  Disable
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
