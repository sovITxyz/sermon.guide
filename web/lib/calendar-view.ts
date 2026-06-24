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
  weekRange,
  yearRange,
} from "@/lib/dates";
import type { CalendarEvent } from "@/lib/types";

// The deterministic series→color mapper (Phase 40) lives in its own module so
// the proxy/test layer can import it without pulling in the date helpers, but
// the calendar views have always imported it from here. Re-export it (and its
// type) so year / month / week all reach the SAME `seriesColor`, guaranteeing a
// given series renders the identical color in every view.
export { type SeriesColor, seriesColor } from "@/lib/series-color";

/** The calendar views: the year wall-planner, a month grid, and a week (Phase 40). */
export type CalendarViewKind = "year" | "month" | "week";

/**
 * The two YEAR-view layouts. `grid` is the original twelve-mini-month wall
 * planner; `planner` is the horizontal "spreadsheet" (months down, days 1–31
 * across). Orthogonal to {@link CalendarViewKind} and meaningful ONLY when
 * `view === "year"`; month and week ignore it. `grid` is the default, so a year
 * URL with no `layout` param stays the canonical clean URL.
 */
export type CalendarLayout = "grid" | "planner";

/** The normalized, validated calendar state derived from the URL. */
export interface CalendarState {
  view: CalendarViewKind;
  /** The anchor date, `YYYY-MM-DD`. Year view uses its year; month its month. */
  anchor: string;
  /** Anchor year, 1-based-safe integer. */
  year: number;
  /** Anchor month, 1-based (1 = January). */
  month: number;
  /** The year-view layout; `grid` unless view=year & layout=planner (see {@link CalendarLayout}). */
  layout: CalendarLayout;
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
  rawLayout: string | null | undefined = null,
): CalendarState {
  const view: CalendarViewKind =
    rawView === "month" ? "month" : rawView === "week" ? "week" : "year";

  // The year-view layout. Only "planner" opts into the horizontal spreadsheet;
  // anything else — a missing/unknown param, or a non-year view — is the default
  // twelve-mini-month "grid". Stored regardless of view so it round-trips, but
  // the views ignore it unless `view === "year"`. `rawLayout` is the LAST param
  // so the established `(…, now)` test seam keeps working unchanged.
  const layout: CalendarLayout = rawLayout === "planner" ? "planner" : "grid";

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
  return { view, anchor, year, month, layout };
}

/**
 * The single half-open `[start, end)` range the one `/sermon-events` fetch
 * needs for a given state: the whole anchor year for the year view, the whole
 * anchor month for the month view, or the seven days of the anchor's week
 * (Sunday-aligned) for the week view. All three fit well under the API's
 * 400-day cap, so each view is exactly one call (the year is 365/366 days).
 */
export function rangeForState(state: CalendarState): DateRange {
  switch (state.view) {
    case "year":
      return yearRange(state.year);
    case "month":
      return monthRange(state.year, state.month);
    case "week":
      return weekRange(state.anchor);
  }
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
 * Build the `?view=&date=` href for a calendar link, normalizing the date to
 * the canonical `YYYY-MM-DD`. Used by the prev/next period controls and the
 * year→month / month→week drill-downs. Works unchanged for `"week"` now that
 * {@link CalendarViewKind} widened.
 *
 * The optional `layout` serializes ONLY the non-default `"planner"` year layout
 * (`&layout=planner`); `"grid"` and `undefined` omit it, so every month/week
 * link and the default grid year stay canonical clean URLs. Pass the current
 * `state.layout` on year prev/next so paging years stays in the active layout.
 */
export function calendarHref(
  view: CalendarViewKind,
  date: string,
  layout?: CalendarLayout,
): string {
  const { year, month, day } = parseDate(date);
  const canonical = formatDate(year, month, day);
  const params = new URLSearchParams({ view, date: canonical });
  if (layout === "planner") {
    params.set("layout", "planner");
  }
  return `/calendar?${params.toString()}`;
}
