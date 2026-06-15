"use client";

import { EditEventPopover } from "@/components/calendar/EditEventPopover";
import { MiniMonth } from "@/components/calendar/MiniMonth";
import { MonthView } from "@/components/calendar/MonthView";
import { QuickCreatePopover } from "@/components/calendar/QuickCreatePopover";
import { WeekView } from "@/components/calendar/WeekView";
import { applyMove, rollbackMove } from "@/lib/calendar-dnd";
import {
  type CalendarState,
  calendarHref,
  groupByDate,
  rangeForState,
  seriesColor,
} from "@/lib/calendar-view";
import { addDays, addMonths, formatDate, monthsOfYear, weekRange } from "@/lib/dates";
import type {
  CalendarEvent,
  CalendarEventListResponse,
  DocumentFull,
  ProseMirrorDoc,
} from "@/lib/types";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Seed content for a brand-new sermon created from an empty calendar day — the
 * same minimal-but-valid ProseMirror/TipTap doc the standalone "New sermon"
 * button uses (web/components/NewSermonButton.tsx). One empty paragraph inside a
 * `doc` node; TipTap renders it as an empty editor with the placeholder showing.
 */
const SEED_CONTENT: ProseMirrorDoc = {
  type: "doc",
  content: [{ type: "paragraph" }],
};

/** Editor route for a linked manuscript. Mirrors web/components/SermonList. */
function sermonHref(documentId: string): string {
  return `/sermons/${encodeURIComponent(documentId)}`;
}

const MONTH_NAMES = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
] as const;

type LoadStatus = "loading" | "ready" | "error";

/** The active popover: none, "create on a day", or "edit an event". */
type Popover =
  | { kind: "none" }
  | { kind: "create"; date: string }
  | { kind: "edit"; event: CalendarEvent };

interface CalendarViewProps {
  state: CalendarState;
  /** Today's `YYYY-MM-DD` from the server, so first paint rings the right day. */
  todayStr: string;
}

/**
 * The calendar client island (Phase 39 read views + Phase 40 week view & CRUD).
 * All period/view state lives in the URL (`?view`/`?date`), so the view +
 * navigation are plain `<Link>`s; the only client state is the fetched events,
 * a refetch trigger, and which popover is open.
 *
 * It owns one data concern: a single half-open range fetch through the
 * same-origin `/api/sermon-events` proxy for the current state's range (a whole
 * year, a whole month, or a week — all one call). After any successful create /
 * edit / delete it bumps `version` to re-run the same fetch so the new state
 * shows up immediately in WHICHEVER view is active. A 401 bounces to /login.
 * Every await re-checks a mounted ref before touching state.
 */
export function CalendarView({ state, todayStr }: CalendarViewProps) {
  const router = useRouter();
  const [status, setStatus] = useState<LoadStatus>("loading");
  const [events, setEvents] = useState<readonly CalendarEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const [popover, setPopover] = useState<Popover>({ kind: "none" });
  // A NON-blocking error for a failed drag-to-reschedule (Phase 42). It is kept
  // SEPARATE from `status`/`error` on purpose: flipping `status` to "error"
  // would blank the whole grid, but a rollback must leave the snapped-back chip
  // ON SCREEN with the error visible beside it. Set on PATCH failure (after the
  // optimistic move is rolled back), cleared when the next move starts.
  const [moveError, setMoveError] = useState<string | null>(null);

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // Re-fetch whenever the derived range changes OR a mutation bumps `version`.
  // Depend on the primitive range bounds (strings) rather than the object so an
  // identical range never re-fires on its own.
  const { start, end } = rangeForState(state);
  // `version` is the deliberate refetch trigger: a successful create/edit/delete
  // bumps it to re-run this exact fetch so the mutation shows up in the active
  // view. It is intentionally a dependency though unused in the effect body.
  // biome-ignore lint/correctness/useExhaustiveDependencies: version is the refetch trigger
  useEffect(() => {
    setStatus("loading");
    setError(null);
    const params = new URLSearchParams({ start, end });
    fetch(`/api/sermon-events?${params.toString()}`, { cache: "no-store" })
      .then(async (res) => {
        if (!mounted.current) {
          return;
        }
        if (res.status === 401) {
          window.location.href = `/login?next=${encodeURIComponent("/calendar")}`;
          return;
        }
        if (!res.ok) {
          const data = (await res.json().catch(() => null)) as { error?: string } | null;
          setError(data?.error ?? "Could not load the calendar.");
          setStatus("error");
          return;
        }
        const data = (await res.json()) as CalendarEventListResponse;
        if (!mounted.current) {
          return;
        }
        setEvents(data.events);
        setStatus("ready");
      })
      .catch(() => {
        if (mounted.current) {
          setError("Network error. Please try again.");
          setStatus("error");
        }
      });
  }, [start, end, version]);

  const refetch = useCallback(() => {
    if (mounted.current) {
      setVersion((v) => v + 1);
    }
  }, []);

  const openCreate = useCallback((date: string) => setPopover({ kind: "create", date }), []);
  // A chip/card click branches on the link state (Phase 41): a LINKED event
  // opens its manuscript at /sermons/{document_id}; an UNLINKED event opens the
  // Phase 40 edit popover (where it can be linked). The views call this for
  // every event — the branch lives here, not in the view, so all three views
  // share one rule.
  const openEdit = useCallback(
    (event: CalendarEvent) => {
      if (event.document_id !== null) {
        router.push(sermonHref(event.document_id));
        return;
      }
      setPopover({ kind: "edit", event });
    },
    [router],
  );
  const closePopover = useCallback(() => setPopover({ kind: "none" }), []);

  /**
   * Drag-to-reschedule (Phase 42). Optimistically move the event to `toDate` in
   * local state so the chip jumps immediately in WHICHEVER view is active, then
   * fire EXACTLY ONE PATCH. On success the optimistic state already reflects the
   * move (no refetch — that would flash the loading state). On failure roll back
   * to the EXACT prior date (captured BEFORE the optimistic update) and surface a
   * non-blocking error so the snapped-back chip and the error are both visible.
   * A drop on the event's current day is a no-op: `applyMove` returns the same
   * array reference, so we skip both the state churn and the network call.
   */
  const onMove = useCallback(
    async (eventId: string, toDate: string) => {
      const prior = events.find((e) => e.event_id === eventId);
      // Capture the EXACT prior date now — rollback restores this, never a
      // recomputed value. No event found or same-day drop → no PATCH.
      if (!prior || prior.event_date === toDate) {
        return;
      }
      const priorDate = prior.event_date;

      setMoveError(null);
      setEvents((evs) => applyMove(evs, eventId, toDate));

      const message = await patchEvent(eventId, { event_date: toDate });
      if (!mounted.current) {
        return;
      }
      if (message !== null) {
        setEvents((evs) => rollbackMove(evs, eventId, priorDate));
        setMoveError(message);
      }
    },
    [events],
  );

  const eventsByDate = groupByDate(events);

  return (
    <section>
      <CalendarHeader state={state} />

      {status === "error" ? (
        <p role="alert" className="mb-4 text-red-600 text-sm">
          {error}
        </p>
      ) : null}

      {moveError ? (
        // A dismissible, NON-blocking banner for a failed drag-to-reschedule:
        // the grid stays visible (status is still "ready"), so the chip that
        // snapped back to its original day and this error show together.
        <div
          role="alert"
          className="mb-4 flex items-start justify-between gap-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-red-700 text-sm"
        >
          <span>{moveError}</span>
          <button
            type="button"
            onClick={() => setMoveError(null)}
            aria-label="Dismiss error"
            className="shrink-0 font-medium text-red-700 hover:text-red-900"
          >
            ✕
          </button>
        </div>
      ) : null}

      {status === "loading" ? (
        <output className="block rounded-lg border border-gray-200 p-8 text-center text-gray-500 text-sm">
          Loading your calendar…
        </output>
      ) : null}

      {status === "ready" && state.view === "year" ? (
        <div
          data-testid="calendar-year"
          className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4"
        >
          {monthsOfYear().map((month) => (
            <MiniMonth
              key={month}
              year={state.year}
              month={month}
              eventsByDate={eventsByDate}
              colorFor={seriesColor}
              todayStr={todayStr}
              onMove={onMove}
            />
          ))}
        </div>
      ) : null}

      {status === "ready" && state.view === "month" ? (
        <div data-testid="calendar-month">
          <MonthView
            year={state.year}
            month={state.month}
            eventsByDate={eventsByDate}
            colorFor={seriesColor}
            todayStr={todayStr}
            onCreate={openCreate}
            onEdit={openEdit}
            onMove={onMove}
          />
        </div>
      ) : null}

      {status === "ready" && state.view === "week" ? (
        <WeekView
          anchor={state.anchor}
          eventsByDate={eventsByDate}
          colorFor={seriesColor}
          todayStr={todayStr}
          onCreate={openCreate}
          onEdit={openEdit}
          onMove={onMove}
        />
      ) : null}

      {popover.kind === "create" ? (
        <QuickCreatePopover
          date={popover.date}
          onClose={closePopover}
          onSubmit={async (input) => {
            const message = await createEvent(input);
            if (message === null) {
              closePopover();
              refetch();
            }
            return message;
          }}
          onCreateDraft={async (input) => {
            // Create-from-date (Phase 41): make the event, create a draft sermon,
            // link the event to it, then open the editor. Two existing endpoints
            // (POST /documents + PATCH the event's document_id) — no new route.
            // On any step's failure the message surfaces inline and we DON'T
            // navigate; a partial state (event created, link missing) is recoverable
            // from the calendar (the event shows unlinked).
            const message = await createDraftForDate(input);
            if (typeof message === "string") {
              return message;
            }
            router.push(sermonHref(message.documentId));
            return null;
          }}
        />
      ) : null}

      {popover.kind === "edit" ? (
        <EditEventPopover
          event={popover.event}
          onClose={closePopover}
          onSave={async (input) => {
            const message = await patchEvent(popover.event.event_id, input);
            if (message === null) {
              closePopover();
              refetch();
            }
            return message;
          }}
          onDelete={async () => {
            const message = await deleteEvent(popover.event.event_id);
            if (message === null) {
              closePopover();
              refetch();
            }
            return message;
          }}
        />
      ) : null}
    </section>
  );
}

/**
 * Read a FastAPI-or-proxy error body. The proxy passes the API's 422 through
 * byte-for-byte (`{detail}`); its own structural rejections are `{error}`. Try
 * both so the materializer-cap / range message reaches the popover verbatim.
 */
async function errorMessage(res: Response, fallback: string): Promise<string> {
  const data = (await res.json().catch(() => null)) as { detail?: unknown; error?: unknown } | null;
  if (data && typeof data.detail === "string") {
    return data.detail;
  }
  if (data && typeof data.error === "string") {
    return data.error;
  }
  return fallback;
}

/** POST a create (one row or a whole weekly run). Returns null on success. */
async function createEvent(input: {
  event_date: string;
  title: string;
  series: string | null;
  repeat_weekly_until: string | null;
}): Promise<string | null> {
  try {
    const res = await fetch("/api/sermon-events", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(input),
      cache: "no-store",
    });
    if (res.ok) {
      return null;
    }
    return await errorMessage(res, "Could not create the event.");
  } catch {
    return "Network error. Please try again.";
  }
}

/**
 * PATCH an edit (title / series / event_date plus the optional three-state
 * `document_id`). The input object is passed VERBATIM to the proxy whitelist —
 * a present `document_id: null` UNLINKS, a present string RE-LINKS, an absent
 * key leaves the link alone. The proxy forwards a non-null `document_id` to the
 * API, which ownership-checks it (Phase 38) and returns a no-oracle 404 on a
 * cross-tenant/nonexistent id; that 404 (and the 422) surfaces here as the
 * error string the popover renders. Returns null on success.
 */
async function patchEvent(
  eventId: string,
  input: {
    event_date?: string;
    title?: string;
    series?: string | null;
    document_id?: string | null;
  },
): Promise<string | null> {
  try {
    const res = await fetch(`/api/sermon-events/${encodeURIComponent(eventId)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(input),
      cache: "no-store",
    });
    if (res.ok) {
      return null;
    }
    return await errorMessage(res, "Could not save the event.");
  } catch {
    return "Network error. Please try again.";
  }
}

/**
 * Create-doc-from-empty-date (Phase 41). Three steps over EXISTING endpoints:
 *   1. POST /api/sermon-events to create the event (the proxy drops document_id
 *      on create — link is a separate PATCH);
 *   2. POST /api/documents to create a draft sermon seeded with an empty doc,
 *      titled after the event;
 *   3. PATCH the new event's document_id to the new doc id.
 * Returns `{ documentId }` on success (the caller navigates into the editor) or
 * a human-readable error string on any step's failure. There is no rollback: a
 * created-but-unlinked event simply shows unlinked in the calendar, recoverable
 * from the edit popover's picker.
 */
async function createDraftForDate(input: {
  event_date: string;
  title: string;
  series: string | null;
  repeat_weekly_until: string | null;
}): Promise<{ documentId: string } | string> {
  let eventId: string;
  try {
    const res = await fetch("/api/sermon-events", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(input),
      cache: "no-store",
    });
    if (!res.ok) {
      return await errorMessage(res, "Could not create the event.");
    }
    const data = (await res.json()) as CalendarEventListResponse;
    const created = data.events[0];
    if (!created) {
      return "Could not create the event.";
    }
    eventId = created.event_id;
  } catch {
    return "Network error. Please try again.";
  }

  let documentId: string;
  try {
    const res = await fetch("/api/documents", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ title: input.title, content: SEED_CONTENT }),
      cache: "no-store",
    });
    if (!res.ok) {
      return await errorMessage(res, "Could not create the sermon draft.");
    }
    const doc = (await res.json()) as DocumentFull;
    documentId = doc.document_id;
  } catch {
    return "Network error. Please try again.";
  }

  const linkError = await patchEvent(eventId, { document_id: documentId });
  if (linkError !== null) {
    return linkError;
  }
  return { documentId };
}

/** DELETE an event. Returns null on success (204). */
async function deleteEvent(eventId: string): Promise<string | null> {
  try {
    const res = await fetch(`/api/sermon-events/${encodeURIComponent(eventId)}`, {
      method: "DELETE",
      cache: "no-store",
    });
    if (res.ok) {
      return null;
    }
    return await errorMessage(res, "Could not delete the event.");
  } catch {
    return "Network error. Please try again.";
  }
}

/** The title + view toggle + period navigation, all URL links. */
function CalendarHeader({ state }: { state: CalendarState }) {
  const monthName = MONTH_NAMES[state.month - 1] ?? "";
  const heading =
    state.view === "year"
      ? `${state.year}`
      : state.view === "month"
        ? `${monthName} ${state.year}`
        : weekHeading(state.anchor);

  // Prev/next step by a year, a month, or 7 days depending on the view.
  const prev = stepHref(state, -1);
  const next = stepHref(state, 1);

  // Keep the same anchor when flipping views so year↔month↔week is a smooth pivot.
  const yearHref = calendarHref("year", formatDate(state.year, state.month, 1));
  const monthViewHref = calendarHref("month", formatDate(state.year, state.month, 1));
  const weekViewHref = calendarHref("week", state.anchor);

  return (
    <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
      <div className="flex items-center gap-2">
        <Link
          href={prev}
          aria-label={prevNextLabel(state.view, "Previous")}
          className="rounded border border-gray-300 px-2 py-1 text-sm hover:bg-gray-50"
        >
          ←
        </Link>
        <h1 className="font-semibold text-xl">{heading}</h1>
        <Link
          href={next}
          aria-label={prevNextLabel(state.view, "Next")}
          className="rounded border border-gray-300 px-2 py-1 text-sm hover:bg-gray-50"
        >
          →
        </Link>
      </div>
      <div className="inline-flex overflow-hidden rounded border border-gray-300 text-sm">
        <Link
          href={yearHref}
          aria-current={state.view === "year" ? "page" : undefined}
          className={`px-3 py-1 ${state.view === "year" ? "bg-black text-white" : "hover:bg-gray-50"}`}
        >
          Year
        </Link>
        <Link
          href={monthViewHref}
          aria-current={state.view === "month" ? "page" : undefined}
          className={`border-gray-300 border-l px-3 py-1 ${
            state.view === "month" ? "bg-black text-white" : "hover:bg-gray-50"
          }`}
        >
          Month
        </Link>
        <Link
          href={weekViewHref}
          aria-current={state.view === "week" ? "page" : undefined}
          className={`border-gray-300 border-l px-3 py-1 ${
            state.view === "week" ? "bg-black text-white" : "hover:bg-gray-50"
          }`}
        >
          Week
        </Link>
      </div>
    </div>
  );
}

/** The prev/next aria-label for the active view's step unit. */
function prevNextLabel(view: CalendarState["view"], dir: "Previous" | "Next"): string {
  const unit = view === "year" ? "year" : view === "month" ? "month" : "week";
  return `${dir} ${unit}`;
}

/** The prev/next href, stepped by the active view's unit (year / month / week). */
function stepHref(state: CalendarState, delta: number): string {
  if (state.view === "year") {
    return calendarHref("year", formatDate(state.year + delta, state.month, 1));
  }
  if (state.view === "month") {
    const next = addMonths(state.year, state.month, delta);
    return calendarHref("month", formatDate(next.year, next.month, 1));
  }
  // Week: step by 7 days from the anchor (pure string arithmetic, TZ-immune).
  return calendarHref("week", addDays(state.anchor, delta * 7));
}

/** "Sun MM/DD – Sat MM/DD"-style heading for the week containing `anchor`. */
function weekHeading(anchor: string): string {
  const { start, end } = weekRange(anchor);
  const last = addDays(end, -1);
  return `${start} – ${last}`;
}
