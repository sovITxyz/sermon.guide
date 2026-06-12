import type { ChunkItem, PositionResponse } from "./types";

/**
 * Pure helpers for the reader VIEW (app/read/[bookId], components/reader/*):
 * window planning/merging, prepend scroll compensation, and the
 * position-persistence decision. No DOM, no server-only imports — unit-tested
 * in test/reader-view.test.ts. (lib/reader.ts is the sibling for the proxy
 * routes.)
 */

/** Chunks fetched per window — the API/proxy default (api/reader.py DEFAULT_WINDOW). */
export const WINDOW_LIMIT = 40;

/** Chunks of context fetched ABOVE a ?chunk=N anchor so it doesn't sit at a hard top edge. */
export const ANCHOR_CONTEXT = 5;

/** Scroll-settle debounce before a position PUT (ms of scroll silence). */
export const SETTLE_MS = 1500;

/**
 * Minimum offset_ratio movement (same chunk) that counts as "changed" for
 * persistence — sub-1% drift inside one chunk never triggers a PUT.
 */
export const OFFSET_EPSILON = 0.01;

/** One windowed fetch against GET /api/books/{id}/chunks?start&limit. */
export interface FetchPlan {
  start: number;
  limit: number;
}

/** Viewport-relative geometry of one rendered chunk (from getBoundingClientRect). */
export interface ChunkRect {
  chunk_index: number;
  top: number;
  height: number;
}

/** What the reader persists: topmost-visible chunk + intra-chunk scroll fraction. */
export interface PositionSnapshot {
  chunk_index: number;
  offset_ratio: number;
}

/**
 * Parse the ?chunk=N search param. Only a plain non-negative base-10 integer
 * (no sign, no exponent, no fraction) within safe-integer range is an anchor;
 * everything else — arrays, "", "-3", "1e3", "12abc" — is null (open at the
 * saved position / start instead).
 */
export function parseChunkParam(raw: string | string[] | undefined): number | null {
  if (typeof raw !== "string" || !/^\d+$/.test(raw)) {
    return null;
  }
  const value = Number(raw);
  return Number.isSafeInteger(value) ? value : null;
}

/**
 * First window: anchored fetches start ANCHOR_CONTEXT chunks above the anchor
 * (clamped to 0) so the target lands with context; un-anchored reads start at 0.
 * `start` is a chunk_index LOWER BOUND upstream, so an anchor past the book's
 * end simply comes back empty (the caller refetches from 0).
 */
export function initialWindowPlan(anchor: number | null): FetchPlan {
  const start = anchor === null ? 0 : Math.max(0, anchor - ANCHOR_CONTEXT);
  return { start, limit: WINDOW_LIMIT };
}

/**
 * Window above the loaded range, or null when there is nothing above (empty
 * state or chunk 0 already loaded). `limit` is exactly the gap size so the
 * response can never overlap the loaded range, and it is always >= 1 (the
 * API 422s on limit < 1).
 */
export function prependPlan(chunks: readonly ChunkItem[]): FetchPlan | null {
  const first = chunks[0];
  if (!first || first.chunk_index === 0) {
    return null;
  }
  const start = Math.max(0, first.chunk_index - WINDOW_LIMIT);
  return { start, limit: first.chunk_index - start };
}

/** Window below the loaded range, or null before anything is loaded. */
export function appendPlan(chunks: readonly ChunkItem[]): FetchPlan | null {
  const last = chunks[chunks.length - 1];
  if (!last) {
    return null;
  }
  return { start: last.chunk_index + 1, limit: WINDOW_LIMIT };
}

/**
 * Merge a fetched window into the loaded range, de-duping on chunk_index:
 * prepends keep only rows strictly below the current first, appends only rows
 * strictly above the current last, so the result stays ascending and
 * duplicate-free even if the ranges overlap. Returns the SAME array reference
 * when nothing new arrived (no wasted re-render).
 */
export function mergeWindow(
  current: readonly ChunkItem[],
  incoming: readonly ChunkItem[],
  direction: "prepend" | "append",
): readonly ChunkItem[] {
  if (current.length === 0) {
    return incoming.length === 0 ? current : [...incoming];
  }
  if (direction === "prepend") {
    const firstLoaded = current[0]?.chunk_index ?? 0;
    const fresh = incoming.filter((chunk) => chunk.chunk_index < firstLoaded);
    return fresh.length === 0 ? current : [...fresh, ...current];
  }
  const lastLoaded = current[current.length - 1]?.chunk_index ?? -1;
  const fresh = incoming.filter((chunk) => chunk.chunk_index > lastLoaded);
  return fresh.length === 0 ? current : [...current, ...fresh];
}

/**
 * A short window IS the end-of-book signal: `start` is a lower bound and rows
 * come back chunk_index-ascending, so fewer rows than requested means the book
 * has nothing further. (Only meaningful for forward/initial fetches — a
 * prepend's limit is the exact gap size.)
 */
export function reachedEnd(receivedCount: number, requestedLimit: number): boolean {
  return receivedCount < requestedLimit;
}

/** True once chunk 0 is loaded — the top sentinel disappears at the book's start. */
export function atBookStart(chunks: readonly ChunkItem[]): boolean {
  return chunks[0]?.chunk_index === 0;
}

/**
 * Manual anchoring for prepends: Safari has no overflow-anchor, so after new
 * rows are inserted above the viewport the scroller must be moved down by
 * exactly the height that was added (scrollHeight delta), keeping the text the
 * user was reading visually still. The delta is floored at 0 — a merge that
 * added nothing must not move the viewport.
 */
export function compensatedScrollTop(
  prevScrollTop: number,
  prevScrollHeight: number,
  newScrollHeight: number,
): number {
  return prevScrollTop + Math.max(0, newScrollHeight - prevScrollHeight);
}

/**
 * The position to persist: the first chunk whose bottom edge is below
 * `viewportTop` (i.e. the chunk overlapping the top of the viewport), with
 * offset_ratio = how much of that chunk has scrolled past the top, clamped to
 * 0..1 and rounded to 3 decimals. Scrolled past everything → last chunk at
 * 1.0; nothing measured → null (don't persist garbage).
 */
export function visiblePosition(
  rects: readonly ChunkRect[],
  viewportTop = 0,
): PositionSnapshot | null {
  for (const rect of rects) {
    if (rect.top + rect.height > viewportTop) {
      const raw = rect.height <= 0 ? 0 : (viewportTop - rect.top) / rect.height;
      return { chunk_index: rect.chunk_index, offset_ratio: roundRatio(clamp01(raw)) };
    }
  }
  const last = rects[rects.length - 1];
  return last ? { chunk_index: last.chunk_index, offset_ratio: 1 } : null;
}

/**
 * Never PUT while unchanged: persist only when nothing has been sent yet, the
 * chunk changed, or offset_ratio moved by at least OFFSET_EPSILON within the
 * same chunk.
 */
export function shouldPersist(lastSent: PositionSnapshot | null, next: PositionSnapshot): boolean {
  if (lastSent === null) {
    return true;
  }
  if (lastSent.chunk_index !== next.chunk_index) {
    return true;
  }
  return Math.abs(lastSent.offset_ratio - next.offset_ratio) >= OFFSET_EPSILON;
}

/**
 * Normalize a GET /position response into a snapshot: no saved position
 * (chunk_index null) → null; a saved chunk with a NULL offset_ratio reads as
 * 0 (top of the chunk) for both restore-scroll and change detection.
 */
export function savedPositionSnapshot(position: PositionResponse): PositionSnapshot | null {
  if (position.chunk_index === null) {
    return null;
  }
  return { chunk_index: position.chunk_index, offset_ratio: position.offset_ratio ?? 0 };
}

function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value));
}

function roundRatio(value: number): number {
  return Math.round(value * 1000) / 1000;
}
