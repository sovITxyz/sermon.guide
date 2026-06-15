import { CalendarView } from "@/components/calendar/CalendarView";
import { parseCalendarState } from "@/lib/calendar-view";
import { today } from "@/lib/dates";

interface CalendarPageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

/** Read the first value of a possibly-repeated query param. */
function firstParam(value: string | string[] | undefined): string | null {
  if (Array.isArray(value)) {
    return value[0] ?? null;
  }
  return value ?? null;
}

/**
 * The sermon calendar (Phase 39, read-only). All state lives in the URL —
 * `?view=year|month&date=YYYY-MM-DD` — so the page is linkable/deep-linkable
 * and a shared link opens the exact view + period. The server normalizes the
 * (possibly malformed) query into a {@link CalendarState} and computes the
 * single canonical "today" once, then hands both to the client island, which
 * owns the one range fetch that drives whichever view is active.
 *
 * Auth: the route is gated by middleware.ts (the `/calendar/:path*` matcher
 * bounces an unauthenticated request to /login before this renders), and the
 * client island re-bounces on a mid-session 401 from the proxy. So this server
 * component itself fetches nothing and needs no try/redirect.
 */
export default async function CalendarPage({ searchParams }: CalendarPageProps) {
  const params = await searchParams;
  const state = parseCalendarState(firstParam(params.view), firstParam(params.date));
  // Compute "today" on the server so first paint rings the right day without a
  // client/server hydration mismatch.
  const todayStr = today();

  return <CalendarView state={state} todayStr={todayStr} />;
}
