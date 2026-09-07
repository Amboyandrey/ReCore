"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useRequireAuth } from "@/lib/auth-context";
import { createConversation } from "@/lib/chat-client";
import { listModels, type EnabledModel } from "@/lib/provider-client";
import { useWorkspaceBySlug } from "@/lib/workspace-context";

// Picking a model is the one decision starting a chat requires — everything else defaults.
export function NewChat({ slug }: { slug: string }) {
  const { loading: authLoading } = useRequireAuth();
  const { workspace, loading: wsLoading } = useWorkspaceBySlug(slug);
  const router = useRouter();
  const [models, setModels] = useState<EnabledModel[]>([]);
  const [loadingModels, setLoadingModels] = useState(true);
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    const task = workspace ? listModels(workspace.id) : Promise.resolve<EnabledModel[]>([]);
    task.then(setModels).finally(() => setLoadingModels(false));
  }, [workspace]);

  async function handlePick(modelId: string) {
    if (!workspace) return;
    setCreating(true);
    const conversation = await createConversation(workspace.id, modelId);
    router.push(`/w/${slug}/c/${conversation.id}`);
  }

  if (authLoading || wsLoading || loadingModels) {
    return <div className="mx-auto max-w-md px-6 py-16 text-sm text-text-muted">Loading…</div>;
  }
  if (!workspace) {
    return (
      <div className="mx-auto max-w-md px-6 py-16">
        <p className="text-sm text-text-soft">You don&apos;t have access to this workspace.</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-md px-6 py-16">
      <h1 className="text-2xl font-semibold tracking-tight text-text">Start a chat</h1>
      <p className="mt-1 text-sm text-text-muted">Pick a model to talk to.</p>

      {models.length === 0 ? (
        <p className="mt-6 text-sm text-text-soft">
          No models are enabled yet.{" "}
          <Link href={`/w/${slug}/settings/providers`} className="text-accent">
            Enable one
          </Link>{" "}
          first.
        </p>
      ) : (
        <div className="mt-6 flex flex-col gap-2">
          {models.map((m) => (
            <button
              key={m.id}
              type="button"
              disabled={creating}
              onClick={() => handlePick(m.id)}
              className="rounded-md border border-border bg-surface px-3 py-2 text-left text-sm text-text hover:border-border-strong disabled:opacity-60"
            >
              {m.display_name}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
