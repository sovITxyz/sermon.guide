import type { SummaryCitation } from "./types";

/**
 * Pure helpers for the /search summary page: query validation, splitting the
 * summary prose into text/citation segments, and the elapsed-time label for
 * the long-request affordance. No DOM — unit-tested in test/summary.test.ts.
 */

/** Mirrors the API's `SummaryRequest.query` max_length (api/summary.py). */
export const MAX_QUERY_LENGTH = 1024;

/** Returns a user-facing problem string, or null when the query is sendable. */
export function searchQueryProblem(query: string): string | null {
  if (query.trim().length === 0) {
    return "Enter a question to search for.";
  }
  if (query.length > MAX_QUERY_LENGTH) {
    return `Questions are limited to ${MAX_QUERY_LENGTH} characters.`;
  }
  return null;
}

export type SummarySegment =
  | { kind: "text"; start: number; text: string }
  | { kind: "marker"; start: number; marker: string; citationIndex: number };

/**
 * Split the summary prose into plain-text and citation-marker segments so the
 * UI can render resolvable markers as links to their citation cards.
 *
 * Only the exact markers of returned citations resolve. Anything else stays
 * plain text — in particular the comma-merged brackets the model sometimes
 * emits (`[Book A:70, Book A:51]`, Phase 14b live finding) and markers it
 * invents or paraphrases. Markers are bracket-delimited and contain no inner
 * `[`/`]`/`:`-broken structure, so exact occurrences of distinct markers can
 * never overlap (`[X:1]` cannot match inside `[X:12]`).
 *
 * `start` is the segment's offset in the original summary — a stable React
 * key and an ordering guarantee for the round-trip (concatenating segments
 * reproduces the input).
 */
export function segmentSummary(
  summary: string,
  citations: readonly Pick<SummaryCitation, "marker">[],
): SummarySegment[] {
  const occurrences: { start: number; end: number; marker: string; citationIndex: number }[] = [];
  citations.forEach((citation, citationIndex) => {
    let from = 0;
    while (from < summary.length) {
      const start = summary.indexOf(citation.marker, from);
      if (start === -1) {
        break;
      }
      const end = start + citation.marker.length;
      occurrences.push({ start, end, marker: citation.marker, citationIndex });
      from = end;
    }
  });
  occurrences.sort((a, b) => a.start - b.start);

  const segments: SummarySegment[] = [];
  let pos = 0;
  for (const occ of occurrences) {
    if (occ.start < pos) {
      // Unreachable for distinct bracket-delimited markers; guards against a
      // malformed citation list producing overlapping (double-counted) spans.
      continue;
    }
    if (occ.start > pos) {
      segments.push({ kind: "text", start: pos, text: summary.slice(pos, occ.start) });
    }
    segments.push({
      kind: "marker",
      start: occ.start,
      marker: occ.marker,
      citationIndex: occ.citationIndex,
    });
    pos = occ.end;
  }
  if (pos < summary.length) {
    segments.push({ kind: "text", start: pos, text: summary.slice(pos) });
  }
  return segments;
}

/** `134` → `"2:14"` — the elapsed label shown while a search is in flight. */
export function formatElapsed(totalSeconds: number): string {
  const whole = Math.max(0, Math.floor(totalSeconds));
  const minutes = Math.floor(whole / 60);
  const seconds = whole % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}
