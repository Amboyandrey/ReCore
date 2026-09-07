"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { useWorkspaces } from "@/lib/workspace-context";

// Lets the signed-in user see which workspace they're in, jump to another, or create one.
export function WorkspaceSwitcher() {
  const { user } = useAuth();
  const { workspaces, loading } = useWorkspaces();
  const pathname = usePathname();
  const router = useRouter();
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
  if (loading) return <span className="h-8 w-32 animate-pulse rounded-md bg-surface-sunk" />;

  const currentSlug = pathname.match(/^\/w\/([^/]+)/)?.[1];
  const current = workspaces.find((w) => w.slug === currentSlug);

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-sm text-text-soft hover:border-border-strong"
      >
        {current ? current.name : "Select workspace"}
        <span className="text-text-muted">▾</span>
      </button>
      {open && (
        <div className="absolute left-0 top-full z-10 mt-1 w-56 rounded-md border border-border bg-surface py-1 shadow-lg">
          {workspaces.length === 0 && (
            <p className="px-3 py-2 text-sm text-text-muted">No workspaces yet</p>
          )}
          {workspaces.map((w) => (
            <button
              key={w.id}
              type="button"
              onClick={() => {
                setOpen(false);
                router.push(`/w/${w.slug}`);
              }}
              className="flex w-full items-center justify-between px-3 py-1.5 text-left text-sm text-text hover:bg-surface-sunk"
            >
              <span>{w.name}</span>
              <span className="font-mono text-xs text-text-muted">{w.role}</span>
            </button>
          ))}
          <div className="my-1 border-t border-border" />
          <Link
            href="/workspaces/new"
            onClick={() => setOpen(false)}
            className="block px-3 py-1.5 text-sm text-accent hover:bg-surface-sunk"
          >
            + New workspace
          </Link>
        </div>
      )}
    </div>
  );
}
