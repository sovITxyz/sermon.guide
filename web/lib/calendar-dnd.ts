/**
 * Pure (no-DOM, no-fetch) helpers for calendar drag-to-reschedule (Phase 42).
 *
 * Dragging an event chip onto another day reschedules it. CalendarView owns the
 * event list and applies an OPTIMISTIC move on drop, then fires exactly one
 * PATCH; on failure it rolls the move back. Both the optimistic apply and the
 * rollback are pure array transforms here so they are unit-testable without a
 * browser and so the "move only that event", "same-date is a no-op", and
 * "rollback restores the exact prior date" invariants are pinned in vitest.
 *
 * The MIME type used to carry the dragged event's id through the native HTML5
 * DataTransfer. `text/plain` is the most broadly-supported channel and is all
 * we need — the payload is just the event_id.
 */

import type { CalendarEvent } from "@/lib/types";

/** The DataTransfer MIME type the drag payload (the event_id) travels on. */
export const DRAG_MIME = "text/plain";

/**
 * Return a NEW events array with `eventId` moved to `newDate`. Only the matching
 * event's `event_date` changes; every other event is returned by identity, and
 * the array order is preserved (so a moved chip keeps its position in its day's
 * server-defined sequence relative to the rest). A move to the date the event
 * already has, or an `eventId` not present, returns the input array UNCHANGED by
 * reference — the caller uses that identity to early-out and skip the PATCH so a
 * same-day drop fires no network call.
 */
export function applyMove(
  events: readonly CalendarEvent[],
  eventId: string,
  newDate: string,
): readonly CalendarEvent[] {
  const target = events.find((e) => e.event_id === eventId);
  if (!target || target.event_date === newDate) {
    return events;
  }
  return events.map((e) => (e.event_id === eventId ? { ...e, event_date: newDate } : e));
}

/**
 * Return a NEW events array with `eventId`'s `event_date` restored to
 * `priorDate` — the rollback after a failed PATCH. `priorDate` must be the EXACT
 * date the event held before {@link applyMove} (captured by the caller BEFORE
 * the optimistic update, never recomputed). Same identity-preserving shape as
 * {@link applyMove}: only the one event changes, order is kept, and an absent
 * id returns the input unchanged.
 */
export function rollbackMove(
  events: readonly CalendarEvent[],
  eventId: string,
  priorDate: string,
): readonly CalendarEvent[] {
  if (!events.some((e) => e.event_id === eventId)) {
    return events;
  }
  return events.map((e) => (e.event_id === eventId ? { ...e, event_date: priorDate } : e));
}
