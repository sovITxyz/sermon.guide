import { describe, expect, it } from "vitest";
import { DRAG_MIME, applyMove, rollbackMove } from "../lib/calendar-dnd";
import type { CalendarEvent } from "../lib/types";

/** A minimal CalendarEvent factory — only the fields these helpers touch. */
function event(eventId: string, eventDate: string): CalendarEvent {
  return {
    event_id: eventId,
    event_date: eventDate,
    title: `Sermon ${eventId}`,
    series: null,
    document_id: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

describe("DRAG_MIME", () => {
  it("is the broadly-supported text/plain channel", () => {
    expect(DRAG_MIME).toBe("text/plain");
  });
});

describe("applyMove", () => {
  it("moves only the targeted event's date and leaves the rest untouched", () => {
    const a = event("a", "2031-03-01");
    const b = event("b", "2031-03-02");
    const c = event("c", "2031-03-03");
    const next = applyMove([a, b, c], "b", "2031-03-10");

    expect(next.map((e) => [e.event_id, e.event_date])).toEqual([
      ["a", "2031-03-01"],
      ["b", "2031-03-10"],
      ["c", "2031-03-03"],
    ]);
  });

  it("returns a new array but reuses the unchanged events by identity", () => {
    const a = event("a", "2031-03-01");
    const b = event("b", "2031-03-02");
    const next = applyMove([a, b], "b", "2031-03-10");

    expect(next).not.toBe([a, b]);
    // Unchanged event is the same reference; only the moved one is a new object.
    expect(next[0]).toBe(a);
    expect(next[1]).not.toBe(b);
  });

  it("preserves array order when moving", () => {
    const a = event("a", "2031-03-01");
    const b = event("b", "2031-03-02");
    const c = event("c", "2031-03-03");
    const next = applyMove([a, b, c], "a", "2031-03-31");
    expect(next.map((e) => e.event_id)).toEqual(["a", "b", "c"]);
  });

  it("is a no-op (same reference) when dropped on the same date", () => {
    const a = event("a", "2031-03-01");
    const b = event("b", "2031-03-02");
    const input = [a, b] as const;
    const next = applyMove(input, "a", "2031-03-01");
    // Same reference back so the caller can early-out and skip the PATCH.
    expect(next).toBe(input);
  });

  it("is a no-op (same reference) when the event id is not present", () => {
    const a = event("a", "2031-03-01");
    const input = [a] as const;
    expect(applyMove(input, "missing", "2031-03-10")).toBe(input);
  });

  it("does not mutate the input array or its events", () => {
    const a = event("a", "2031-03-01");
    const b = event("b", "2031-03-02");
    const input = [a, b];
    applyMove(input, "b", "2031-03-10");
    expect(a.event_date).toBe("2031-03-01");
    expect(b.event_date).toBe("2031-03-02");
    expect(input.map((e) => e.event_date)).toEqual(["2031-03-01", "2031-03-02"]);
  });
});

describe("rollbackMove", () => {
  it("restores exactly the prior date for the targeted event only", () => {
    const a = event("a", "2031-03-01");
    // b was optimistically moved to 03-10; roll it back to its captured prior date.
    const movedB = event("b", "2031-03-10");
    const c = event("c", "2031-03-03");
    const next = rollbackMove([a, movedB, c], "b", "2031-03-02");

    expect(next.map((e) => [e.event_id, e.event_date])).toEqual([
      ["a", "2031-03-01"],
      ["b", "2031-03-02"],
      ["c", "2031-03-03"],
    ]);
  });

  it("round-trips with applyMove back to the exact original date", () => {
    const a = event("a", "2031-03-01");
    const b = event("b", "2031-03-02");
    const moved = applyMove([a, b], "b", "2031-03-20");
    const restored = rollbackMove(moved, "b", "2031-03-02");
    expect(restored.find((e) => e.event_id === "b")?.event_date).toBe("2031-03-02");
  });

  it("returns the input unchanged when the event id is not present", () => {
    const a = event("a", "2031-03-01");
    const input = [a] as const;
    expect(rollbackMove(input, "missing", "2031-03-02")).toBe(input);
  });

  it("does not mutate the input events", () => {
    const moved = event("b", "2031-03-10");
    rollbackMove([moved], "b", "2031-03-02");
    expect(moved.event_date).toBe("2031-03-10");
  });
});
