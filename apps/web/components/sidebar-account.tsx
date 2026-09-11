"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useAuth } from "@/lib/auth-context";

// The account chip pinned to the bottom of the chat sidebar — same information AuthStatus shows
// in the global header (Flags link for superusers, the signed-in email, Sign out), just laid out
// to fit a narrow sidebar column instead of a wide horizontal bar: a compact row that opens a
// small menu on click, the same interaction WorkspaceSwitcher's dropdown already uses.
export function SidebarAccount() {
  const { user, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [open]);

  if (!user) return null;

  return (
    <div ref={ref} className="relative border-t border-border p-2">
      {open && (
        <div className="absolute bottom-full left-2 right-2 z-10 mb-1 rounded-md border border-border bg-surface py-1 shadow-lg">
          {user.is_superuser && (
            <Link
              href="/admin/flags"
              onClick={() => setOpen(false)}
              className="block px-3 py-1.5 text-sm text-text-soft hover:bg-surface-sunk hover:text-text"
            >
              Flags
            </Link>
          )}
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              logout();
            }}
            className="block w-full px-3 py-1.5 text-left text-sm text-text-soft hover:bg-surface-sunk hover:text-text"
          >
            Sign out
          </button>
        </div>
      )}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left hover:bg-surface-sunk"
      >
        <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-accent/20 text-xs font-medium text-accent">
          {user.email[0]?.toUpperCase()}
        </span>
        <span className="min-w-0 flex-1 truncate text-xs text-text-soft">{user.email}</span>
      </button>
    </div>
  );
}
