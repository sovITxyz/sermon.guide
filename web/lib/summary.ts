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
 * One span of the summary covered by a citation marker: a standalone marker
 * (`[A:1]`) covers `[start, end)` and emits one chip, while a comma-merged
 * bracket (`[A:70, A:51]`, Phase 24) covers the whole bracket and emits one
 * chip per resolvable member — each at a distinct `start` inside the bracket
 * so React keys stay unique and chip order follows the in-bracket order.
 */
type MarkerSpan = {
  start: number;
  end: number;
  markers: { start: number; marker: string; citationIndex: number }[];
};

/** A `[A:70]` citation marker split into its book label and chunk index. */
function parseMarker(marker: string): { label: string; index: string } | null {
  if (!marker.startsWith("[") || !marker.endsWith("]")) {
    return null;
  }
  const inner = marker.slice(1, -1);
  const colon = inner.lastIndexOf(":");
  if (colon === -1) {
    return null;
  }
  return { label: inner.slice(0, colon), index: inner.slice(colon + 1) };
}

/**
 * Resolve every comma-separated member of a merged bracket against the
 * citation set. Returns one entry per member that resolves to a citation, in
 * member order, each with its offset inside the original summary; invented or
 * paraphrased members are skipped (never fabricated into a chip). Returns null
 * when the bracket is not a real merge of two-plus resolvable members, so the
 * caller can leave the bracket as prose.
 *
 * `bracketStart` is the bracket's `[` offset in the summary; `inner` is the
 * text between the brackets. Member offsets are recovered by scanning `inner`
 * with a forward cursor, so repeated members (`[A:1, A:1]`) get distinct
 * offsets in document order.
 */
function resolveMergedBracket(
  bracketStart: number,
  inner: string,
  markerToIndex: Map<string, number>,
): { start: number; marker: string; citationIndex: number }[] | null {
  const rawMembers = inner.split(",");
  if (rawMembers.length < 2) {
    return null;
  }
  const resolved: { start: number; marker: string; citationIndex: number }[] = [];
  let cursor = 0;
  for (const raw of rawMembers) {
    const trimmed = raw.trim();
    const marker = `[${trimmed}]`;
    const parsed = parseMarker(marker);
    const citationIndex = markerToIndex.get(marker);
    if (!parsed || citationIndex === undefined) {
      // Advance the cursor past this member so a later identical member token
      // does not re-match this slice; an unresolved member contributes no chip.
      cursor += raw.length + 1;
      continue;
    }
    const found = inner.indexOf(trimmed, cursor);
    // `trimmed` is non-empty (it resolved), so indexOf cannot land inside the
    // already-consumed prefix; fall back to the raw member start if it does.
    const memberOffset = found === -1 ? cursor : found;
    resolved.push({
      // +1 for the opening `[`; bracketStart + 1 + memberOffset is the member's
      // offset in the original summary.
      start: bracketStart + 1 + memberOffset,
      marker,
      citationIndex,
    });
    cursor = memberOffset + trimmed.length;
  }
  if (resolved.length === 0) {
    return null;
  }
  return resolved;
}

/**
 * Split the summary prose into plain-text and citation-marker segments so the
 * UI can render resolvable markers as links to their citation cards.
 *
 * Only the exact markers of returned citations resolve, two ways:
 *  - a standalone bracket (`[A:1]`) that matches a citation marker verbatim;
 *  - a comma-merged bracket the model sometimes emits (`[Book A:70, Book
 *    A:51]`, Phase 14b live finding) — Phase 24 carries the API's merged-member
 *    contract to the renderer, so each member that resolves to a returned
 *    citation becomes its own linked chip. Members that resolve to nothing
 *    (invented/paraphrased) are dropped; a bracket with no resolvable member
 *    stays prose.
 *
 * Markers are bracket-delimited and contain no inner `[`/`]`, so exact
 * occurrences of distinct standalone markers can never overlap (`[X:1]` cannot
 * match inside `[X:12]`).
 *
 * `start` is the segment's offset in the original summary — a stable React key
 * and an ordering guarantee. Standalone markers round-trip exactly; merged
 * brackets do NOT (their `[`, `]`, and `, ` separators are structural, not
 * prose, and are dropped), so the concatenation invariant holds only for
 * summaries without an exploded merged bracket.
 */
export function segmentSummary(
  summary: string,
  citations: readonly Pick<SummaryCitation, "marker">[],
): SummarySegment[] {
  const markerToIndex = new Map<string, number>();
  citations.forEach((citation, citationIndex) => {
    // First citation wins a duplicate marker, matching first-appearance dedup.
    if (!markerToIndex.has(citation.marker)) {
      markerToIndex.set(citation.marker, citationIndex);
    }
  });

  // Walk every bracket group once. A group resolves either as a single verbatim
  // marker or as a merged bracket of resolvable members; anything else is prose.
  const spans: MarkerSpan[] = [];
  let scan = 0;
  while (scan < summary.length) {
    const open = summary.indexOf("[", scan);
    if (open === -1) {
      break;
    }
    const close = summary.indexOf("]", open + 1);
    if (close === -1) {
      break;
    }
    const bracket = summary.slice(open, close + 1);
    const inner = summary.slice(open + 1, close);
    const verbatimIndex = markerToIndex.get(bracket);
    if (verbatimIndex !== undefined) {
      spans.push({
        start: open,
        end: close + 1,
        markers: [{ start: open, marker: bracket, citationIndex: verbatimIndex }],
      });
    } else {
      const members = resolveMergedBracket(open, inner, markerToIndex);
      if (members) {
        spans.push({ start: open, end: close + 1, markers: members });
      }
    }
    scan = close + 1;
  }

  const segments: SummarySegment[] = [];
  let pos = 0;
  for (const span of spans) {
    if (span.start < pos) {
      // Unreachable for the forward bracket scan; guards against overlap.
      continue;
    }
    if (span.start > pos) {
      segments.push({ kind: "text", start: pos, text: summary.slice(pos, span.start) });
    }
    for (const member of span.markers) {
      segments.push({
        kind: "marker",
        start: member.start,
        marker: member.marker,
        citationIndex: member.citationIndex,
      });
    }
    pos = span.end;
  }
  if (pos < summary.length) {
    segments.push({ kind: "text", start: pos, text: summary.slice(pos) });
  }
  return segments;
}

/**
 * Presentational guard for `parent_section`: EPUB extraction sometimes leaves
 * raw HTML fragments in the section metadata (seen live in Phase 16's verify:
 * `<a href="part0002.html#pt03ch_11" …`). React escapes them — no XSS — but a
 * tag soup header is worse than none, so anything containing `<` is dropped.
 */
export function displaySection(section: string | null): string | null {
  if (!section) {
    return null;
  }
  const trimmed = section.trim();
  if (trimmed.length === 0 || trimmed.includes("<")) {
    return null;
  }
  return trimmed;
}

/** `134` → `"2:14"` — the elapsed label shown while a search is in flight. */
export function formatElapsed(totalSeconds: number): string {
  const whole = Math.max(0, Math.floor(totalSeconds));
  const minutes = Math.floor(whole / 60);
  const seconds = whole % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}
