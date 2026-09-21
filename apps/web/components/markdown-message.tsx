"use client";

import { createContext, memo, useContext } from "react";
import type { ReactNode } from "react";
import Markdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";

// The subset of an mdast tree this file walks. react-markdown owns the real types, but they live
// in `mdast`, a transitive dependency we'd rather not import from directly — this is every field
// the break plugin below actually touches.
type MdastNode = {
  type: string;
  value?: string;
  children?: MdastNode[];
};

// `<br>` written as literal text (rather than parsed as inline HTML) — what a model produces
// inside a table cell when GFM leaves no other way to break a line.
const LITERAL_BREAK = /<br\s*\/?>/gi;

// The same pattern without /g — a global regex carries `lastIndex` between `.test()` calls and
// would skip every other match, so matching and splitting use separate objects.
const HAS_LITERAL_BREAK = /<br\s*\/?>/i;

// The tag on its own, used to recognise an inline-HTML node that is *only* a line break.
const BREAK_ONLY = /^\s*<br\s*\/?>\s*$/i;

// Turn a text node containing `<br>` into alternating text and hard-break nodes, so the break
// renders as one instead of being escaped and shown to the reader as `<br>`.
function splitLiteralBreaks(value: string): MdastNode[] {
  const parts = value.split(LITERAL_BREAK);
  const out: MdastNode[] = [];
  parts.forEach((part, index) => {
    if (index > 0) out.push({ type: "break" });
    if (part) out.push({ type: "text", value: part });
  });
  return out;
}

// A remark plugin that promotes `<br>` — and only `<br>` — to a real line break, whether the
// parser saw it as inline HTML or as plain text. Everything else stays untouched, which means
// every other tag the model emits is still escaped and shown as text by react-markdown rather
// than parsed: no raw HTML from a model reply ever reaches the DOM (ARCHITECTURE.md §19, and
// the CSP allows 'unsafe-inline' scripts, so an injection here would be fully exploitable).
function remarkLiteralBreaks() {
  return (tree: MdastNode) => visit(tree);
}

// Depth-first rewrite of a node's children, applied in place.
function visit(node: MdastNode): void {
  if (!node.children) return;
  const next: MdastNode[] = [];
  for (const child of node.children) {
    if (child.type === "html" && BREAK_ONLY.test(child.value ?? "")) {
      next.push({ type: "break" });
      continue;
    }
    if (child.type === "text" && HAS_LITERAL_BREAK.test(child.value ?? "")) {
      next.push(...splitLiteralBreaks(child.value ?? ""));
      continue;
    }
    visit(child);
    next.push(child);
  }
  node.children = next;
}

// Whether the `code` element being rendered sits inside a fenced block. react-markdown gives a
// component no way to see its parent, and a bare ``` fence carries no language class to key off,
// so `pre` announces itself here and the `code` below reads it.
const InCodeBlock = createContext(false);

// Every block-level element gets explicit spacing and design tokens rather than a typography
// plugin, matching how the rest of this UI is written (ARCHITECTURE.md §18).
const components: Components = {
  p: ({ children }) => <p className="mb-3 leading-relaxed last:mb-0">{children}</p>,
  h1: ({ children }) => <h1 className="mb-2 mt-4 text-base font-semibold first:mt-0">{children}</h1>,
  h2: ({ children }) => <h2 className="mb-2 mt-4 text-base font-semibold first:mt-0">{children}</h2>,
  h3: ({ children }) => <h3 className="mb-2 mt-3 text-sm font-semibold first:mt-0">{children}</h3>,
  h4: ({ children }) => <h4 className="mb-2 mt-3 text-sm font-semibold first:mt-0">{children}</h4>,
  h5: ({ children }) => <h5 className="mb-1 mt-3 text-sm font-semibold first:mt-0">{children}</h5>,
  h6: ({ children }) => <h6 className="mb-1 mt-3 text-sm font-semibold first:mt-0">{children}</h6>,
  strong: ({ children }) => <strong className="font-semibold text-text">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  ul: ({ children }) => <ul className="mb-3 list-disc space-y-1 pl-5 last:mb-0">{children}</ul>,
  ol: ({ children }) => <ol className="mb-3 list-decimal space-y-1 pl-5 last:mb-0">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed [&>p]:mb-1">{children}</li>,
  hr: () => <hr className="my-4 border-border" />,
  blockquote: ({ children }) => (
    <blockquote className="mb-3 border-l-2 border-border-strong pl-3 text-text-soft last:mb-0">
      {children}
    </blockquote>
  ),

  // Opened in a new tab with `noreferrer`: these URLs come from a model, so the page it lands on
  // learns nothing about where the reader came from and can't reach back through `window.opener`.
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-accent underline underline-offset-2 hover:opacity-80"
    >
      {children}
    </a>
  ),

  // Shown as a link rather than an <img>: the CSP's `img-src 'self' data:` blocks every remote
  // image anyway, so rendering one would only ever produce a broken-image icon.
  img: ({ src, alt }) => (
    <a
      href={typeof src === "string" ? src : undefined}
      target="_blank"
      rel="noopener noreferrer"
      className="text-accent underline underline-offset-2 hover:opacity-80"
    >
      {alt || "image"}
    </a>
  ),

  // A wide table scrolls sideways instead of squeezing its columns into unreadable slivers —
  // the whole reason an assistant message is rendered full-width rather than as a bubble.
  table: ({ children }) => (
    <div className="mb-3 overflow-x-auto last:mb-0">
      <table className="w-full border-collapse text-left text-sm">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="bg-surface-sunk">{children}</thead>,
  th: ({ children }) => (
    <th className="border border-border px-2 py-1 align-top font-semibold">{children}</th>
  ),
  td: ({ children }) => <td className="border border-border px-2 py-1 align-top">{children}</td>,

  pre: ({ children }) => (
    <InCodeBlock.Provider value={true}>
      <pre className="mb-3 overflow-x-auto rounded-md bg-surface-sunk p-3 text-xs last:mb-0">
        {children}
      </pre>
    </InCodeBlock.Provider>
  ),
  code: ({ children }) => <CodeSpan>{children}</CodeSpan>,
};

// Inline code gets its own chip; code already inside a fenced block inherits the <pre>'s styling
// and must not be boxed a second time.
function CodeSpan({ children }: { children?: ReactNode }) {
  const inBlock = useContext(InCodeBlock);
  if (inBlock) return <code className="font-mono">{children}</code>;
  return (
    <code className="rounded bg-surface-sunk px-1 py-0.5 font-mono text-[0.85em]">{children}</code>
  );
}

// Model output rendered as GitHub-flavoured Markdown. Memoised on `content` because a streaming
// reply re-renders on every chunk that arrives, and each render re-parses the whole document.
export const MarkdownMessage = memo(function MarkdownMessage({
  content,
  className,
}: {
  content: string;
  className?: string;
}) {
  return (
    <div className={className}>
      <Markdown remarkPlugins={[remarkGfm, remarkLiteralBreaks]} components={components}>
        {content}
      </Markdown>
    </div>
  );
});
