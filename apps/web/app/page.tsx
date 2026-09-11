import { getApiStatus } from "@/lib/api";
import { WorkspacePicker } from "@/components/workspace-picker";

// Renders a status pill whose color communicates state at a glance, not just its label.
function StatusPill({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 font-mono text-xs ${
        ok
          ? "border-success/30 bg-success/10 text-success"
          : "border-danger/30 bg-danger/10 text-danger"
      }`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${ok ? "bg-success" : "bg-danger"}`} />
      {label}
    </span>
  );
}

// Landing page for phase 1: proves the shell is wired to the API before any real feature exists.
export default async function Home() {
  const status = await getApiStatus();

  return (
    <div className="mx-auto max-w-5xl px-6 py-16">
      <div className="max-w-2xl">
        <h1 className="text-3xl font-semibold tracking-tight text-balance text-text">
          A chat platform for any LLM, one workspace at a time.
        </h1>
        <p className="mt-3 text-base leading-relaxed text-text-soft">
          Multi-workspace, bring-your-own API key, streaming chat over Anthropic, OpenAI, Google
          and any OpenAI-compatible endpoint. This shell is the foundation the rest of the
          platform builds on.
        </p>
      </div>

      <div className="mt-10 rounded-lg border border-border bg-surface p-6">
        <h2 className="font-mono text-xs uppercase tracking-wider text-text-muted">
          System status
        </h2>
        <div className="mt-4 flex flex-wrap gap-2">
          <StatusPill ok={status.reachable} label={status.reachable ? "api up" : "api down"} />
          {status.reachable && (
            <>
              <StatusPill ok={status.postgres === "up"} label={`postgres ${status.postgres}`} />
              <StatusPill ok={status.redis === "up"} label={`redis ${status.redis}`} />
            </>
          )}
        </div>
        {!status.reachable && (
          <p className="mt-3 text-sm text-text-muted">{status.error} — is the API running?</p>
        )}
      </div>

      <WorkspacePicker />
    </div>
  );
}
