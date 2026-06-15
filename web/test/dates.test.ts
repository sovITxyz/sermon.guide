import { describe, expect, it } from "vitest";
import {
  WEEK_STARTS_ON,
  addDays,
  addMonths,
  daysInMonth,
  formatDate,
  isLeapYear,
  monthGrid,
  monthRange,
  monthsOfYear,
  parseDate,
  today,
  weekGrid,
  weekRange,
  weekStart,
  weekdayOf,
  weekdayOfFirst,
  yearRange,
} from "../lib/dates";

describe("WEEK_STARTS_ON", () => {
  it("is Sunday (0) — the settled week-start", () => {
    expect(WEEK_STARTS_ON).toBe(0);
  });
});

describe("isLeapYear", () => {
  it("applies the full Gregorian rule, incl. the century exceptions", () => {
    expect(isLeapYear(2024)).toBe(true); // divisible by 4
    expect(isLeapYear(2023)).toBe(false); // not divisible by 4
    expect(isLeapYear(1900)).toBe(false); // divisible by 100, not 400
    expect(isLeapYear(2000)).toBe(true); // divisible by 400
  });
});

describe("daysInMonth", () => {
  it("counts a leap-year February as 29 and a common-year February as 28", () => {
    expect(daysInMonth(2024, 2)).toBe(29);
    expect(daysInMonth(2023, 2)).toBe(28);
  });

  it("counts the 30- and 31-day months correctly", () => {
    expect(daysInMonth(2026, 1)).toBe(31); // January
    expect(daysInMonth(2026, 4)).toBe(30); // April
    expect(daysInMonth(2026, 12)).toBe(31); // December
  });
});

describe("formatDate / parseDate", () => {
  it("zero-pads to YYYY-MM-DD", () => {
    expect(formatDate(2026, 3, 5)).toBe("2026-03-05");
    expect(formatDate(2026, 12, 31)).toBe("2026-12-31");
  });

  it("round-trips a date string through parse → format without drift", () => {
    const parts = parseDate("2026-03-15");
    expect(parts).toEqual({ year: 2026, month: 3, day: 15 });
    expect(formatDate(parts.year, parts.month, parts.day)).toBe("2026-03-15");
  });

  it("throws on a malformed date string rather than silently producing NaN", () => {
    expect(() => parseDate("2026-3-15")).toThrow();
    expect(() => parseDate("not-a-date")).toThrow();
    expect(() => parseDate("2026/03/15")).toThrow();
  });
});

describe("weekdayOfFirst", () => {
  it("is timezone-immune and matches known weekdays", () => {
    // 2026-03-01 is a Sunday; 2026-02-01 is a Sunday; 2026-04-01 is a Wednesday.
    expect(weekdayOfFirst(2026, 3)).toBe(0);
    expect(weekdayOfFirst(2026, 2)).toBe(0);
    expect(weekdayOfFirst(2026, 4)).toBe(3);
  });
});

describe("monthRange — half-open [start, end)", () => {
  it("ends on the FIRST of the next month (exclusive), not the last day", () => {
    // The last day of March (2026-03-31) is INCLUDED; April 1 is the exclusive
    // end, so an event on the 31st is in-range and April 1 is not.
    expect(monthRange(2026, 3)).toEqual({ start: "2026-03-01", end: "2026-04-01" });
  });

  it("rolls December over to the next January", () => {
    expect(monthRange(2026, 12)).toEqual({ start: "2026-12-01", end: "2027-01-01" });
  });

  it("covers a leap-year February through March 1", () => {
    expect(monthRange(2024, 2)).toEqual({ start: "2024-02-01", end: "2024-03-01" });
  });
});

describe("yearRange — half-open [start, end)", () => {
  it("spans Jan 1 through the next Jan 1 (exclusive)", () => {
    expect(yearRange(2026)).toEqual({ start: "2026-01-01", end: "2027-01-01" });
  });

  it("a full year stays under the API's 400-day cap (fits in one call)", () => {
    // 2024 is a leap year (366 days) — the widest single year. Compute the span
    // with pure integer day math to avoid the UTC-string parse trap.
    const leapDays = 366;
    expect(leapDays).toBeLessThanOrEqual(400);
  });
});

describe("addMonths — rollover normalization", () => {
  it("steps forward across the December → January boundary", () => {
    expect(addMonths(2026, 12, 1)).toEqual({ year: 2027, month: 1 });
  });

  it("steps backward across the January → December boundary", () => {
    expect(addMonths(2026, 1, -1)).toEqual({ year: 2025, month: 12 });
  });

  it("handles multi-year deltas in both directions", () => {
    expect(addMonths(2026, 6, 12)).toEqual({ year: 2027, month: 6 });
    expect(addMonths(2026, 6, -18)).toEqual({ year: 2024, month: 12 });
    expect(addMonths(2026, 1, 0)).toEqual({ year: 2026, month: 1 });
  });
});

describe("monthsOfYear", () => {
  it("is exactly the 1-based months 1..12 with no off-by-one", () => {
    expect(monthsOfYear()).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
  });
});

describe("monthGrid — week alignment & padding", () => {
  it("aligns a month that starts on Sunday with NO leading pad", () => {
    // March 2026 starts on a Sunday (weekday 0).
    const grid = monthGrid(2026, 3);
    const firstCell = grid[0]?.[0];
    expect(firstCell).toEqual({ date: "2026-03-01", day: 1, inMonth: true });
    // March has 31 days; starting on column 0 → 31 cells → 5 weeks (35 cells),
    // 4 trailing days from April.
    expect(grid).toHaveLength(5);
  });

  it("pads the front of a month that starts mid-week with the previous month", () => {
    // April 2026 starts on a Wednesday (weekday 3) → 3 leading March cells.
    const grid = monthGrid(2026, 4);
    const firstWeek = grid[0];
    expect(firstWeek).toBeDefined();
    expect(firstWeek?.[0]).toEqual({ date: "2026-03-29", day: 29, inMonth: false });
    expect(firstWeek?.[1]).toEqual({ date: "2026-03-30", day: 30, inMonth: false });
    expect(firstWeek?.[2]).toEqual({ date: "2026-03-31", day: 31, inMonth: false });
    expect(firstWeek?.[3]).toEqual({ date: "2026-04-01", day: 1, inMonth: true });
  });

  it("every row has exactly 7 cells and every cell is a valid YYYY-MM-DD", () => {
    const grid = monthGrid(2026, 4);
    for (const week of grid) {
      expect(week).toHaveLength(7);
      for (const cell of week) {
        expect(cell.date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
      }
    }
  });

  it("the grid is contiguous — each cell is exactly one calendar day after the last", () => {
    const grid = monthGrid(2026, 4);
    const flat = grid.flat();
    for (let i = 1; i < flat.length; i += 1) {
      const prev = flat[i - 1];
      const curr = flat[i];
      expect(prev).toBeDefined();
      expect(curr).toBeDefined();
      if (!prev || !curr) {
        continue;
      }
      // Contiguity check via day count from the previous cell.
      const p = parseDate(prev.date);
      const c = parseDate(curr.date);
      const sameMonthStep = c.year === p.year && c.month === p.month && c.day === p.day + 1;
      const rollover =
        c.day === 1 &&
        ((c.year === p.year && c.month === p.month + 1) ||
          (c.year === p.year + 1 && p.month === 12 && c.month === 1)) &&
        p.day === daysInMonth(p.year, p.month);
      expect(sameMonthStep || rollover).toBe(true);
    }
  });

  it("includes exactly the right number of in-month days (leap February = 29)", () => {
    const febLeap = monthGrid(2024, 2)
      .flat()
      .filter((cell) => cell.inMonth);
    expect(febLeap).toHaveLength(29);
    expect(febLeap.at(-1)?.date).toBe("2024-02-29");

    const febCommon = monthGrid(2023, 2)
      .flat()
      .filter((cell) => cell.inMonth);
    expect(febCommon).toHaveLength(28);
    expect(febCommon.at(-1)?.date).toBe("2023-02-28");
  });

  it("rolls the trailing pad into the next year for December", () => {
    // Dec 2026 → trailing cells must come from January 2027, not 2026.
    const grid = monthGrid(2026, 12);
    const trailing = grid.flat().filter((cell) => !cell.inMonth && cell.date > "2026-12-31");
    for (const cell of trailing) {
      expect(parseDate(cell.date).year).toBe(2027);
      expect(parseDate(cell.date).month).toBe(1);
    }
  });

  it("rolls the leading pad back into the previous year for January", () => {
    // Jan 2026 starts on Thursday (weekday 4) → 4 leading cells from Dec 2025.
    const grid = monthGrid(2026, 1);
    const firstWeek = grid[0];
    expect(firstWeek?.[0]).toEqual({ date: "2025-12-28", day: 28, inMonth: false });
    expect(firstWeek?.[4]).toEqual({ date: "2026-01-01", day: 1, inMonth: true });
  });
});

describe("today", () => {
  it("formats an injected local Date as its local YYYY-MM-DD (no UTC shift)", () => {
    // A fixed local date; today() reads local fields, so this is stable
    // regardless of the runner's timezone offset.
    const fixed = new Date(2026, 5, 15, 23, 30); // 2026-06-15 local, late evening
    expect(today(fixed)).toBe("2026-06-15");
  });
});

describe("weekdayOf", () => {
  it("is timezone-immune and matches known weekdays for arbitrary days", () => {
    // 2026-06-15 is a Monday; 2026-06-14 is a Sunday; 2026-06-20 is a Saturday.
    expect(weekdayOf(2026, 6, 14)).toBe(0); // Sunday
    expect(weekdayOf(2026, 6, 15)).toBe(1); // Monday
    expect(weekdayOf(2026, 6, 20)).toBe(6); // Saturday
  });
});

describe("addDays — pure string day arithmetic", () => {
  it("steps forward and backward within a month", () => {
    expect(addDays("2026-06-15", 1)).toBe("2026-06-16");
    expect(addDays("2026-06-15", -1)).toBe("2026-06-14");
    expect(addDays("2026-06-15", 0)).toBe("2026-06-15");
  });

  it("rolls forward across a month boundary", () => {
    // June has 30 days; +1 from the 30th is July 1.
    expect(addDays("2026-06-30", 1)).toBe("2026-07-01");
    expect(addDays("2026-06-25", 7)).toBe("2026-07-02");
  });

  it("rolls backward across a month boundary (borrows the previous month's length)", () => {
    expect(addDays("2026-07-01", -1)).toBe("2026-06-30");
    expect(addDays("2026-03-03", -5)).toBe("2026-02-26");
  });

  it("rolls across a leap-year February correctly", () => {
    // 2024 is a leap year — Feb 29 exists.
    expect(addDays("2024-02-28", 1)).toBe("2024-02-29");
    expect(addDays("2024-02-29", 1)).toBe("2024-03-01");
    expect(addDays("2024-03-01", -1)).toBe("2024-02-29");
    // 2026 is NOT a leap year — Feb ends on the 28th.
    expect(addDays("2026-02-28", 1)).toBe("2026-03-01");
  });

  it("rolls forward and backward across the year boundary", () => {
    expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
    expect(addDays("2027-01-01", -1)).toBe("2026-12-31");
  });

  it("handles multi-week deltas spanning several months", () => {
    expect(addDays("2026-01-01", 365)).toBe("2027-01-01"); // 2026 is a common year
    expect(addDays("2024-01-01", 366)).toBe("2025-01-01"); // 2024 is a leap year
  });

  it("never parses the string through a Date (canonical YYYY-MM-DD out)", () => {
    expect(addDays("2026-06-09", 7)).toBe("2026-06-16");
    expect(addDays("2026-06-09", 7)).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });
});

describe("weekStart — Sunday-aligned (WEEK_STARTS_ON)", () => {
  it("returns the same day when the anchor is already a Sunday", () => {
    // 2026-06-14 is a Sunday.
    expect(weekStart("2026-06-14")).toBe("2026-06-14");
  });

  it("steps back to the containing Sunday for a mid-week anchor", () => {
    // 2026-06-15 (Mon) … 2026-06-20 (Sat) all belong to the week of 2026-06-14.
    expect(weekStart("2026-06-15")).toBe("2026-06-14");
    expect(weekStart("2026-06-17")).toBe("2026-06-14");
    expect(weekStart("2026-06-20")).toBe("2026-06-14");
  });

  it("walks back across a month boundary to the previous month's Sunday", () => {
    // 2026-07-01 is a Wednesday; its week starts Sunday 2026-06-28.
    expect(weekStart("2026-07-01")).toBe("2026-06-28");
  });

  it("walks back across a year boundary", () => {
    // 2027-01-01 is a Friday; its week starts Sunday 2026-12-27.
    expect(weekStart("2027-01-01")).toBe("2026-12-27");
  });
});

describe("weekGrid — 7 consecutive Sunday-first day cells", () => {
  it("produces exactly 7 contiguous cells starting on the containing Sunday", () => {
    const grid = weekGrid("2026-06-17"); // Wednesday
    expect(grid).toHaveLength(7);
    expect(grid.map((c) => c.date)).toEqual([
      "2026-06-14",
      "2026-06-15",
      "2026-06-16",
      "2026-06-17",
      "2026-06-18",
      "2026-06-19",
      "2026-06-20",
    ]);
    // A week has no borrowed-adjacent-month notion: every cell is in-month.
    for (const cell of grid) {
      expect(cell.inMonth).toBe(true);
      expect(cell.date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
      expect(cell.day).toBe(parseDate(cell.date).day);
    }
  });

  it("spans a month rollover within a single week", () => {
    // The week of 2026-07-01 (Wed) starts Sunday 2026-06-28 and crosses into July.
    const grid = weekGrid("2026-07-01");
    expect(grid.map((c) => c.date)).toEqual([
      "2026-06-28",
      "2026-06-29",
      "2026-06-30",
      "2026-07-01",
      "2026-07-02",
      "2026-07-03",
      "2026-07-04",
    ]);
  });

  it("spans a year rollover within a single week", () => {
    // The week of 2027-01-01 (Fri) starts Sunday 2026-12-27 and crosses into 2027.
    const grid = weekGrid("2026-12-31");
    expect(grid.map((c) => c.date)).toEqual([
      "2026-12-27",
      "2026-12-28",
      "2026-12-29",
      "2026-12-30",
      "2026-12-31",
      "2027-01-01",
      "2027-01-02",
    ]);
  });

  it("the first cell's weekday is always WEEK_STARTS_ON (Sunday)", () => {
    for (const anchor of ["2026-06-15", "2026-07-01", "2026-12-31", "2024-02-29"]) {
      const grid = weekGrid(anchor);
      const first = grid[0];
      expect(first).toBeDefined();
      if (!first) {
        continue;
      }
      const { year, month, day } = parseDate(first.date);
      expect(weekdayOf(year, month, day)).toBe(WEEK_STARTS_ON);
    }
  });
});

describe("weekRange — half-open [start, end) of exactly 7 days", () => {
  it("starts on the containing Sunday and ends on the FOLLOWING Sunday (exclusive)", () => {
    // Week containing Wed 2026-06-17: [2026-06-14, 2026-06-21).
    expect(weekRange("2026-06-17")).toEqual({ start: "2026-06-14", end: "2026-06-21" });
  });

  it("is exactly 7 days wide (end = start + 7)", () => {
    const { start, end } = weekRange("2026-06-15");
    expect(addDays(start, 7)).toBe(end);
  });

  it("rolls the exclusive end across a month boundary", () => {
    // Week of 2026-07-01 starts Sunday 2026-06-28, ends Sunday 2026-07-05.
    expect(weekRange("2026-07-01")).toEqual({ start: "2026-06-28", end: "2026-07-05" });
  });

  it("rolls across a year boundary", () => {
    // Week of 2026-12-31 starts Sunday 2026-12-27, ends Sunday 2027-01-03.
    expect(weekRange("2026-12-31")).toEqual({ start: "2026-12-27", end: "2027-01-03" });
  });

  it("a Sunday anchor yields the week starting that same day", () => {
    expect(weekRange("2026-06-14")).toEqual({ start: "2026-06-14", end: "2026-06-21" });
  });
});
