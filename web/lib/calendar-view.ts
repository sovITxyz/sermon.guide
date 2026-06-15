/**
 * Pure (no-DOM, no-fetch) view helpers for the calendar page (Phase 39).
 *
 * The page's whole state is in the URL — `?view=year|month&date=YYYY-MM-DD` —
 * so it is linkable and deep-linkable. These helpers turn the raw, possibly
 * malformed query into a normalized view + anchor, derive the single half-open
 * range the one `/sermon-events` fetch needs, group the returned events by day,
 * and assign each `series` a deterministic color. Everything here is pure
 * string/number arithmetic so it is timezone-immune (see web/lib/dates.ts) and
 * unit-testable without a browser.
 */

import {
  type DateRange,
  daysInMonth,
  formatDate,
  monthRange,
  parseDate,
  today,
  yearRange,
} from "@/lib/dates";
import type { CalendarEvent } from "@/lib/types";

/** The two read-only views Phase 39 ships (week + CRUD arrive in Phase 40). */
export type CalendarViewKind = "year" | "month";

/** The normalized, validated calendar state derived from the URL. */
export interface CalendarState {
  view: CalendarViewKind;
  /** The anchor date, `YYYY-MM-DD`. Year view uses its year; month its month. */
  anchor: string;
  /** Anchor year, 1-based-safe integer. */
  year: number;
  /** Anchor month, 1-based (1 = January). */
  month: number;
}

/**
 * Normalize the raw `?view` / `?date` query into a {@link CalendarState}.
 *
 * Defensive by construction: an unknown/missing `view` falls back to `year`
 * (the headline wall-planner), and a missing, structurally-invalid, OR
 * semantically-impossible `date` falls back to `now` (today, local). `parseDate`
 * only checks the `YYYY-MM-DD` SHAPE (it throws on a malformed string but
 * happily returns `month: 13` for "2026-13-99"), so this layer additionally
 * range-checks month/day — otherwise a hand-typed `?date=2026-13-99` would
 * drive `monthGrid`/`monthRange` with an out-of-range month and render a
 * garbage grid. A bad `?date` must degrade to today, never crash or corrupt.
 *
 * `now` is injectable so tests can pin "today" without touching the clock.
 */
export function parseCalendarState(
  rawView: string | null | undefined,
  rawDate: string | null | undefined,
  now: Date = new Date(),
): CalendarState {
  const view: CalendarViewKind = rawView === "month" ? "month" : "year";

  let anchor = today(now);
  if (typeof rawDate === "string") {
    try {
      // parseDate validates the YYYY-MM-DD shape and throws on anything else;
      // additionally enforce a real calendar date (1 ≤ month ≤ 12, day within
      // the month). Re-format from the parsed parts so the anchor is canonical.
      const { year, month, day } = parseDate(rawDate);
      if (month >= 1 && month <= 12 && day >= 1 && day <= daysInMonth(year, month)) {
        anchor = formatDate(year, month, day);
      }
    } catch {
      // Malformed ?date — keep the today() fallback above.
    }
  }

  const { year, month } = parseDate(anchor);
  return { view, anchor, year, month };
}

/**
 * The single half-open `[start, end)` range the one `/sermon-events` fetch
 * needs for a given state: the whole anchor year for the year view, the whole
 * anchor month for the month view. Both fit well under the API's 400-day cap,
 * so each view is exactly one call (the year is 365/366 days).
 */
export function rangeForState(state: CalendarState): DateRange {
  return state.view === "year" ? yearRange(state.year) : monthRange(state.year, state.month);
}

/**
 * Group events by their `event_date` (`YYYY-MM-DD`). The returned Map's values
 * preserve the API's `event_date`-ascending order (the API sorts; we never
 * re-sort), so a day's events render in a stable, server-defined sequence. A
 * day with no events is simply absent from the Map.
 */
export function groupByDate(events: readonly CalendarEvent[]): Map<string, CalendarEvent[]> {
  const byDate = new Map<string, CalendarEvent[]>();
  for (const event of events) {
    const bucket = byDate.get(event.event_date);
    if (bucket) {
      bucket.push(event);
    } else {
      byDate.set(event.event_date, [event]);
    }
  }
  return byDate;
}

/**
 * A small, fixed palette of Tailwind utility-class triples for series dots and
 * chips. Deliberately a closed set of static class strings (not interpolated)
 * so Tailwind's content scanner emits every one — a dynamically-built class
 * name like `bg-${x}` would be purged from the production CSS.
 */
export interface SeriesColor {
  /** Background for a chip / a filled dot. */
  bg: string;
  /** Foreground text color for a chip. */
  text: string;
  /** A standalone dot color (used in the dense year view). */
  dot: string;
}

const SERIES_PALETTE: readonly SeriesColor[] = [
  { bg: "bg-blue-100", text: "text-blue-800", dot: "bg-blue-500" },
  { bg: "bg-emerald-100", text: "text-emerald-800", dot: "bg-emerald-500" },
  { bg: "bg-amber-100", text: "text-amber-800", dot: "bg-amber-500" },
  { bg: "bg-rose-100", text: "text-rose-800", dot: "bg-rose-500" },
  { bg: "bg-violet-100", text: "text-violet-800", dot: "bg-violet-500" },
  { bg: "bg-cyan-100", text: "text-cyan-800", dot: "bg-cyan-500" },
  { bg: "bg-orange-100", text: "text-orange-800", dot: "bg-orange-500" },
  { bg: "bg-teal-100", text: "text-teal-800", dot: "bg-teal-500" },
];

/** The neutral color for events with no series label (`series === null`). */
const NO_SERIES_COLOR: SeriesColor = {
  bg: "bg-gray-100",
  text: "text-gray-700",
  dot: "bg-gray-400",
};

/**
 * A stable, non-negative 32-bit hash of a string (FNV-1a-ish via the classic
 * `h = h*31 + c` fold, kept in 32 bits with `| 0` then unsigned-shifted). Pure
 * and deterministic so a given series always maps to the same palette slot
 * across renders and reloads — the "deterministic series→color hash" the B3
 * backlog calls for.
 */
function hashString(value: string): number {
  let hash = 0;
  for (let i = 0; i < value.length; i += 1) {
    hash = (hash * 31 + value.charCodeAt(i)) | 0;
  }
  return hash >>> 0;
}

/**
 * Map a `series` label to a deterministic {@link SeriesColor}. `null` (no
 * series) gets the neutral gray; any non-empty label hashes into the fixed
 * palette. Same label → same color, every time, with no shared mutable state.
 */
export function seriesColor(series: string | null): SeriesColor {
  if (series === null || series.length === 0) {
    return NO_SERIES_COLOR;
  }
  // SERIES_PALETTE is a non-empty const; the modulo keeps the index in range,
  // and the `?? NO_SERIES_COLOR` satisfies noUncheckedIndexedAccess.
  const index = hashString(series) % SERIES_PALETTE.length;
  return SERIES_PALETTE[index] ?? NO_SERIES_COLOR;
}

/**
 * Build the `?view=&date=` href for a calendar link, normalizing the date to
 * the canonical `YYYY-MM-DD`. Used by the prev/next month controls and the
 * year→month drill-down (clicking a MiniMonth opens that month).
 */
export function calendarHref(view: CalendarViewKind, date: string): string {
  const { year, month, day } = parseDate(date);
  const canonical = formatDate(year, month, day);
  const params = new URLSearchParams({ view, date: canonical });
  return `/calendar?${params.toString()}`;
}
