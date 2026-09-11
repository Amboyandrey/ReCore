"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { AuthStatus } from "@/components/auth-status";
import { PendingInvitationsBanner } from "@/components/pending-invitations-banner";
import { WorkspaceSwitcher } from "@/components/workspace-switcher";

// A chat page (c/new or c/[conversationId]) builds its own full-bleed, ChatGPT-style chrome —
// the "ReCore | workspace ▾" pairing and the account chip move into its own sidebar, see
// conversation-sidebar.tsx — so the global header, invitations banner, and footer below would
// only be redundant, wasted vertical space there. Every other route keeps them exactly as before.
function isChatRoute(pathname: string): boolean {
  return /^\/w\/[^/]+\/c(\/|$)/.test(pathname);
}

export function SiteChrome({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const chat = isChatRoute(pathname ?? "");

  if (chat) {
    return <main className="flex-1">{children}</main>;
  }

  // "ReCore" is home base: back to this workspace if the current route is inside one, otherwise
  // the app's own landing page — the same rule the sidebar's own "ReCore" link follows.
  const currentSlug = (pathname ?? "").match(/^\/w\/([^/]+)/)?.[1];
  const homeHref = currentSlug ? `/w/${currentSlug}` : "/";

  return (
    <>
      <header className="sticky top-0 z-50 border-b border-border bg-surface">
        <div className="mx-auto flex h-16 max-w-5xl items-center justify-between px-6">
          <div className="flex items-center gap-4">
            <Link href={homeHref} className="text-lg font-semibold tracking-tight text-text">
              ReCore
            </Link>
            <WorkspaceSwitcher />
          </div>
          <AuthStatus />
        </div>
      </header>
      <PendingInvitationsBanner />
      <main className="flex-1">{children}</main>
      <footer className="border-t border-border">
        <div className="mx-auto max-w-5xl px-6 py-4 font-mono text-xs text-text-muted">
          ReCore &middot; multi-workspace LLM chat platform
        </div>
      </footer>
    </>
  );
}
