"use client";

import Link from "next/link";
import type { Conversation } from "@/lib/chat-client";

// The "+ New chat" link and the list of conversations a user can see in this workspace — their
// own, plus any teammate's they've been let into. Shared by the draft composer (c/new) and a
// real chat thread (c/[conversationId]) so both look and behave identically.
export function ConversationSidebar({
  slug,
  currentUserId,
  activeConversationId,
  conversations,
  onDelete,
}: {
  slug: string;
  currentUserId: string | undefined;
  activeConversationId: string | null;
  conversations: Conversation[];
  onDelete: (id: string) => void;
}) {
  return (
    <aside className="hidden w-56 shrink-0 sm:block">
      <Link
        href={`/w/${slug}/c/new`}
        className="block rounded-md border border-border px-3 py-2 text-center text-sm text-accent"
      >
        + New chat
      </Link>
      <ul className="mt-4 flex flex-col gap-1">
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
                    onDelete(c.id);
                  }}
                  aria-label={`Delete ${c.title}`}
                  title="Delete conversation"
                  className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-1 text-text-muted opacity-0 hover:text-danger group-hover:opacity-100"
                >
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
                </button>
              )}
            </li>
          );
        })}
      </ul>
    </aside>
  );
}
