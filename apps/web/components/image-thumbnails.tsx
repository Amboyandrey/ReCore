"use client";

import { attachmentContentUrl } from "@/lib/attachment-client";

// A row of stored images, each opening full size in a new tab — tool output or an uploaded image.
export function ImageThumbnails({
  workspaceId,
  images,
  align = "start",
}: {
  workspaceId: string;
  images: { id: string; alt: string }[];
  align?: "start" | "end";
}) {
  if (images.length === 0) return null;
  return (
    <div className={`flex flex-wrap gap-2 ${align === "end" ? "justify-end" : ""}`}>
      {images.map((image) => {
        const src = attachmentContentUrl(workspaceId, image.id);
        return (
          <a key={image.id} href={src} target="_blank" rel="noopener noreferrer">
            {/* next/image can't be used: its optimizer fetches server-side, without the viewer's session. */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={src}
              alt={image.alt}
              loading="lazy"
              className="max-h-64 max-w-xs rounded-md border border-border object-contain"
            />
          </a>
        );
      })}
    </div>
  );
}
