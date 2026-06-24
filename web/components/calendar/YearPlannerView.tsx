import { type seriesColor as SeriesColorFn, calendarHref } from "@/lib/calendar-view";
import { type YearPlannerRow, yearGrid } from "@/lib/dates";
import type { CalendarEvent } from "@/lib/types";
import Link from "next/link";

/** Three-letter month labels for the narrow sticky row header. */
const MONTH_ABBR = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
] as const;

/** Full month names — the accessible label for the row header + month drill-down. */
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

/** The 31 day-of-month column numbers `[1 … 31]` for the header row. */
const DAY_COLUMNS = Array.from({ length: 31 }, (_, i) => i + 1);

/** Up to this many series dots render per day before collapsing to "+N". */
const MAX_DOTS = 2;

interface YearPlannerViewProps {
  year: number;
  /** Events grouped by `event_date` (`YYYY-MM-DD`) for the whole year. */
  eventsByDate: Map<string, CalendarEvent[]>;
  /** The deterministic series→color mapper from lib/calendar-view. */
  colorFor: typeof SeriesColorFn;
  /** Today's `YYYY-MM-DD`, to ring the current day. */
  todayStr: string;
}

/**
 * The horizontal "spreadsheet" year layout (the alternative to the twelve-mini-
 * month grid): one big table with the 12 months as ROWS (Jan → Dec) and the
 * days-of-month 1–31 as COLUMNS. Each real day is a small cell; days that do not
 * exist in a month (Feb 30/31, the 31st of a 30-day month) are inert blocked
 * cells. A day that HAS events shows up to {@link MAX_DOTS} series-colored dots
 * (plus a "+N" overflow hint) and is a drill-down link into that day's WEEK view
 * — the year is an at-a-glance overview, so acting on events happens one level
 * down (mirroring the mini-month layout, which is also read-only). Weekends are
 * faintly shaded and today is ringed for scanning.
 *
 * Built from the pure {@link yearGrid} so the rectangular months-down/days-across
 * shape and the blocked-cell placement are timezone-immune and vitest-pinned. A
 * semantic `<table>` gives screen readers real column (day) and row (month)
 * headers; the header row and month column are sticky so they stay visible while
 * the body scrolls horizontally on narrow screens. Event titles render as plain
 * text only (in the link's `aria-label`/`title`) — never `dangerouslySetInnerHTML`.
 */
export function YearPlannerView({ year, eventsByDate, colorFor, todayStr }: YearPlannerViewProps) {
  const rows = yearGrid(year);

  return (
    <div
      data-testid="calendar-year-planner"
      className="overflow-x-auto rounded-lg border border-gray-200"
    >
      <table
        aria-label={`${year} sermon planner`}
        className="w-full min-w-[52rem] border-separate border-spacing-0 text-center"
      >
        <thead>
          <tr>
            <th
              scope="col"
              className="sticky top-0 left-0 z-30 border-gray-200 border-r border-b bg-gray-50 px-2 py-1 text-left font-medium text-[10px] text-gray-400"
            >
              <span className="sr-only">Month</span>
              <span aria-hidden="true">{year}</span>
            </th>
            {DAY_COLUMNS.map((day) => (
              <th
                scope="col"
                key={day}
                className="sticky top-0 z-20 border-gray-200 border-r border-b bg-gray-50 py-1 font-medium text-[10px] text-gray-400"
              >
                {day}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <PlannerRow
              key={row.month}
              row={row}
              year={year}
              eventsByDate={eventsByDate}
              colorFor={colorFor}
              todayStr={todayStr}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** One month-row: a sticky month label that drills into the month view + 31 day cells. */
function PlannerRow({
  row,
  year,
  eventsByDate,
  colorFor,
  todayStr,
}: {
  row: YearPlannerRow;
  year: number;
  eventsByDate: Map<string, CalendarEvent[]>;
  colorFor: typeof SeriesColorFn;
  todayStr: string;
}) {
  const monthName = MONTH_NAMES[row.month - 1] ?? "";
  const monthAnchor = `${year}-${String(row.month).padStart(2, "0")}-01`;

  return (
    <tr>
      <th
        scope="row"
        className="sticky left-0 z-10 border-gray-100 border-r border-b bg-white px-2 py-1 text-left font-medium text-gray-700 text-xs"
      >
        <Link href={calendarHref("month", monthAnchor)} className="hover:underline">
          <span aria-hidden="true">{MONTH_ABBR[row.month - 1]}</span>
          <span className="sr-only">{`${monthName} ${year}`}</span>
        </Link>
      </th>
      {row.cells.map((cell, i) => {
        const day = i + 1;
        const events = cell ? (eventsByDate.get(cell.date) ?? []) : [];
        const isToday = cell !== null && cell.date === todayStr;
        return (
          <PlannerDay
            key={cell?.date ?? `${row.month}-${day}`}
            cell={cell}
            events={events}
            isToday={isToday}
            colorFor={colorFor}
          />
        );
      })}
    </tr>
  );
}

/**
 * One day cell. Three shapes: a blocked cell (the day does not exist in the
 * month), an empty in-month cell (weekend-shaded / today-ringed but inert, so
 * the planner has no sea of focusable empty links), and an event cell (a
 * drill-to-week link showing series dots). Visual state (weekend, today) lives
 * on the `<td>`; only event cells carry an interactive `<Link>`.
 */
function PlannerDay({
  cell,
  events,
  isToday,
  colorFor,
}: {
  cell: YearPlannerRow["cells"][number];
  events: CalendarEvent[];
  isToday: boolean;
  colorFor: typeof SeriesColorFn;
}) {
  if (cell === null) {
    // The day does not exist in this month — an inert, visually-blocked cell.
    return <td className="h-6 border-gray-100 border-r border-b bg-gray-100" />;
  }

  const tdClass = `h-6 border-gray-100 border-r border-b p-0 ${
    cell.weekend ? "bg-gray-50" : "bg-white"
  } ${isToday ? "ring-1 ring-blue-500 ring-inset" : ""}`;

  // `aria-current="date"` marks today programmatically (the ring is color-only).
  const todayProps = isToday ? ({ "aria-current": "date" } as const) : {};

  if (events.length === 0) {
    return <td className={tdClass} {...todayProps} />;
  }

  const dots = events.slice(0, MAX_DOTS);
  const overflow = events.length - dots.length;
  const titles = events.map((event) => event.title).join(", ");
  const todaySuffix = isToday ? " (today)" : "";

  return (
    <td className={tdClass} {...todayProps}>
      <Link
        href={calendarHref("week", cell.date)}
        data-date={cell.date}
        data-has-events="true"
        aria-label={`${cell.date}, ${events.length} event${events.length === 1 ? "" : "s"}: ${titles}${todaySuffix}`}
        title={titles}
        className="flex h-full w-full items-center justify-center gap-0.5 hover:bg-blue-50"
      >
        {dots.map((event) => (
          <span
            key={event.event_id}
            className={`inline-block h-1.5 w-1.5 rounded-full ${colorFor(event.series).dot}`}
            aria-hidden="true"
          />
        ))}
        {overflow > 0 ? <span className="text-[8px] text-gray-500">+{overflow}</span> : null}
      </Link>
    </td>
  );
}
