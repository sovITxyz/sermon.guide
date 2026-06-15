"use client";

import { displaySection, searchQueryProblem } from "@/lib/summary";
import type { SearchHit, SearchResponse } from "@/lib/types";
import type { Editor } from "@tiptap/react";
import { type FormEvent, useEffect, useRef, useState } from "react";
import type { CitationAttrs } from "./CitationNode";

/**
 * In-editor LibraryDrawer (Phase 37, B2 slice D). A panel that searches the
 * user's library and inserts a cited passage as a first-class `citation` block
 * into the manuscript.
 *
 * It reuses the SearchPanel plumbing (same-origin POST, client validation, the
 * mounted guard) but hits the NEW thin `/api/search` proxy — RAW hybrid hits,
 * NO LLM round-trip — so it is FAST. There is no minutes-long elapsed ticker
 * here (that affordance is for the /search-summary generative path); a plain
 * inline spinner label is enough.
 *
 * MAPPING THE GAP: a raw /search hit (lib/types.ts:SearchHit) carries NO title
 * and no field literally named `snippet`. So a hit -> citation attrs mapping:
 *   bookId        <- hit.book_id
 *   chunkIndex    <- hit.metadata.chunk_index
 *   parentSection <- hit.metadata.parent_section
 *   snippet       <- hit.content_chunk   (CACHED at insert — self-contained)
 *   bookTitle     <- the one-shot /library {book_id -> title} map (the shell's
 *                    single fetch); a book missing from the map falls back to a
 *                    neutral label so the card still renders.
 *
 * INSERT: clicking a hit runs editor.chain().focus().insertContent({type:
 * "citation", attrs}).run(). That fires the editor's existing `update` event,
 * which the Phase 36 autosave already debounces + single-flights — NO autosave
 * change is needed, and the inserted node is part of editor.getJSON() so it
 * survives save -> reload (CitationNode owns the round-trip).
 *
 * SECURITY: the drawer renders hit titles + snippet previews as PLAIN TEXT
 * (JSX children) — ZERO dangerouslySetInnerHTML, mirroring the citation card.
 */

const UNTITLED_BOOK = "Untitled book";

/** A book that left the library since insert has no title in the map. */
function titleFor(bookId: string, bookTitles: ReadonlyMap<string, string>): string {
  return bookTitles.get(bookId) ?? UNTITLED_BOOK;
}

/** Map a raw /search hit to the citation node attrs, caching title + snippet. */
function hitToCitationAttrs(
  hit: SearchHit,
  bookTitles: ReadonlyMap<string, string>,
): CitationAttrs {
  return {
    bookId: hit.book_id,
    chunkIndex: hit.metadata.chunk_index,
    bookTitle: titleFor(hit.book_id, bookTitles),
    snippet: hit.content_chunk,
    parentSection: hit.metadata.parent_section,
  };
}

export function LibraryDrawer({
  editor,
  bookTitles,
  onClose,
}: {
  editor: Editor | null;
  // The shell's one-shot {book_id -> title} map — the only source of a hit's
  // title (raw /search hits carry none). Read-only; the drawer never fetches it.
  bookTitles: ReadonlyMap<string, string>;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);
  const [hits, setHits] = useState<SearchHit[] | null>(null);

  // A search can outlive a teardown (drawer close / navigation); re-check before
  // touching state after every await, the SearchPanel/Uploader pattern.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  async function onSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setError(null);
    const q = query.trim();
    const problem = searchQueryProblem(q);
    if (problem) {
      setError(problem);
      return;
    }
    setSearching(true);
    setHits(null);
    try {
      const res = await fetch("/api/search", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ query: q }),
      });
      if (!mounted.current) {
        return;
      }
      if (!res.ok) {
        const data = (await res.json().catch(() => null)) as { error?: string } | null;
        setError(data?.error ?? "Something went wrong. Please try again.");
        setSearching(false);
        return;
      }
      const data = (await res.json()) as SearchResponse;
      if (!mounted.current) {
        return;
      }
      setHits(data.hits);
      setSearching(false);
    } catch {
      if (mounted.current) {
        setError("Network error. Please try again.");
        setSearching(false);
      }
    }
  }

  function insertHit(hit: SearchHit): void {
    if (!editor) {
      return;
    }
    const attrs = hitToCitationAttrs(hit, bookTitles);
    // insertContent fires the editor `update` event -> the existing Phase 36
    // autosave debounces + single-flights it; the node is in getJSON() so it
    // survives save -> reload. No autosave wiring is needed here.
    editor.chain().focus().insertContent({ type: "citation", attrs }).run();
  }

  return (
    <aside
      aria-label="Insert a citation from your library"
      className="mb-3 rounded-lg border border-gray-300 bg-gray-50 p-4"
    >
      <div className="mb-3 flex items-baseline justify-between gap-4">
        <h2 className="font-semibold text-sm">Cite from your library</h2>
        <button
          type="button"
          onClick={onClose}
          className="shrink-0 text-blue-600 text-xs hover:underline"
        >
          Close
        </button>
      </div>

      <form onSubmit={onSubmit} noValidate className="flex gap-2">
        <label htmlFor="library-drawer-query" className="sr-only">
          Search your library
        </label>
        <input
          id="library-drawer-query"
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search your library to cite a passage"
          disabled={searching}
          className="w-full rounded border border-gray-300 px-3 py-2 text-sm focus:border-black focus:outline-none disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={searching}
          className="shrink-0 rounded bg-black px-3 py-2 font-medium text-sm text-white disabled:opacity-50"
        >
          {searching ? "Searching…" : "Search"}
        </button>
      </form>

      {error ? (
        <p role="alert" className="mt-3 text-red-600 text-sm">
          {error}
        </p>
      ) : null}

      {searching ? (
        <output className="mt-3 block text-gray-600 text-sm">Searching your library…</output>
      ) : null}

      {hits && hits.length === 0 ? (
        <p className="mt-3 rounded-lg border border-gray-300 border-dashed p-4 text-center text-gray-600 text-sm">
          No passages found in your library for that query.
        </p>
      ) : null}

      {hits && hits.length > 0 ? (
        <ul className="mt-3 space-y-2">
          {hits.map((hit) => {
            const section = displaySection(hit.metadata.parent_section);
            return (
              <li key={`${hit.book_id}:${hit.metadata.chunk_index}`}>
                <button
                  type="button"
                  onClick={() => insertHit(hit)}
                  data-testid="library-drawer-hit"
                  className="block w-full rounded-lg border border-gray-200 bg-white p-3 text-left hover:border-blue-300 focus:border-blue-300 focus:outline-none"
                >
                  <div className="mb-1 flex flex-wrap items-baseline gap-x-2 gap-y-1">
                    <span className="font-medium text-sm">{titleFor(hit.book_id, bookTitles)}</span>
                    <span className="text-gray-500 text-xs">
                      {section ? `${section} · ` : ""}
                      chunk {hit.metadata.chunk_index}
                    </span>
                    <span className="text-blue-600 text-xs">Insert citation</span>
                  </div>
                  <p className="line-clamp-2 whitespace-pre-wrap text-gray-600 text-sm">
                    {hit.content_chunk}
                  </p>
                </button>
              </li>
            );
          })}
        </ul>
      ) : null}
    </aside>
  );
}
