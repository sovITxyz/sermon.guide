/**
 * Pure helpers for the reading entry points (Phase 33): citation-card
 * "Read in context" links and library-row progress + "Continue reading".
 * No server-only deps — importable from client components and Vitest.
 */

/**
 * Href into the reader. With a `chunkIndex` the reader anchor-scrolls to that
 * chunk (?chunk=N — citation cards); without one it resumes from the saved
 * position (library rows).
 */
export function readHref(bookId: string, chunkIndex?: number): string {
  const base = `/read/${encodeURIComponent(bookId)}`;
  return chunkIndex === undefined ? base : `${base}?chunk=${chunkIndex}`;
}

/**
 * Render the API's 0..1 `progress` as a whole percentage, or null when there
 * is nothing to show (no saved position / book has no chunks). The API
 * already clamps to 1.0; the re-clamp here is defense against drift.
 */
export function formatProgress(progress: number | null): string | null {
  if (progress === null || !Number.isFinite(progress)) {
    return null;
  }
  const clamped = Math.min(1, Math.max(0, progress));
  return `${Math.round(clamped * 100)}%`;
}
