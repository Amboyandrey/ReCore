"use client";

import { useEffect, useState } from "react";
import { evaluateFlags, type ResolvedFlags } from "./flag-client";

// Resolves a workspace's flags on mount and whenever the workspace changes — the client-side
// stand-in for ARCHITECTURE.md's server-fetched FlagsProvider, matching how this app's other
// per-workspace state (models, credentials) is already fetched client-side, not server-rendered.
export function useWorkspaceFlags(workspaceId: string | undefined): {
  flags: ResolvedFlags;
  loading: boolean;
} {
  const [flags, setFlags] = useState<ResolvedFlags>({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const task = workspaceId ? evaluateFlags(workspaceId) : Promise.resolve<ResolvedFlags>({});
    task.then(setFlags).finally(() => setLoading(false));
  }, [workspaceId]);

  return { flags, loading };
}
