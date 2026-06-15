import { describe, expect, it } from "vitest";
import {
  calendarHref,
  groupByDate,
  parseCalendarState,
  rangeForState,
  seriesColor,
} from "../lib/calendar-view";
import type { CalendarEvent } from "../lib/types";

/** A minimal CalendarEvent factory — only the fields these helpers read. */
function event(eventDate: string, overrides: Partial<CalendarEvent> = {}): CalendarEvent {
  return {
    event_id: overrides.event_id ?? `id-${eventDate}-${overrides.title ?? ""}`,
    event_date: eventDate,
    title: overrides.title ?? "Sermon",
    series: overrides.series ?? null,
    document_id: overrides.document_id ?? null,
    created_at: overrides.created_at ?? "2026-01-01T00:00:00Z",
    updated_at: overrides.updated_at ?? "2026-01-01T00:00:00Z",
  };
}

// A fixed "now" so the today()-fallback paths are deterministic.
const NOW = new Date(2026, 5, 15, 23, 30); // local 2026-06-15, late evening

describe("parseCalendarState", () => {
  it("defaults to the year view and today when both params are absent", () => {
    const state = parseCalendarState(null, null, NOW);
    expect(state.view).toBe("year");
    expect(state.anchor).toBe("2026-06-15");
    expect(state.year).toBe(2026);
    expect(state.month).toBe(6);
  });

  it("honors view=month and a valid date", () => {
    const state = parseCalendarState("month", "2026-03-15", NOW);
    expect(state.view).toBe("month");
    expect(state.anchor).toBe("2026-03-15");
    expect(state.month).toBe(3);
  });

  it("honors view=week and keeps the clicked day as the anchor", () => {
    const state = parseCalendarState("week", "2026-03-18", NOW);
    expect(state.view).toBe("week");
    // The anchor stays the exact clicked day; rangeForState Sunday-aligns it.
    expect(state.anchor).toBe("2026-03-18");
  });

  it("falls back to year for an unknown/empty view", () => {
    expect(parseCalendarState("decade", "2026-03-15", NOW).view).toBe("year");
    expect(parseCalendarState("", "2026-03-15", NOW).view).toBe("year");
  });

  it("falls back to today for a malformed date instead of throwing", () => {
    expect(parseCalendarState("month", "garbage", NOW).anchor).toBe("2026-06-15");
    expect(parseCalendarState("month", "2026-3-5", NOW).anchor).toBe("2026-06-15");
  });

  it("falls back to today for a structurally-valid but impossible date", () => {
    // parseDate only checks the YYYY-MM-DD shape — month/day range is this
    // layer's job, so a 13th month or a 99th day must NOT drive the grid.
    expect(parseCalendarState("year", "2026-13-99", NOW).anchor).toBe("2026-06-15");
    expect(parseCalendarState("month", "2026-02-30", NOW).anchor).toBe("2026-06-15");
    expect(parseCalendarState("month", "2023-02-29", NOW).anchor).toBe("2026-06-15"); // non-leap
    expect(parseCalendarState("month", "2026-00-10", NOW).anchor).toBe("2026-06-15"); // month 0
  });

  it("canonicalizes the anchor from the parsed parts", () => {
    // A structurally-valid YYYY-MM-DD is preserved verbatim.
    expect(parseCalendarState("month", "2024-02-29", NOW).anchor).toBe("2024-02-29");
  });
});

describe("rangeForState", () => {
  it("spans the whole anchor year for the year view (half-open)", () => {
    const range = rangeForState(parseCalendarState("year", "2026-06-15", NOW));
    expect(range).toEqual({ start: "2026-01-01", end: "2027-01-01" });
  });

  it("spans the whole anchor month for the month view (half-open)", () => {
    const range = rangeForState(parseCalendarState("month", "2026-03-10", NOW));
    expect(range).toEqual({ start: "2026-03-01", end: "2026-04-01" });
  });

  it("rolls a December month range into the next January", () => {
    const range = rangeForState(parseCalendarState("month", "2026-12-25", NOW));
    expect(range).toEqual({ start: "2026-12-01", end: "2027-01-01" });
  });

  it("spans the Sunday-aligned seven days of the anchor's week (half-open)", () => {
    // 2026-03-18 is a Wednesday → the week is Sun 2026-03-15 .. Sat 2026-03-21,
    // half-open end is the following Sunday 2026-03-22.
    const range = rangeForState(parseCalendarState("week", "2026-03-18", NOW));
    expect(range).toEqual({ start: "2026-03-15", end: "2026-03-22" });
  });

  it("rolls a week range across a month boundary", () => {
    // 2026-04-01 is a Wednesday → week Sun 2026-03-29 .. Sat 2026-04-04.
    const range = rangeForState(parseCalendarState("week", "2026-04-01", NOW));
    expect(range).toEqual({ start: "2026-03-29", end: "2026-04-05" });
  });
});

describe("groupByDate", () => {
  it("buckets events by event_date and preserves input order within a day", () => {
    const a = event("2026-03-01", { title: "First" });
    const b = event("2026-03-01", { title: "Second" });
    const c = event("2026-03-02", { title: "Third" });
    const grouped = groupByDate([a, b, c]);
    expect(grouped.get("2026-03-01")?.map((e) => e.title)).toEqual(["First", "Second"]);
    expect(grouped.get("2026-03-02")?.map((e) => e.title)).toEqual(["Third"]);
    expect(grouped.has("2026-03-03")).toBe(false);
  });

  it("returns an empty map for no events", () => {
    expect(groupByDate([]).size).toBe(0);
  });
});

describe("seriesColor", () => {
  it("is deterministic: the same series always maps to the same color", () => {
    expect(seriesColor("Advent")).toEqual(seriesColor("Advent"));
    expect(seriesColor("Romans")).toEqual(seriesColor("Romans"));
  });

  it("gives a neutral gray to null and empty series", () => {
    expect(seriesColor(null).dot).toBe("bg-gray-400");
    expect(seriesColor("").dot).toBe("bg-gray-400");
  });

  it("returns static (non-interpolated) Tailwind class strings", () => {
    const color = seriesColor("Advent");
    expect(color.bg).toMatch(/^bg-/);
    expect(color.text).toMatch(/^text-/);
    expect(color.dot).toMatch(/^bg-/);
  });
});

describe("calendarHref", () => {
  it("builds a normalized linkable href", () => {
    expect(calendarHref("month", "2026-03-15")).toBe("/calendar?view=month&date=2026-03-15");
    expect(calendarHref("year", "2026-01-01")).toBe("/calendar?view=year&date=2026-01-01");
  });
});
