import { type seriesColor as SeriesColorFn, calendarHref } from "@/lib/calendar-view";
import { monthGrid } from "@/lib/dates";
import type { CalendarEvent } from "@/lib/types";
import Link from "next/link";

/** Sunday-first one-letter weekday headers (week starts Sunday — settled). */
const WEEKDAY_INITIALS = ["S", "M", "T", "W", "T", "F", "S"] as const;
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

/** Up to this many series dots render per day in the dense year view. */
const MAX_DOTS = 2;

interface MiniMonthProps {
  year: number;
  /** 1-based month (1 = January). */
  month: number;
  /** Events grouped by `event_date` (`YYYY-MM-DD`) for the whole year. */
  eventsByDate: Map<string, CalendarEvent[]>;
  /** The deterministic series→color mapper from lib/calendar-view. */
  colorFor: typeof SeriesColorFn;
  /** Today's `YYYY-MM-DD`, to ring the current day. */
  todayStr: string;
}

/**
 * One month in the year wall-planner. A `grid-cols-7` of compact day boxes;
 * each day that has events shows up to {@link MAX_DOTS} series-colored dots
 * (plus a "+N" overflow hint) and is wrapped in a `<details>` popover so the
 * full, plain-text event list is reachable on click at this small size — no
 * hover-only affordance, no `dangerouslySetInnerHTML`. The whole month title is
 * a link that drills into the month view.
 */
export function MiniMonth({ year, month, eventsByDate, colorFor, todayStr }: MiniMonthProps) {
  const weeks = monthGrid(year, month);
  const monthName = MONTH_NAMES[month - 1] ?? "";
  const monthAnchor = `${year}-${String(month).padStart(2, "0")}-01`;

  return (
    <section className="rounded-lg border border-gray-200 p-2" aria-label={`${monthName} ${year}`}>
      <h3 className="mb-1 px-1 font-medium text-sm">
        <Link href={calendarHref("month", monthAnchor)} className="hover:underline">
          {monthName}
        </Link>
      </h3>
      <div className="grid grid-cols-7 gap-px text-center text-[10px] text-gray-400">
        {WEEKDAY_INITIALS.map((initial, i) => (
          // Static 7-element header — index key is stable and safe here.
          // biome-ignore lint/suspicious/noArrayIndexKey: fixed weekday header
          <div key={i} aria-hidden="true">
            {initial}
          </div>
        ))}
      </div>
      <div className="grid grid-cols-7 gap-px">
        {weeks.map((week) => (
          <MiniWeek
            key={week[0]?.date ?? Math.random()}
            week={week}
            eventsByDate={eventsByDate}
            colorFor={colorFor}
            todayStr={todayStr}
          />
        ))}
      </div>
    </section>
  );
}

/** One row of the mini month — kept separate so each cell's key is its date. */
function MiniWeek({
  week,
  eventsByDate,
  colorFor,
  todayStr,
}: {
  week: ReturnType<typeof monthGrid>[number];
  eventsByDate: Map<string, CalendarEvent[]>;
  colorFor: typeof SeriesColorFn;
  todayStr: string;
}) {
  return (
    <>
      {week.map((cell) => {
        const events = cell.inMonth ? (eventsByDate.get(cell.date) ?? []) : [];
        const isToday = cell.inMonth && cell.date === todayStr;
        return (
          <MiniDay
            key={cell.date}
            date={cell.date}
            day={cell.day}
            inMonth={cell.inMonth}
            isToday={isToday}
            events={events}
            colorFor={colorFor}
          />
        );
      })}
    </>
  );
}

function MiniDay({
  date,
  day,
  inMonth,
  isToday,
  events,
  colorFor,
}: {
  date: string;
  day: number;
  inMonth: boolean;
  isToday: boolean;
  events: CalendarEvent[];
  colorFor: typeof SeriesColorFn;
}) {
  const base = "flex aspect-square flex-col items-center justify-start rounded p-0.5 text-[10px]";
  const tone = inMonth ? "text-gray-700" : "text-gray-300";
  const ring = isToday ? "ring-1 ring-blue-500" : "";

  // Adjacent-month padding cells never carry events — render a bare number.
  if (!inMonth || events.length === 0) {
    return (
      <div className={`${base} ${tone} ${ring}`}>
        <span>{day}</span>
      </div>
    );
  }

  const dots = events.slice(0, MAX_DOTS);
  const overflow = events.length - dots.length;

  return (
    <details
      className={`group ${base} ${tone} ${ring} cursor-pointer`}
      data-has-events="true"
      aria-label={`${date}, ${events.length} event${events.length === 1 ? "" : "s"}`}
    >
      <summary className="flex list-none flex-col items-center gap-0.5">
        <span>{day}</span>
        <span className="flex items-center gap-0.5">
          {dots.map((event) => (
            <span
              key={event.event_id}
              className={`inline-block h-1.5 w-1.5 rounded-full ${colorFor(event.series).dot}`}
              aria-hidden="true"
            />
          ))}
          {overflow > 0 ? <span className="text-[8px] text-gray-500">+{overflow}</span> : null}
        </span>
      </summary>
      <ul className="absolute z-10 mt-1 w-44 space-y-1 rounded-md border border-gray-200 bg-white p-2 text-left shadow-lg">
        {events.map((event) => {
          const color = colorFor(event.series);
          return (
            <li key={event.event_id} className="text-xs">
              <span
                className={`mr-1 inline-block h-2 w-2 rounded-full align-middle ${color.dot}`}
                aria-hidden="true"
              />
              <span className="font-medium">{event.title}</span>
              {event.series ? <span className="ml-1 text-gray-500">· {event.series}</span> : null}
            </li>
          );
        })}
      </ul>
    </details>
  );
}
