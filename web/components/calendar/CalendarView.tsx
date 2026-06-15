"use client";

import { MiniMonth } from "@/components/calendar/MiniMonth";
import { MonthView } from "@/components/calendar/MonthView";
import {
  type CalendarState,
  calendarHref,
  groupByDate,
  rangeForState,
  seriesColor,
} from "@/lib/calendar-view";
import { addMonths, formatDate, monthsOfYear } from "@/lib/dates";
import type { CalendarEvent, CalendarEventListResponse } from "@/lib/types";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

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

interface CalendarViewProps {
  state: CalendarState;
  /** Today's `YYYY-MM-DD` from the server, so first paint rings the right day. */
  todayStr: string;
}

/**
 * The calendar client island (Phase 39). All page state lives in the URL
 * (`?view`/`?date`), so this component is driven entirely by the `state` prop
 * the server page derived; the view/period switchers are plain `<Link>`s that
 * change the URL (linkable + deep-linkable, no client routing state to drift).
 *
 * It owns exactly ONE data concern: a single half-open range fetch through the
 * same-origin `/api/sermon-events` proxy for the current state's range (a whole
 * year or a whole month — both one call). The fetched events are grouped by day
 * and handed to the presentational MiniMonth grid (year) or MonthView (month).
 * A 401 bounces to /login (the cookie expired mid-session); other failures show
 * an inline error. Every await re-checks a mounted ref before touching state,
 * since a fast view-switch can outlive a request.
 */
export function CalendarView({ state, todayStr }: CalendarViewProps) {
  const [status, setStatus] = useState<LoadStatus>("loading");
  const [events, setEvents] = useState<readonly CalendarEvent[]>([]);
  const [error, setError] = useState<string | null>(null);

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // Re-fetch whenever the derived range changes. Depend on the primitive range
  // bounds (strings) rather than the object so an identical range never re-fires.
  const { start, end } = rangeForState(state);
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
          // The session cookie expired since the page rendered — re-auth.
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
  }, [start, end]);

  const eventsByDate = groupByDate(events);

  return (
    <section>
      <CalendarHeader state={state} />

      {status === "error" ? (
        <p role="alert" className="mb-4 text-red-600 text-sm">
          {error}
        </p>
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
          />
        </div>
      ) : null}
    </section>
  );
}

/** The title + view toggle + period navigation, all URL links. */
function CalendarHeader({ state }: { state: CalendarState }) {
  const monthName = MONTH_NAMES[state.month - 1] ?? "";
  const heading = state.view === "year" ? `${state.year}` : `${monthName} ${state.year}`;

  // Prev/next step by a year (year view) or a month (month view).
  const prev =
    state.view === "year"
      ? calendarHref("year", formatDate(state.year - 1, state.month, 1))
      : monthHref(state.year, state.month, -1);
  const next =
    state.view === "year"
      ? calendarHref("year", formatDate(state.year + 1, state.month, 1))
      : monthHref(state.year, state.month, 1);

  // Keep the same anchor when flipping views so year↔month is a smooth pivot.
  const yearHref = calendarHref("year", formatDate(state.year, state.month, 1));
  const monthViewHref = calendarHref("month", formatDate(state.year, state.month, 1));

  return (
    <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
      <div className="flex items-center gap-2">
        <Link
          href={prev}
          aria-label={state.view === "year" ? "Previous year" : "Previous month"}
          className="rounded border border-gray-300 px-2 py-1 text-sm hover:bg-gray-50"
        >
          ←
        </Link>
        <h1 className="font-semibold text-xl">{heading}</h1>
        <Link
          href={next}
          aria-label={state.view === "year" ? "Next year" : "Next month"}
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
      </div>
    </div>
  );
}

/** A month-view href stepped `delta` months from `(year, month)`, normalized. */
function monthHref(year: number, month: number, delta: number): string {
  const next = addMonths(year, month, delta);
  return calendarHref("month", formatDate(next.year, next.month, 1));
}
