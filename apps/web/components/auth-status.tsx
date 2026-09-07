"use client";

import Link from "next/link";
import { useAuth } from "@/lib/auth-context";

// Shows who's signed in and a way to sign out, or links to sign in/up if no one is.
export function AuthStatus() {
  const { user, loading, logout } = useAuth();

  if (loading) return <span className="h-4 w-24 animate-pulse rounded bg-surface-sunk" />;

  if (!user) {
    return (
      <div className="flex items-center gap-4 text-sm">
        <Link href="/login" className="text-text-soft hover:text-text">
          Sign in
        </Link>
        <Link
          href="/signup"
          className="rounded-md bg-accent px-3 py-1.5 font-medium text-accent-contrast"
        >
          Sign up
        </Link>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-4 text-sm">
      <span className="text-text-soft">{user.email}</span>
      <button
        onClick={() => logout()}
        className="text-text-muted hover:text-text"
        type="button"
      >
        Sign out
      </button>
    </div>
  );
}
