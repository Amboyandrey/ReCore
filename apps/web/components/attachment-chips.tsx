"use client";

import type { Attachment } from "@/lib/attachment-client";

// What each attachment's chip shows next to its filename — silence here is exactly how three
// separate attachment bugs went unnoticed for as long as they did (see ARCHITECTURE.md history).
export const EXTRACT_STATUS_LABEL: Record<Attachment["extract_status"], string> = {
  pending: "processing…",
  done: "text extracted",
  passthrough: "sent as image",
  unsupported: "format not supported",
  failed: "couldn't be read",
};

// Whether any of these (not-yet-sent) attachments would be silently dropped by the current
// model — an image, and the model doesn't accept images.
export function hasBlockedImage(attachments: Attachment[], modelSupportsVision: boolean): boolean {
  return !modelSupportsVision && attachments.some((a) => a.extract_status === "passthrough");
}

// The composer's row of not-yet-sent attachment chips — removable, and flagged red the moment
// one would be blocked (an image on a non-vision model) or already failed/unsupported, so that's
// visible before Send is even pressed rather than discovered from the model's reply.
export function PendingAttachmentChips({
  attachments,
  modelSupportsVision,
  onRemove,
}: {
  attachments: Attachment[];
  modelSupportsVision: boolean;
  onRemove: (id: string) => void;
}) {
  if (attachments.length === 0) return null;
  return (
    <ul className="mt-4 flex flex-wrap gap-2">
      {attachments.map((a) => {
        const blocked = a.extract_status === "passthrough" && !modelSupportsVision;
        return (
          <li
            key={a.id}
            className={`flex items-center gap-2 rounded-full border px-3 py-1 text-xs ${
              blocked || a.extract_status === "failed" || a.extract_status === "unsupported"
                ? "border-danger/40 bg-danger/10 text-danger"
                : "border-border bg-surface text-text-soft"
            }`}
          >
            <span className="max-w-[12rem] truncate">{a.original_filename}</span>
            <span className="text-[0.65rem] uppercase tracking-wide opacity-80">
              {blocked ? "model can't read images" : EXTRACT_STATUS_LABEL[a.extract_status]}
            </span>
            <button
              type="button"
              onClick={() => onRemove(a.id)}
              className="text-text-muted hover:text-danger"
              aria-label={`Remove ${a.original_filename}`}
            >
              ×
            </button>
          </li>
        );
      })}
    </ul>
  );
}

// Read-only chips shown under an already-sent message — what makes an attachment's presence (and
// whether the model actually got to read it) visible in history, not just in the composer at the
// moment it was sent.
export function SentAttachmentChips({ attachments }: { attachments: Attachment[] }) {
  if (attachments.length === 0) return null;
  return (
    <div className="mt-1.5 flex flex-wrap justify-end gap-1.5">
      {attachments.map((a) => (
        <span
          key={a.id}
          className="flex items-center gap-1 rounded-full border border-border-strong/60 bg-surface-sunk px-2 py-0.5 text-[0.7rem] text-text-muted"
        >
          <span className="max-w-[10rem] truncate">📎 {a.original_filename}</span>
          <span className="opacity-70">· {EXTRACT_STATUS_LABEL[a.extract_status]}</span>
        </span>
      ))}
    </div>
  );
}
