"use client";

import { useEffect, useState } from "react";
import type { LiveSource } from "@/lib/chat-client";
import type { MessageSource } from "@/lib/knowledge-client";

const COLLAPSE_KEY = "recore:sources-collapsed";

// A rectangle with a vertical divider — the conventional "toggle sidebar" glyph, same shape
// conversation-sidebar.tsx's own PanelIcon uses (each sidebar keeps its own copy rather than
// sharing one, matching this codebase's existing per-component icon convention).
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
      <path strokeLinecap="round" strokeLinejoin="round" d="M7.5 4v12" />
    </svg>
  );
}

// One group of sources: either the reply currently streaming in (no persisted id yet) or an
// already-persisted assistant message.
type SourceGroup = { key: string; sources: (LiveSource | MessageSource)[] };

function SourceItem({ source }: { source: LiveSource | MessageSource }) {
  return (
    <li className="rounded-md border border-border bg-surface px-2.5 py-2 text-xs">
      {source.url ? (
        <a
          href={source.url}
          target="_blank"
          rel="noreferrer"
          className="font-medium text-accent hover:underline"
        >
          {source.label}
        </a>
      ) : (
        <span className="font-medium text-text">{source.label}</span>
      )}
      <p className="mt-1 line-clamp-3 text-text-muted">{source.snippet}</p>
      <p className="mt-1 truncate text-[0.65rem] uppercase tracking-wide text-text-muted">
        {"connector_name" in source ? source.connector_name : null}
      </p>
    </li>
  );
}

// The right-hand sidebar listing the knowledge sources folded into each reply in a conversation —
// what ReStore's retrieval actually used, grouped by message with the live (currently streaming)
// group on top and every persisted group below it, newest first. Hidden entirely when there's
// nothing to show, so a conversation with knowledge off (or simply no matches yet) looks no
// different from before this feature existed.
export function SourcesSidebar({
  assistantMessageIdsNewestFirst,
  sourcesByMessageId,
  liveSources,
}: {
  // Newest-first order of assistant message ids this conversation has — used only to order the
  // persisted groups below the live one; a message with no sources contributes no group.
  assistantMessageIdsNewestFirst: string[];
  sourcesByMessageId: Record<string, MessageSource[]>;
  liveSources: LiveSource[];
}) {
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    // Deferred into a .then() rather than read synchronously in the effect body — see
    // conversation-sidebar.tsx / workspace-context.tsx for why.
    Promise.resolve().then(() => {
      try {
        if (localStorage.getItem(COLLAPSE_KEY) === "1") setCollapsed(true);
      } catch {
        // ignore — a private window or disabled storage just means starting expanded
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

  const groups: SourceGroup[] = [];
  if (liveSources.length > 0) groups.push({ key: "live", sources: liveSources });
  for (const messageId of assistantMessageIdsNewestFirst) {
    const sources = sourcesByMessageId[messageId];
    if (sources && sources.length > 0) groups.push({ key: messageId, sources });
  }

  if (groups.length === 0) return null;

  if (collapsed) {
    return (
      <div className="sticky top-0 hidden h-screen shrink-0 border-l border-border py-3 sm:block">
        <button
          type="button"
          onClick={toggleCollapsed}
          title="Show sources"
          aria-label="Show sources"
          className="rounded-md p-1.5 text-text-soft hover:bg-surface-sunk hover:text-text"
        >
          <PanelIcon />
        </button>
      </div>
    );
  }

  return (
    <aside className="sticky top-0 hidden h-screen w-72 shrink-0 flex-col overflow-y-auto border-l border-border sm:flex">
      <div className="flex h-16 shrink-0 items-center justify-between border-b border-border px-3">
        <span className="text-sm font-semibold tracking-tight text-text">Sources</span>
        <button
          type="button"
          onClick={toggleCollapsed}
          title="Hide sources"
          aria-label="Hide sources"
          className="rounded-md p-1.5 text-text-soft hover:bg-surface-sunk hover:text-text"
        >
          <PanelIcon />
        </button>
      </div>
      <div className="flex flex-col gap-4 px-3 py-3">
        {groups.map((group) => (
          <ul key={group.key} className="flex flex-col gap-1.5">
            {group.sources.map((source, index) => (
              <SourceItem key={index} source={source} />
            ))}
          </ul>
        ))}
      </div>
    </aside>
  );
}
