"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { listConversations, type Conversation } from "@/lib/chat-client";
import { SidebarAccount } from "@/components/sidebar-account";
import { WorkspaceSwitcher } from "@/components/workspace-switcher";

const PAGE_SIZE = 30;
const SEARCH_DEBOUNCE_MS = 300;
const COLLAPSE_KEY = "recore:sidebar-collapsed";

function SearchIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      className="h-4 w-4"
    >
      <circle cx="8.75" cy="8.75" r="5.25" strokeLinecap="round" strokeLinejoin="round" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M16.25 16.25 12.7 12.7" />
    </svg>
  );
}

// A rectangle with a vertical divider — the conventional "toggle sidebar" glyph.
function PanelIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      className="h-4 w-4"
    >
      <rect x="3" y="4" width="14" height="12" rx="2" strokeLinecap="round" strokeLinejoin="round" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M12.5 4v12" />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      className="h-3.5 w-3.5"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M4.5 5.5h11m-9 0v-1.25A1.25 1.25 0 0 1 7.75 3h4.5a1.25 1.25 0 0 1 1.25 1.25V5.5m1.75 0-.6 9.6A1.5 1.5 0 0 1 13.15 16.5h-6.3a1.5 1.5 0 0 1-1.5-1.4l-.6-9.6"
      />
    </svg>
  );
}

// The list of conversations a user can see in this workspace — their own, plus any teammate's
// they've been let into. Docked on the left, its own independent scroll region, collapsible, and
// searchable. Shared by the draft composer (c/new) and a real chat thread (c/[conversationId]) so
// both look and behave identically.
//
// Owns its own paginated fetch (30 at a time, more loaded as the list scrolls near its bottom)
// rather than receiving the list as a prop — search and infinite scroll both need to re-query the
// API, so there's no simpler "dumb list" shape that still works. `refreshKey` is the one thing a
// parent controls: bump it after an action that changes ordering or membership it already knows
// about (a message was sent, changing this conversation's title or bumping it to the top) to reset
// back to a fresh first page.
export function ConversationSidebar({
  slug,
  workspaceId,
  currentUserId,
  activeConversationId,
  refreshKey,
  onDelete,
}: {
  slug: string;
  workspaceId: string | undefined;
  currentUserId: string | undefined;
  activeConversationId: string | null;
  refreshKey: number;
  onDelete: (id: string) => void;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(true);
  const sentinelRef = useRef<HTMLLIElement>(null);

  // Collapsed state is a per-viewer convenience, remembered across visits — read only after
  // mount (never during the initial render) so a private window or disabled storage just means
  // starting expanded, not a hydration mismatch between server and client markup.
  useEffect(() => {
    // Deferred into a .then() rather than read synchronously in the effect body — setState
    // directly at an effect's top level risks cascading renders (react-hooks/set-state-in-effect).
    Promise.resolve().then(() => {
      try {
        if (localStorage.getItem(COLLAPSE_KEY) === "1") setCollapsed(true);
      } catch {
        // ignore — see above
      }
    });
  }, []);

  function toggleCollapsed() {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(COLLAPSE_KEY, next ? "1" : "0");
      } catch {
        // ignore
      }
      return next;
    });
  }

  // Waits for a pause in typing before actually re-querying, rather than firing a request (and
  // resetting the scrolled list) on every keystroke.
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search.trim()), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [search]);

  // The first page — reset and refetch whenever the search term settles, or the parent bumps
  // refreshKey. Deferred into a .then()/.finally() chain, never called directly at the effect's
  // top level — see workspace-context.tsx for why: calling setState synchronously there risks
  // cascading renders under Next's stricter react-hooks rules.
  useEffect(() => {
    if (!workspaceId) return;
    let cancelled = false;
    listConversations(workspaceId, { limit: PAGE_SIZE, q: debouncedSearch || undefined })
      .then((page) => {
        if (cancelled) return;
        setConversations(page);
        setHasMore(page.length === PAGE_SIZE);
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [workspaceId, debouncedSearch, refreshKey]);

  const loadMore = useCallback(() => {
    if (!workspaceId || loadingMore || !hasMore || conversations.length === 0) return;
    setLoadingMore(true);
    listConversations(workspaceId, {
      limit: PAGE_SIZE,
      before: conversations[conversations.length - 1].updated_at,
      q: debouncedSearch || undefined,
    })
      .then((page) => {
        setConversations((prev) => [...prev, ...page]);
        setHasMore(page.length === PAGE_SIZE);
      })
      .finally(() => setLoadingMore(false));
  }, [workspaceId, loadingMore, hasMore, conversations, debouncedSearch]);

  // Infinite scroll: the next page loads once the sentinel at the bottom of the list scrolls
  // into view, rather than requiring a manual "Load more" click.
  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries[0]?.isIntersecting) loadMore();
    });
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [loadMore]);

  function handleDelete(id: string) {
    // Optimistic: this component owns its own list independently of the parent, which still
    // does the actual API call (and, if this was the open conversation, redirects away from it).
    setConversations((prev) => prev.filter((c) => c.id !== id));
    onDelete(id);
  }

  // Sticky to the very top of the viewport — the chat page has no global nav above it (see
  // site-chrome.tsx), so this sidebar's own top row stands in for it. Its own list still gets an
  // independent scrollbar via the bounded height this creates, without touching the rest of the
  // app's natural page-scroll model at all.
  if (collapsed) {
    return (
      <div className="sticky top-0 hidden h-screen shrink-0 border-r border-border py-3 sm:block">
        <button
          type="button"
          onClick={toggleCollapsed}
          title="Show conversations"
          aria-label="Show conversations"
          className="rounded-md p-1.5 text-text-soft hover:bg-surface-sunk hover:text-text"
        >
          <PanelIcon />
        </button>
      </div>
    );
  }

  return (
    <aside className="sticky top-0 hidden h-screen w-64 shrink-0 flex-col border-r border-border sm:flex">
      <div className="flex h-16 shrink-0 items-center gap-2 border-b border-border px-3">
        <Link href={`/w/${slug}`} className="text-sm font-semibold tracking-tight text-text">
          ReCore
        </Link>
        <WorkspaceSwitcher destination={(newSlug) => `/w/${newSlug}/c/new`} />
      </div>

      <div className="flex shrink-0 items-center gap-2 px-3 py-3">
        {searchOpen ? (
          <input
            autoFocus
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            onBlur={() => {
              if (!search) setSearchOpen(false);
            }}
            placeholder="Search conversations…"
            className="min-w-0 flex-1 rounded-md border border-border bg-surface px-2 py-1.5 text-sm text-text outline-none focus:border-accent"
          />
        ) : (
          <Link
            href={`/w/${slug}/c/new`}
            className="flex-1 rounded-md border border-border px-3 py-1.5 text-center text-sm text-accent"
          >
            + New chat
          </Link>
        )}
        <div className="flex shrink-0 items-center gap-1">
          <button
            type="button"
            onClick={() => setSearchOpen((prev) => !prev)}
            title="Search conversations"
            aria-label="Search conversations"
            className={`rounded-md p-1.5 hover:bg-surface-sunk hover:text-text ${
              searchOpen ? "text-accent" : "text-text-soft"
            }`}
          >
            <SearchIcon />
          </button>
          <button
            type="button"
            onClick={toggleCollapsed}
            title="Hide conversations"
            aria-label="Hide conversations"
            className="rounded-md p-1.5 text-text-soft hover:bg-surface-sunk hover:text-text"
          >
            <PanelIcon />
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {loading ? (
          <p className="px-2 py-2 text-xs text-text-muted">Loading…</p>
        ) : conversations.length === 0 ? (
          <p className="px-2 py-2 text-xs text-text-muted">
            {debouncedSearch ? "No matching conversations." : "No conversations yet."}
          </p>
        ) : (
          <ul className="flex flex-col gap-1">
            {conversations.map((c) => {
              const isMine = c.user_id === currentUserId;
              return (
                <li key={c.id} className="group relative">
                  <Link
                    href={`/w/${slug}/c/${c.id}`}
                    className={`flex items-center gap-1.5 truncate rounded-md py-1.5 pl-2 text-sm ${
                      isMine ? "pr-7" : "pr-2"
                    } ${
                      c.id === activeConversationId
                        ? "bg-surface-sunk text-text"
                        : "text-text-soft hover:bg-surface-sunk"
                    }`}
                  >
                    <span className="truncate">{c.title}</span>
                    {!isMine && (
                      <span className="shrink-0 rounded-full border border-border-strong px-1.5 py-0.5 text-[0.6rem] uppercase tracking-wide text-text-muted">
                        shared
                      </span>
                    )}
                  </Link>
                  {isMine && (
                    <button
                      type="button"
                      onClick={(e) => {
                        e.preventDefault();
                        handleDelete(c.id);
                      }}
                      aria-label={`Delete ${c.title}`}
                      title="Delete conversation"
                      className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-1 text-text-muted opacity-0 hover:text-danger group-hover:opacity-100"
                    >
                      <TrashIcon />
                    </button>
                  )}
                </li>
              );
            })}
            <li ref={sentinelRef} aria-hidden className="h-px" />
            {loadingMore && <p className="px-2 py-2 text-xs text-text-muted">Loading more…</p>}
          </ul>
        )}
      </div>

      <SidebarAccount />
    </aside>
  );
}
