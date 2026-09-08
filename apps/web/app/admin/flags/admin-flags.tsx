"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useRequireAuth } from "@/lib/auth-context";
import {
  createFlag,
  deleteOverride,
  FlagError,
  listFlags,
  listOverrides,
  setOverride,
  updateFlag,
  type Flag,
  type FlagOverride,
  type FlagScope,
} from "@/lib/flag-client";

// The platform-wide flag admin: define flags, flip their default, and pin per-user/workspace
// overrides — the lever behind the provider-killswitch demo and the `attachments` rollout.
export function AdminFlags() {
  const { user, loading: authLoading } = useRequireAuth();

  const [flags, setFlags] = useState<Flag[]>([]);
  const [loadingFlags, setLoadingFlags] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [key, setKey] = useState("");
  const [description, setDescription] = useState("");
  const [defaultValue, setDefaultValue] = useState(false);
  const [creating, setCreating] = useState(false);

  const [expandedFlagId, setExpandedFlagId] = useState<string | null>(null);
  const [overrides, setOverrides] = useState<FlagOverride[]>([]);
  const [overrideScope, setOverrideScope] = useState<FlagScope>("workspace");
  const [overrideScopeId, setOverrideScopeId] = useState("");
  const [overrideValue, setOverrideValue] = useState(false);

  // Deferred into .then()/.finally() — see workspace-context.tsx for why this shape is required.
  useEffect(() => {
    const task = user?.is_superuser ? listFlags() : Promise.resolve<Flag[]>([]);
    task.then(setFlags).finally(() => setLoadingFlags(false));
  }, [user]);

  async function handleCreate(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setCreating(true);
    try {
      const flag = await createFlag({ key, description, default_value: defaultValue });
      setFlags((prev) => [...prev, flag].sort((a, b) => a.key.localeCompare(b.key)));
      setKey("");
      setDescription("");
      setDefaultValue(false);
    } catch (err) {
      setError(err instanceof FlagError ? err.message : "Something went wrong.");
    } finally {
      setCreating(false);
    }
  }

  async function handleToggleDefault(flag: Flag) {
    setError(null);
    try {
      const updated = await updateFlag(flag.id, { default_value: !flag.default_value });
      setFlags((prev) => prev.map((f) => (f.id === updated.id ? updated : f)));
    } catch (err) {
      setError(err instanceof FlagError ? err.message : "Something went wrong.");
    }
  }

  async function handleArchive(flag: Flag) {
    setError(null);
    try {
      await updateFlag(flag.id, { archived: true });
      setFlags((prev) => prev.filter((f) => f.id !== flag.id));
      if (expandedFlagId === flag.id) setExpandedFlagId(null);
    } catch (err) {
      setError(err instanceof FlagError ? err.message : "Something went wrong.");
    }
  }

  async function handleExpand(flag: Flag) {
    if (expandedFlagId === flag.id) {
      setExpandedFlagId(null);
      return;
    }
    setExpandedFlagId(flag.id);
    setOverrides(await listOverrides(flag.id));
  }

  async function handleAddOverride(e: FormEvent, flag: Flag) {
    e.preventDefault();
    setError(null);
    try {
      const created = await setOverride(flag.id, {
        scope: overrideScope,
        scope_id: overrideScopeId,
        value: overrideValue,
      });
      setOverrides((prev) => [...prev.filter((o) => o.id !== created.id), created]);
      setOverrideScopeId("");
    } catch (err) {
      setError(err instanceof FlagError ? err.message : "Something went wrong.");
    }
  }

  async function handleRemoveOverride(flag: Flag, override: FlagOverride) {
    setError(null);
    try {
      await deleteOverride(flag.id, override.id);
      setOverrides((prev) => prev.filter((o) => o.id !== override.id));
    } catch (err) {
      setError(err instanceof FlagError ? err.message : "Something went wrong.");
    }
  }

  if (authLoading || loadingFlags) {
    return <div className="mx-auto max-w-3xl px-6 py-16 text-sm text-text-muted">Loading…</div>;
  }

  if (!user?.is_superuser) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-16">
        <p className="text-sm text-text-soft">This page requires superuser access.</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-16">
      <h1 className="text-2xl font-semibold tracking-tight text-text">Feature flags</h1>
      <p className="mt-1 text-sm text-text-muted">
        Definitions, defaults, and the per-user or per-workspace overrides that beat them.
      </p>

      {error && (
        <p className="mt-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      <ul className="mt-8 flex flex-col gap-2">
        {flags.map((flag) => (
          <li key={flag.id} className="rounded-md border border-border bg-surface px-3 py-2 text-sm">
            <div className="flex items-center justify-between gap-3">
              <button
                type="button"
                onClick={() => handleExpand(flag)}
                className="flex-1 truncate text-left"
              >
                <span className="font-mono text-xs text-text-soft">{flag.key}</span>
                <span className="ml-2 text-text-muted">{flag.description}</span>
              </button>
              <label className="flex items-center gap-1.5 text-xs text-text-soft">
                <input
                  type="checkbox"
                  checked={flag.default_value === true}
                  onChange={() => handleToggleDefault(flag)}
                />
                default
              </label>
              {flag.rollout_percentage !== null && (
                <span className="font-mono text-xs text-text-muted">{flag.rollout_percentage}% rollout</span>
              )}
              <button
                type="button"
                onClick={() => handleArchive(flag)}
                className="text-xs text-danger hover:underline"
              >
                Archive
              </button>
            </div>

            {expandedFlagId === flag.id && (
              <div className="mt-3 border-t border-border pt-3">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-text-muted">
                  Overrides
                </h3>
                {overrides.length === 0 && (
                  <p className="mt-2 text-xs text-text-muted">None — every caller sees the default.</p>
                )}
                <ul className="mt-2 flex flex-col gap-1.5">
                  {overrides.map((o) => (
                    <li
                      key={o.id}
                      className="flex items-center justify-between rounded-md border border-border-strong px-2 py-1 text-xs"
                    >
                      <span className="font-mono text-text-soft">
                        {o.scope}:{o.scope_id} → {String(o.value)}
                      </span>
                      <button
                        type="button"
                        onClick={() => handleRemoveOverride(flag, o)}
                        className="text-danger hover:underline"
                      >
                        Remove
                      </button>
                    </li>
                  ))}
                </ul>

                <form
                  onSubmit={(e) => handleAddOverride(e, flag)}
                  className="mt-3 flex flex-wrap items-end gap-2"
                >
                  <label className="flex flex-col gap-1 text-xs">
                    <span className="text-text-soft">Scope</span>
                    <select
                      value={overrideScope}
                      onChange={(e) => setOverrideScope(e.target.value as FlagScope)}
                      className="rounded-md border border-border bg-surface px-2 py-1 text-text"
                    >
                      <option value="workspace">Workspace</option>
                      <option value="user">User</option>
                    </select>
                  </label>
                  <label className="flex flex-col gap-1 text-xs">
                    <span className="text-text-soft">{overrideScope === "user" ? "User id" : "Workspace id"}</span>
                    <input
                      type="text"
                      required
                      value={overrideScopeId}
                      onChange={(e) => setOverrideScopeId(e.target.value)}
                      placeholder="uuid"
                      className="w-64 rounded-md border border-border bg-surface px-2 py-1 font-mono text-text"
                    />
                  </label>
                  <label className="flex items-center gap-1.5 text-xs text-text-soft">
                    <input
                      type="checkbox"
                      checked={overrideValue}
                      onChange={(e) => setOverrideValue(e.target.checked)}
                    />
                    value
                  </label>
                  <button
                    type="submit"
                    className="rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-accent-contrast"
                  >
                    Pin
                  </button>
                </form>
              </div>
            )}
          </li>
        ))}
      </ul>

      <form onSubmit={handleCreate} className="mt-8 flex flex-wrap items-end gap-3 border-t border-border pt-6">
        <label className="flex flex-col gap-1.5 text-sm">
          <span className="text-text-soft">Key</span>
          <input
            type="text"
            required
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder="my.new.flag"
            className="rounded-md border border-border bg-surface px-3 py-2 font-mono text-text outline-none focus:border-accent"
          />
        </label>
        <label className="flex flex-col gap-1.5 text-sm">
          <span className="text-text-soft">Description</span>
          <input
            type="text"
            required
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="w-64 rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
          />
        </label>
        <label className="flex items-center gap-1.5 text-sm text-text-soft">
          <input
            type="checkbox"
            checked={defaultValue}
            onChange={(e) => setDefaultValue(e.target.checked)}
          />
          default on
        </label>
        <button
          type="submit"
          disabled={creating}
          className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-60"
        >
          {creating ? "Creating…" : "Create flag"}
        </button>
      </form>
    </div>
  );
}
