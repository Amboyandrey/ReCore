"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { useAuth } from "./auth-context";
import {
  createWorkspace as createWorkspaceRequest,
  listWorkspaces,
  type WorkspaceSummary,
} from "./workspace-client";

type WorkspaceContextValue = {
  workspaces: WorkspaceSummary[];
  loading: boolean;
  refresh: () => Promise<void>;
  create: (name: string) => Promise<WorkspaceSummary>;
};

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

// Holds the signed-in user's workspaces, refetching whenever who's signed in changes.
export function WorkspaceProvider({ children }: { children: React.ReactNode }) {
  const { user } = useAuth();
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setWorkspaces(user ? await listWorkspaces() : []);
  }, [user]);

  // Every branch is deferred into a .then()/.finally() callback, never called directly at the
  // effect's top level — calling setState synchronously there would (per Next's stricter
  // react-hooks rules) risk cascading renders.
  useEffect(() => {
    const task = user ? listWorkspaces() : Promise.resolve<WorkspaceSummary[]>([]);
    task.then(setWorkspaces).finally(() => setLoading(false));
  }, [user]);

  async function create(name: string) {
    const workspace = await createWorkspaceRequest(name);
    setWorkspaces((prev) => [workspace, ...prev]);
    return workspace;
  }

  return (
    <WorkspaceContext value={{ workspaces, loading, refresh, create }}>{children}</WorkspaceContext>
  );
}

// Reads the signed-in user's workspaces — must be called under <WorkspaceProvider>.
export function useWorkspaces(): WorkspaceContextValue {
  const ctx = useContext(WorkspaceContext);
  if (!ctx) throw new Error("useWorkspaces must be used within a WorkspaceProvider");
  return ctx;
}

// Finds one workspace by its slug among the ones the user belongs to — used by every /w/[slug] page.
export function useWorkspaceBySlug(slug: string): { workspace: WorkspaceSummary | null; loading: boolean } {
  const { workspaces, loading } = useWorkspaces();
  return { workspace: workspaces.find((w) => w.slug === slug) ?? null, loading };
}
