/**
 * Deterministic `series` → Tailwind color mapping for the calendar (Phase 40).
 *
 * Pure, no DOM, no fetch — unit-tested in test/series-color.test.ts and reused
 * across the year / month / week views so a given series renders the SAME color
 * everywhere (same input → same output, no shared mutable state).
 *
 * TAILWIND LITERAL-CLASS INVARIANT (load-bearing): Tailwind's content scanner
 * only emits classes that appear as COMPLETE LITERAL strings in source — there
 * is no safelist. A runtime-built class like `bg-${hue}-500` or `bg-rose-${n}`
 * is PURGED from production CSS and renders unstyled. So the palette below is a
 * CLOSED const array of fully-literal class triples; this module NEVER derives
 * a class name from the series string or a hue. The series only selects an
 * INDEX into the literal array.
 */

/** A Tailwind class triple for a series: a chip background+text and a dot. */
export interface SeriesColor {
  /** Background for a chip / a filled dot. */
  bg: string;
  /** Foreground text color for a chip. */
  text: string;
  /** A standalone dot color (used in the dense year/month views). */
  dot: string;
}

/**
 * The closed palette — every entry is a complete literal Tailwind class string
 * so the content scanner emits all of them. Order is stable: a series's slot is
 * `hash(series) % SERIES_PALETTE.length`, so reordering or resizing this array
 * would reshuffle existing series→color assignments (acceptable; not persisted).
 */
export const SERIES_PALETTE: readonly SeriesColor[] = [
  { bg: "bg-blue-100", text: "text-blue-800", dot: "bg-blue-500" },
  { bg: "bg-emerald-100", text: "text-emerald-800", dot: "bg-emerald-500" },
  { bg: "bg-amber-100", text: "text-amber-800", dot: "bg-amber-500" },
  { bg: "bg-rose-100", text: "text-rose-800", dot: "bg-rose-500" },
  { bg: "bg-violet-100", text: "text-violet-800", dot: "bg-violet-500" },
  { bg: "bg-cyan-100", text: "text-cyan-800", dot: "bg-cyan-500" },
  { bg: "bg-orange-100", text: "text-orange-800", dot: "bg-orange-500" },
  { bg: "bg-teal-100", text: "text-teal-800", dot: "bg-teal-500" },
];

/**
 * The stable neutral color for events with no series (`series === null` or
 * empty). A fixed literal triple — never indexed into the palette.
 */
export const NO_SERIES_COLOR: SeriesColor = {
  bg: "bg-gray-100",
  text: "text-gray-700",
  dot: "bg-gray-400",
};

/**
 * A stable, non-negative 32-bit hash of a string via the classic
 * `h = h*31 + c` fold, kept in 32 bits with `| 0` then unsigned-shifted so the
 * result is a deterministic `>>> 0` value. Pure — a given series always hashes
 * to the same number across renders, reloads, and machines.
 */
function hashString(value: string): number {
  let hash = 0;
  for (let i = 0; i < value.length; i += 1) {
    hash = (hash * 31 + value.charCodeAt(i)) | 0;
  }
  return hash >>> 0;
}

/**
 * Map a `series` label to a deterministic {@link SeriesColor}. `null` or the
 * empty string (no series) gets the neutral gray; any non-empty label hashes
 * into the fixed literal palette. Same label → same color, every time. The
 * `?? NO_SERIES_COLOR` satisfies `noUncheckedIndexedAccess` (the modulo already
 * keeps the index in range for the non-empty const array).
 */
export function seriesColor(series: string | null): SeriesColor {
  if (series === null || series.length === 0) {
    return NO_SERIES_COLOR;
  }
  const index = hashString(series) % SERIES_PALETTE.length;
  return SERIES_PALETTE[index] ?? NO_SERIES_COLOR;
}
