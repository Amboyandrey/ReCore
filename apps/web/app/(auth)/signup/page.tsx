"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { AuthError, signup } from "@/lib/auth-client";

const MIN_PASSWORD_LENGTH = 12;

export default function SignupPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await signup(email, password);
      const next = new URLSearchParams(window.location.search).get("next");
      const target = next ? `/login?created=1&next=${encodeURIComponent(next)}` : "/login?created=1";
      router.push(target);
    } catch (err) {
      setError(err instanceof AuthError ? err.message : "Something went wrong.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mx-auto max-w-sm px-6 py-16">
      <h1 className="text-2xl font-semibold tracking-tight text-text">Create an account</h1>
      <p className="mt-1 text-sm text-text-muted">Start chatting with any LLM you bring a key for.</p>

      <form onSubmit={handleSubmit} className="mt-8 flex flex-col gap-4">
        <label className="flex flex-col gap-1.5 text-sm">
          <span className="text-text-soft">Email</span>
          <input
            type="email"
            required
            autoComplete="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
          />
        </label>
        <label className="flex flex-col gap-1.5 text-sm">
          <span className="text-text-soft">Password</span>
          <input
            type="password"
            required
            minLength={MIN_PASSWORD_LENGTH}
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="rounded-md border border-border bg-surface px-3 py-2 text-text outline-none focus:border-accent"
          />
          <span className="text-xs text-text-muted">At least {MIN_PASSWORD_LENGTH} characters.</span>
        </label>

        {error && (
          <p className="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={submitting}
          className="mt-2 rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-60"
        >
          {submitting ? "Creating account…" : "Create account"}
        </button>
      </form>

      <p className="mt-6 text-sm text-text-muted">
        Already have an account?{" "}
        <Link href="/login" className="text-accent">
          Sign in
        </Link>
      </p>
    </div>
  );
}
