"use client";

import { useSelection } from "@/components/library/selection-context";
import { readHref } from "@/lib/library";
import { displaySection, formatElapsed, searchQueryProblem, segmentSummary } from "@/lib/summary";
import type { SummaryRequest, SummaryResponse } from "@/lib/types";
import Link from "next/link";
import { type FormEvent, useEffect, useRef, useState } from "react";

// Cards clamp the passage to a few lines; only passages long enough to
// plausibly be clamped get a Show more toggle (heuristic — the clamp itself
// is line-based).
const PREVIEW_TOGGLE_CHARS = 280;

/**
 * `totalBooks` (the JWT user's library size, server-fetched on the /search page)
 * backs the unscoped "Searching all N books" label. It is optional so the
 * component still renders bare in tests / outside the page.
 *
 * Phase 51 hydration: `hydratedResult` is a saved `SummaryResponse` pushed in
 * when the user reopens an entry from the "Recent" panel. When its identity
 * CHANGES to a non-null value the panel adopts it as the rendered result — the
 * SAME summary/citation render the live search uses — with NO second
 * /search-summary call (the costly 2–4 min pipeline is never re-run). A fresh
 * live search later still wins because it sets `result` directly; the hydration
 * effect only fires when the prop reference changes (RecentSearches passes a
 * fresh object per reopen). `onSearched` lets the parent refresh the Recent list
 * after a successful live search so the new row appears.
 */
export function SearchPanel({
  totalBooks,
  hydratedResult,
  onSearched,
}: {
  totalBooks?: number;
  hydratedResult?: SummaryResponse | null;
  onSearched?: () => void;
}) {
  // The shared library selection (Phase 49). Empty (no provider, or nothing
  // ticked) => whole library: the scope fields are omitted from the POST.
  const { bookIds, collectionIds, resolved } = useSelection();
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [result, setResult] = useState<SummaryResponse | null>(null);
  const [expanded, setExpanded] = useState<ReadonlySet<number>>(new Set());

  // Same guard as Uploader: a search outlives navigation (~2 min round-trip),
  // so every await re-checks mounted before touching state.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // Reopen a saved search from the "Recent" panel (Phase 51): adopt the pushed
  // result into the existing render. Fires only when the prop identity changes
  // to a non-null value, so it never clobbers a live search's result (whose
  // object reference it does not share). Clears any in-flight/error state.
  useEffect(() => {
    if (hydratedResult) {
      setResult(hydratedResult);
      setSearching(false);
      setError(null);
      setExpanded(new Set());
    }
  }, [hydratedResult]);

  // Elapsed ticker for the long-request affordance; the interval lives only
  // while a search is in flight.
  useEffect(() => {
    if (!searching) {
      return;
    }
    setElapsed(0);
    const id = window.setInterval(() => setElapsed((s) => s + 1), 1000);
    return () => window.clearInterval(id);
  }, [searching]);

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
    setResult(null);
    setExpanded(new Set());
    try {
      // Fold the shared selection into the POST scope: the RAW selection (ad-hoc
      // book_ids + whole collection_ids) so the API re-resolves collections to
      // their current membership + ownership and intersects with the library.
      // Empty arrays are OMITTED (= whole library).
      const body: SummaryRequest = { query: q };
      if (bookIds.length > 0) {
        body.book_ids = bookIds;
      }
      if (collectionIds.length > 0) {
        body.collection_ids = collectionIds;
      }
      const res = await fetch("/api/search-summary", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
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
      const data = (await res.json()) as SummaryResponse;
      if (!mounted.current) {
        return;
      }
      setResult(data);
      setSearching(false);
      // Tell the parent a search just landed so it can refresh the Recent list
      // (the API saved this run server-side) — the new row appears without a
      // full reload. Best-effort: never block the render on it.
      onSearched?.();
    } catch {
      if (mounted.current) {
        setError("Network error. Please try again.");
        setSearching(false);
      }
    }
  }

  function toggleExpanded(index: number): void {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(index)) {
        next.delete(index);
      } else {
        next.add(index);
      }
      return next;
    });
  }

  const segments = result ? segmentSummary(result.summary, result.citations) : [];

  return (
    <div className="space-y-6">
      <form onSubmit={onSubmit} noValidate className="flex gap-2">
        <label htmlFor="search-query" className="sr-only">
          Question
        </label>
        <input
          id="search-query"
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="What does this say about grace?"
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

      {/* Scope hint. A plain <p> (NOT role=status) so it never collides with the
          in-flight searching <output>. */}
      <p className="text-gray-500 text-xs" data-testid="search-scope">
        {resolved.length > 0
          ? `Searching ${resolved.length} selected ${resolved.length === 1 ? "book" : "books"}.`
          : totalBooks === undefined
            ? "Searching your whole library."
            : `Searching all ${totalBooks} ${totalBooks === 1 ? "book" : "books"} in your library.`}
      </p>

      {error ? (
        <p role="alert" className="text-red-600 text-sm">
          {error}
        </p>
      ) : null}

      {searching ? (
        <output className="block rounded-lg border border-blue-200 bg-blue-50 p-4 text-sm">
          <p className="font-medium text-blue-900">
            Searching your library… {formatElapsed(elapsed)}
          </p>
          <p className="mt-1 text-blue-700">
            Retrieval, reranking, and summary generation usually take a couple of minutes.
          </p>
        </output>
      ) : null}

      {result && result.citations.length === 0 ? (
        <div className="rounded-lg border border-gray-300 border-dashed p-8 text-center text-gray-600 text-sm">
          {result.summary}
        </div>
      ) : null}

      {result && result.citations.length > 0 ? (
        <div className="space-y-6">
          <div className="rounded-lg border border-gray-200 p-6 shadow-sm">
            <h2 className="mb-3 font-semibold text-lg">Summary</h2>
            <p className="whitespace-pre-wrap text-sm leading-relaxed">
              {segments.map((seg) =>
                seg.kind === "text" ? (
                  <span key={`t${seg.start}`}>{seg.text}</span>
                ) : (
                  <a
                    key={`m${seg.start}`}
                    href={`#citation-${seg.citationIndex + 1}`}
                    title={seg.marker}
                    className="mx-0.5 rounded bg-blue-50 px-1 font-medium text-blue-700 text-xs hover:underline"
                  >
                    [{seg.citationIndex + 1}]
                  </a>
                ),
              )}
            </p>
          </div>

          <div>
            <h2 className="mb-3 font-semibold text-lg">Sources</h2>
            <ol className="space-y-3">
              {result.citations.map((citation, index) => {
                const section = displaySection(citation.parent_section);
                return (
                  <li
                    key={citation.marker}
                    id={`citation-${index + 1}`}
                    className="rounded-lg border border-gray-200 p-4 target:border-blue-300"
                  >
                    <div className="mb-2 flex flex-wrap items-baseline gap-x-2 gap-y-1">
                      <span className="font-medium text-blue-700 text-xs">[{index + 1}]</span>
                      <span className="font-medium text-sm">{citation.title}</span>
                      <span className="text-gray-500 text-xs">
                        {section ? `${section} · ` : ""}
                        chunk {citation.chunk_index}
                      </span>
                      <Link
                        href={readHref(citation.book_id, citation.chunk_index)}
                        className="text-blue-600 text-xs hover:underline"
                      >
                        Read in context
                      </Link>
                    </div>
                    <p
                      className={`whitespace-pre-wrap text-gray-600 text-sm ${
                        expanded.has(index) ? "" : "line-clamp-4"
                      }`}
                    >
                      {citation.content}
                    </p>
                    {citation.content.length > PREVIEW_TOGGLE_CHARS ? (
                      <button
                        type="button"
                        onClick={() => toggleExpanded(index)}
                        className="mt-2 text-blue-600 text-xs hover:underline"
                      >
                        {expanded.has(index) ? "Show less" : "Show more"}
                      </button>
                    ) : null}
                  </li>
                );
              })}
            </ol>
          </div>
        </div>
      ) : null}
    </div>
  );
}
