/**
 * Pure `YYYY-MM-DD` string date helpers for the calendar (Phase 39).
 *
 * Every date the calendar handles is a Postgres `DATE` (day-anchored, no time,
 * no timezone) carried end-to-end as a `YYYY-MM-DD` STRING. This module does
 * ALL of its arithmetic on the numeric (year, month, day) parts — it never
 * calls `new Date("YYYY-MM-DD")`.
 *
 * Why never `new Date(dateString)`: the string ctor parses a bare date as UTC
 * midnight, so `new Date("2026-03-15")` rendered or compared in any UTC-minus
 * timezone shows the PREVIOUS day. String/number math is timezone-immune. The
 * one place a Date object appears (weekday-of-the-first computation) uses
 * `Date.UTC(y, m, d)` with explicit numeric args and reads only the UTC field
 * back, which is also timezone-immune.
 *
 * Month numbers in this module's public surface are 1-based (1 = January,
 * 12 = December) to match the `YYYY-MM-DD` wire format. The internal Date hop
 * converts to JS's 0-based month exactly once, at the call site.
 */

/**
 * The first day of the week, as a JS weekday index (0 = Sunday … 6 = Saturday).
 * Settled as Sunday (B3 open question). The month grid pads its first row so
 * that this weekday lands in column 0.
 */
export const WEEK_STARTS_ON = 0;

/** Days in each 1-based month for a non-leap year; February patched at runtime. */
const DAYS_IN_MONTH_COMMON = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

/** A parsed `YYYY-MM-DD` date: 1-based month, matching the wire format. */
export interface DateParts {
  year: number;
  /** 1-based: 1 = January … 12 = December. */
  month: number;
  /** 1-based day of month. */
  day: number;
}

/** One cell in a month grid. */
export interface DayCell {
  /** `YYYY-MM-DD` for this cell's day. */
  date: string;
  /** 1-based day-of-month number to render. */
  day: number;
  /**
   * True when the cell belongs to the month the grid is FOR; false for the
   * leading/trailing days borrowed from the adjacent months to fill the
   * first/last week.
   */
  inMonth: boolean;
}

/**
 * One cell in the horizontal year planner (the "spreadsheet" year layout): a
 * single real day of a month. Unlike {@link DayCell} there is no `inMonth` flag
 * — the planner has no borrowed-adjacent-month padding; a slot that would fall
 * past the month's last day is simply absent (a `null` in the row), so every
 * cell here is a real, in-month day.
 */
export interface YearPlannerCell {
  /** `YYYY-MM-DD` for this day. */
  date: string;
  /** 1-based day-of-month (1 … 31). */
  day: number;
  /** True for Saturday/Sunday, for optional weekend shading. */
  weekend: boolean;
}

/**
 * One month-row of the horizontal year planner: a 1-based `month` and exactly
 * 31 column slots. Slot index `i` (0-based) is day `i + 1`; a slot is `null`
 * when that day does not exist in the month (Feb 30/31, the 31st of a 30-day
 * month), which the view renders as an inert blocked cell.
 */
export interface YearPlannerRow {
  /** 1-based month (1 = January). */
  month: number;
  /** Exactly 31 slots; `null` where the day does not exist in this month. */
  cells: (YearPlannerCell | null)[];
}

/** A half-open `[start, end)` range of `YYYY-MM-DD` strings (matches the API). */
export interface DateRange {
  /** Inclusive start, `YYYY-MM-DD`. */
  start: string;
  /** Exclusive end, `YYYY-MM-DD`. */
  end: string;
}

/** Pad an integer to a fixed width with leading zeros (no locale, no Date). */
function pad(value: number, width: number): string {
  return String(value).padStart(width, "0");
}

/** Gregorian leap-year rule. */
export function isLeapYear(year: number): boolean {
  return (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
}

/** Days in a 1-based `month` of `year`, leap-year-correct for February. */
export function daysInMonth(year: number, month: number): number {
  if (month === 2 && isLeapYear(year)) {
    return 29;
  }
  // month is 1-based; the table is 0-based.
  return DAYS_IN_MONTH_COMMON[month - 1] ?? 30;
}

/**
 * Format 1-based `(year, month, day)` parts as `YYYY-MM-DD`. Pure string build
 * — never routes through a Date.
 */
export function formatDate(year: number, month: number, day: number): string {
  return `${pad(year, 4)}-${pad(month, 2)}-${pad(day, 2)}`;
}

/**
 * Parse a `YYYY-MM-DD` string into its numeric parts (1-based month/day). Pure
 * integer parsing — does NOT construct a Date, so it cannot UTC-shift. Throws
 * on a structurally malformed string so a bad value surfaces loudly rather
 * than silently becoming `NaN`.
 */
export function parseDate(value: string): DateParts {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) {
    throw new Error(`Not a YYYY-MM-DD date: ${value}`);
  }
  // The capture groups are guaranteed present by the regex match.
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  return { year, month, day };
}

/**
 * The JS weekday index (0 = Sunday … 6 = Saturday) of the first day of a
 * 1-based `(year, month)`. This is the only Date use in the module: it builds
 * the date with explicit numeric UTC args and reads the UTC weekday straight
 * back, so it is timezone-immune. The month is converted to JS's 0-based form
 * here, exactly once.
 */
export function weekdayOfFirst(year: number, month: number): number {
  return new Date(Date.UTC(year, month - 1, 1)).getUTCDay();
}

/** Today's local date as a `YYYY-MM-DD` string (uses the device's local day). */
export function today(now: Date = new Date()): string {
  // Local fields — the calendar's notion of "today" is the user's wall-clock
  // day, and getFullYear/getMonth/getDate read the local calendar date.
  return formatDate(now.getFullYear(), now.getMonth() + 1, now.getDate());
}

/**
 * The half-open `[start, end)` range covering an entire 1-based `(year, month)`:
 * the first of the month through (exclusive) the first of the next month. The
 * exclusive end is the load-bearing half-open boundary the API expects — an
 * event dated on the last day of the month is INCLUDED, the first of next month
 * is not. Rolls December → next January.
 */
export function monthRange(year: number, month: number): DateRange {
  const nextYear = month === 12 ? year + 1 : year;
  const nextMonth = month === 12 ? 1 : month + 1;
  return {
    start: formatDate(year, month, 1),
    end: formatDate(nextYear, nextMonth, 1),
  };
}

/**
 * The half-open `[start, end)` range covering an entire `year`: Jan 1 of
 * `year` through (exclusive) Jan 1 of the next year. A full year is 365/366
 * days — well under the API's 400-day `RANGE_CAP_DAYS`, so a year view fits in
 * one `/calendar/events` call.
 */
export function yearRange(year: number): DateRange {
  return {
    start: formatDate(year, 1, 1),
    end: formatDate(year + 1, 1, 1),
  };
}

/**
 * Advance a 1-based `(year, month)` by `delta` months (negative steps back),
 * normalizing the rollover across year boundaries. Returns `{ year, month }`.
 */
export function addMonths(
  year: number,
  month: number,
  delta: number,
): {
  year: number;
  month: number;
} {
  // Work in a 0-based absolute month count, then split back out.
  const absolute = year * 12 + (month - 1) + delta;
  return {
    year: Math.floor(absolute / 12),
    month: (absolute % 12) + 1,
  };
}

/**
 * Build the month grid for a 1-based `(year, month)`: full weeks of 7 `DayCell`s
 * each, padded at the front with the trailing days of the previous month and at
 * the back with the leading days of the next month, so every row has exactly 7
 * cells and column 0 is always `WEEK_STARTS_ON` (Sunday). Cells outside the
 * target month are flagged `inMonth: false`.
 *
 * Pure string/number arithmetic: day numbers and `YYYY-MM-DD` strings are built
 * with `formatDate`, never by mutating a Date.
 */
export function monthGrid(year: number, month: number): DayCell[][] {
  const firstWeekday = weekdayOfFirst(year, month);
  // How many leading cells from the previous month fill row 0 before the 1st.
  const leadingCount = (firstWeekday - WEEK_STARTS_ON + 7) % 7;

  const { year: prevYear, month: prevMonth } = addMonths(year, month, -1);
  const { year: nextYear, month: nextMonth } = addMonths(year, month, 1);
  const prevDays = daysInMonth(prevYear, prevMonth);
  const thisDays = daysInMonth(year, month);

  const cells: DayCell[] = [];

  // Leading days from the previous month.
  for (let i = 0; i < leadingCount; i += 1) {
    const day = prevDays - leadingCount + 1 + i;
    cells.push({ date: formatDate(prevYear, prevMonth, day), day, inMonth: false });
  }

  // The target month's own days.
  for (let day = 1; day <= thisDays; day += 1) {
    cells.push({ date: formatDate(year, month, day), day, inMonth: true });
  }

  // Trailing days from the next month to complete the final week.
  const remainder = cells.length % 7;
  if (remainder !== 0) {
    const trailingCount = 7 - remainder;
    for (let day = 1; day <= trailingCount; day += 1) {
      cells.push({ date: formatDate(nextYear, nextMonth, day), day, inMonth: false });
    }
  }

  // Chunk the flat cell list into weeks of 7.
  const weeks: DayCell[][] = [];
  for (let i = 0; i < cells.length; i += 7) {
    weeks.push(cells.slice(i, i + 7));
  }
  return weeks;
}

/**
 * The twelve 1-based month numbers `[1 … 12]` — for a year view that lays out
 * twelve month grids. A named helper so the year page never hard-codes the
 * range or risks an off-by-one.
 */
export function monthsOfYear(): number[] {
  return Array.from({ length: 12 }, (_, i) => i + 1);
}

/**
 * Build the horizontal year planner grid for `year`: twelve {@link YearPlannerRow}s
 * (January → December), each a fixed 31-slot row where column `i` (0-based) is
 * day `i + 1`. A slot is `null` when that day does not exist in the month — Feb
 * 30/31, and the 31st of any 30-day month — which the planner view renders as an
 * inert blocked cell. Real slots carry the `YYYY-MM-DD` date, the day number,
 * and a `weekend` flag.
 *
 * Pure string/number arithmetic: dates come from {@link formatDate} and the
 * weekend flag from {@link weekdayOf} (the only Date hop, `Date.UTC`-based and
 * therefore timezone-immune). The fixed 31-column shape lets the view render a
 * clean rectangular months-down / days-across spreadsheet without per-row
 * column math.
 */
export function yearGrid(year: number): YearPlannerRow[] {
  const rows: YearPlannerRow[] = [];
  for (let month = 1; month <= 12; month += 1) {
    const lastDay = daysInMonth(year, month);
    const cells: (YearPlannerCell | null)[] = [];
    for (let day = 1; day <= 31; day += 1) {
      if (day > lastDay) {
        // The day does not exist in this month — a blocked slot.
        cells.push(null);
        continue;
      }
      const weekday = weekdayOf(year, month, day);
      cells.push({
        date: formatDate(year, month, day),
        day,
        weekend: weekday === 0 || weekday === 6,
      });
    }
    rows.push({ month, cells });
  }
  return rows;
}

/**
 * The JS weekday index (0 = Sunday … 6 = Saturday) of a 1-based
 * `(year, month, day)`. Like {@link weekdayOfFirst}, the ONLY Date use is
 * `Date.UTC(...)` with explicit numeric args, reading the UTC weekday straight
 * back — timezone-immune. Never parses a `YYYY-MM-DD` STRING through a Date.
 */
export function weekdayOf(year: number, month: number, day: number): number {
  return new Date(Date.UTC(year, month - 1, day)).getUTCDay();
}

/**
 * Advance a `YYYY-MM-DD` string by `delta` calendar days (negative steps back),
 * returning a fresh canonical `YYYY-MM-DD`. Pure integer arithmetic on the
 * (year, month, day) parts: it normalizes day overflow/underflow by walking
 * month/year boundaries with {@link daysInMonth}, so it never constructs a Date
 * from the string and is therefore timezone-immune. Used by the week grid and
 * the prev/next-week nav (delta = ±7).
 */
export function addDays(value: string, delta: number): string {
  let { year, month, day } = parseDate(value);
  day += delta;
  // Walk forward over month ends.
  while (day > daysInMonth(year, month)) {
    day -= daysInMonth(year, month);
    if (month === 12) {
      month = 1;
      year += 1;
    } else {
      month += 1;
    }
  }
  // Walk backward over month starts (borrow from the previous month).
  while (day < 1) {
    if (month === 1) {
      month = 12;
      year -= 1;
    } else {
      month -= 1;
    }
    day += daysInMonth(year, month);
  }
  return formatDate(year, month, day);
}

/**
 * The `YYYY-MM-DD` of the first day (Sunday, per {@link WEEK_STARTS_ON}) of the
 * week CONTAINING `value`. Steps back by the day's offset from the week start
 * using the same `(weekday - WEEK_STARTS_ON + 7) % 7` idiom `monthGrid` uses
 * for its leading pad. Pure string/number arithmetic.
 */
export function weekStart(value: string): string {
  const { year, month, day } = parseDate(value);
  const offset = (weekdayOf(year, month, day) - WEEK_STARTS_ON + 7) % 7;
  return addDays(value, -offset);
}

/**
 * Build the 7-day grid for the week CONTAINING `value`: exactly seven
 * consecutive `DayCell`s starting on Sunday (per {@link WEEK_STARTS_ON}). Unlike
 * the month grid, a week has no "borrowed adjacent month" notion — every day in
 * the week is a real day the user can act on — so every cell is `inMonth: true`.
 * Pure string/number arithmetic via {@link addDays}; never parses a Date string.
 */
export function weekGrid(value: string): DayCell[] {
  const start = weekStart(value);
  const cells: DayCell[] = [];
  for (let i = 0; i < 7; i += 1) {
    const date = addDays(start, i);
    cells.push({ date, day: parseDate(date).day, inMonth: true });
  }
  return cells;
}

/**
 * The half-open `[start, end)` range covering the week CONTAINING `value`:
 * Sunday of that week through (exclusive) the following Sunday — exactly seven
 * days, well under the API's 400-day cap, so a week view is one
 * `/calendar/events` call. The exclusive end (start + 7 days) is the
 * load-bearing half-open boundary the API expects: Saturday is INCLUDED, the
 * next Sunday is not. Rolls cleanly across month/year boundaries via
 * {@link addDays}.
 */
export function weekRange(value: string): DateRange {
  const start = weekStart(value);
  return { start, end: addDays(start, 7) };
}
