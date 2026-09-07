"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { acceptInvitation, MemberError, previewInvitation, type InvitePreview } from "@/lib/member-client";

// Shows what an invite link offers, then lets the matching signed-in account accept it.
export function InviteAccept({ token }: { token: string }) {
  const { user, loading: authLoading } = useAuth();
  const router = useRouter();
  const [preview, setPreview] = useState<InvitePreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [accepting, setAccepting] = useState(false);

  useEffect(() => {
    previewInvitation(token)
      .then(setPreview)
      .catch((err) => setError(err instanceof MemberError ? err.message : "Something went wrong."));
  }, [token]);

  async function handleAccept() {
    setAccepting(true);
    setError(null);
    try {
      const workspace = await acceptInvitation(token);
      router.push(`/w/${workspace.slug}`);
    } catch (err) {
      setError(err instanceof MemberError ? err.message : "Something went wrong.");
    } finally {
      setAccepting(false);
    }
  }

  if (error) {
    return (
      <div className="mx-auto max-w-sm px-6 py-16">
        <p className="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      </div>
    );
  }

  if (!preview || authLoading) {
    return <div className="mx-auto max-w-sm px-6 py-16 text-sm text-text-muted">Loading…</div>;
  }

  const next = `/invite/${token}`;
  const wrongAccount = !!user && user.email.toLowerCase() !== preview.email.toLowerCase();

  return (
    <div className="mx-auto max-w-sm px-6 py-16">
      <h1 className="text-2xl font-semibold tracking-tight text-text">You&apos;re invited</h1>
      <p className="mt-3 text-sm text-text-soft">
        Join <span className="text-text">{preview.workspace_name}</span> as{" "}
        <span className="font-mono text-xs">{preview.role}</span>, invited at {preview.email}.
      </p>

      {!user ? (
        <div className="mt-8 flex flex-col gap-3">
          <Link
            href={`/login?next=${encodeURIComponent(next)}`}
            className="rounded-md bg-accent px-3 py-2 text-center text-sm font-medium text-accent-contrast"
          >
            Sign in to accept
          </Link>
          <Link
            href={`/signup?next=${encodeURIComponent(next)}`}
            className="rounded-md border border-border px-3 py-2 text-center text-sm text-text-soft"
          >
            Create an account
          </Link>
        </div>
      ) : wrongAccount ? (
        <p className="mt-8 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          You&apos;re signed in as {user.email}, but this invite is for {preview.email}. Sign out
          and try again.
        </p>
      ) : (
        <button
          type="button"
          onClick={handleAccept}
          disabled={accepting}
          className="mt-8 w-full rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-60"
        >
          {accepting ? "Joining…" : `Join ${preview.workspace_name}`}
        </button>
      )}
    </div>
  );
}
