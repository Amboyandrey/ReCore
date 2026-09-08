"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { useRequireAuth } from "@/lib/auth-context";
import { createConversation } from "@/lib/chat-client";
import { getLastModelId, setLastModelId } from "@/lib/last-model";
import { listModels, type EnabledModel } from "@/lib/provider-client";
import { useWorkspaceBySlug } from "@/lib/workspace-context";

// Picks which model a brand-new chat should start on: whichever one this workspace used last
// (remembered across sessions), falling back to the first enabled, undisabled model. The model
// itself is changeable from the chat page's own picker, so this is just a starting guess, not a
// decision the user has to make before they can start typing.
function pickDefaultModel(workspaceId: string, models: EnabledModel[]): EnabledModel | null {
  if (models.length === 0) return null;
  const lastId = getLastModelId(workspaceId);
  const last = lastId && models.find((m) => m.id === lastId && m.provider_enabled);
  return last || models.find((m) => m.provider_enabled) || models[0];
}

// No model-picker page: landing here immediately starts a conversation on a best-guess model
// and drops straight into the chat page, which carries its own model switcher.
export function NewChat({ slug }: { slug: string }) {
  const { loading: authLoading } = useRequireAuth();
  const { workspace, loading: wsLoading } = useWorkspaceBySlug(slug);
  const router = useRouter();
  const [models, setModels] = useState<EnabledModel[]>([]);
  const [loadingModels, setLoadingModels] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const startedRef = useRef(false);

  useEffect(() => {
    const task = workspace ? listModels(workspace.id) : Promise.resolve<EnabledModel[]>([]);
    task.then(setModels).finally(() => setLoadingModels(false));
  }, [workspace]);

  useEffect(() => {
    if (!workspace || loadingModels || startedRef.current) return;
    const model = pickDefaultModel(workspace.id, models);
    if (!model) return; // no enabled models — render the empty state below instead
    startedRef.current = true;
    createConversation(workspace.id, model.id)
      .then((conversation) => {
        setLastModelId(workspace.id, model.id);
        router.replace(`/w/${slug}/c/${conversation.id}`);
      })
      .catch(() => {
        startedRef.current = false;
        setError("Couldn't start a new chat. Try again.");
      });
  }, [workspace, loadingModels, models, slug, router]);

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

  if (models.length === 0) {
    return (
      <div className="mx-auto max-w-md px-6 py-16">
        <h1 className="text-2xl font-semibold tracking-tight text-text">Start a chat</h1>
        <p className="mt-6 text-sm text-text-soft">
          No models are enabled yet.{" "}
          <Link href={`/w/${slug}/settings/providers`} className="text-accent">
            Enable one
          </Link>{" "}
          first.
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-md px-6 py-16">
      {error ? (
        <p className="text-sm text-danger">{error}</p>
      ) : (
        <p className="text-sm text-text-muted">Starting chat…</p>
      )}
    </div>
  );
}
