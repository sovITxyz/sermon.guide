"use client";

import type { ChunkItem } from "@/lib/types";
import { memo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";

/**
 * One book chunk rendered as markdown. Deliberately NO rehype-raw: raw HTML
 * embedded in chunk content stays inert (react-markdown emits it as literal
 * text), preserving the repo's zero-dangerouslySetInnerHTML stance. Tailwind
 * preflight strips element defaults, so the common markdown elements get
 * explicit utility classes via the `components` map.
 *
 * - `img` is stubbed to its alt text as a styled <span> — EPUB-internal image
 *   refs never resolve, and no <img> element means no network fetch at all.
 * - links open in a new tab with rel="noopener noreferrer".
 *
 * Memoized on (content, tinted): scroll-driven window growth re-renders the
 * list, but unchanged chunks skip their markdown re-parse.
 */

const markdownComponents: Components = {
  a: ({ node: _node, children, ...props }) => (
    <a
      {...props}
      target="_blank"
      rel="noopener noreferrer"
      className="text-blue-600 hover:underline"
    >
      {children}
    </a>
  ),
  img: ({ node: _node, alt }) => (
    <span className="text-gray-400 text-sm italic">[image{alt ? `: ${alt}` : ""}]</span>
  ),
  h1: ({ node: _node, ...props }) => <h1 {...props} className="mt-8 mb-3 font-semibold text-2xl" />,
  h2: ({ node: _node, ...props }) => <h2 {...props} className="mt-6 mb-3 font-semibold text-xl" />,
  h3: ({ node: _node, ...props }) => <h3 {...props} className="mt-5 mb-2 font-semibold text-lg" />,
  h4: ({ node: _node, ...props }) => <h4 {...props} className="mt-4 mb-2 font-semibold" />,
  h5: ({ node: _node, ...props }) => <h5 {...props} className="mt-4 mb-2 font-medium" />,
  h6: ({ node: _node, ...props }) => <h6 {...props} className="mt-4 mb-2 font-medium" />,
  p: ({ node: _node, ...props }) => <p {...props} className="my-3 leading-relaxed" />,
  ul: ({ node: _node, ...props }) => <ul {...props} className="my-3 list-disc pl-6" />,
  ol: ({ node: _node, ...props }) => <ol {...props} className="my-3 list-decimal pl-6" />,
  li: ({ node: _node, ...props }) => <li {...props} className="my-1 leading-relaxed" />,
  blockquote: ({ node: _node, ...props }) => (
    <blockquote {...props} className="my-3 border-gray-300 border-l-4 pl-3 text-gray-600 italic" />
  ),
  code: ({ node: _node, ...props }) => (
    <code {...props} className="rounded bg-gray-100 px-1 font-mono text-sm" />
  ),
  pre: ({ node: _node, ...props }) => (
    <pre {...props} className="my-3 overflow-x-auto rounded bg-gray-100 p-3 text-sm" />
  ),
  hr: ({ node: _node, ...props }) => <hr {...props} className="my-6 border-gray-200" />,
};

interface ReaderChunkProps {
  chunk: ChunkItem;
  tinted: boolean;
}

function ReaderChunkImpl({ chunk, tinted }: ReaderChunkProps) {
  return (
    <div
      data-chunk-index={chunk.chunk_index}
      className={`scroll-mt-4 rounded px-1 transition-colors duration-700 ${
        tinted ? "bg-blue-50" : "bg-transparent"
      }`}
    >
      <ReactMarkdown components={markdownComponents}>{chunk.content}</ReactMarkdown>
    </div>
  );
}

export const ReaderChunk = memo(ReaderChunkImpl);
