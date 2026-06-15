import type { seriesColor as SeriesColorFn } from "@/lib/calendar-view";
import { monthGrid } from "@/lib/dates";
import type { CalendarEvent } from "@/lib/types";

/** Sunday-first weekday headers (week starts Sunday — settled). */
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"] as const;

/** Up to this many event chips render per day before collapsing to "+N more". */
const MAX_CHIPS = 3;

interface MonthViewProps {
  year: number;
  /** 1-based month (1 = January). */
  month: number;
  /** Events grouped by `event_date` (`YYYY-MM-DD`) for the month. */
  eventsByDate: Map<string, CalendarEvent[]>;
  colorFor: typeof SeriesColorFn;
  /** Today's `YYYY-MM-DD`, to ring the current day. */
  todayStr: string;
}

/**
 * The single-month view: a `grid-cols-7` of tall day cells. Each in-month day
 * shows up to {@link MAX_CHIPS} series-colored event CHIPS (title as a plain
 * text node — never `dangerouslySetInnerHTML`) and a "+N more" line when there
 * are more. Leading/trailing adjacent-month days are dimmed. Built from the
 * pure `monthGrid` so the alignment (Sunday start, leap February) is exactly
 * what the vitest suite pins.
 */
export function MonthView({ year, month, eventsByDate, colorFor, todayStr }: MonthViewProps) {
  const weeks = monthGrid(year, month);

  return (
    <div className="overflow-hidden rounded-lg border border-gray-200">
      <div className="grid grid-cols-7 border-gray-200 border-b bg-gray-50 text-center font-medium text-gray-500 text-xs">
        {WEEKDAYS.map((label) => (
          <div key={label} className="py-2">
            {label}
          </div>
        ))}
      </div>
      <div className="grid grid-cols-7">
        {weeks.map((week) =>
          week.map((cell) => {
            const events = cell.inMonth ? (eventsByDate.get(cell.date) ?? []) : [];
            const isToday = cell.inMonth && cell.date === todayStr;
            return (
              <MonthDay
                key={cell.date}
                day={cell.day}
                inMonth={cell.inMonth}
                isToday={isToday}
                events={events}
                colorFor={colorFor}
              />
            );
          }),
        )}
      </div>
    </div>
  );
}

function MonthDay({
  day,
  inMonth,
  isToday,
  events,
  colorFor,
}: {
  day: number;
  inMonth: boolean;
  isToday: boolean;
  events: CalendarEvent[];
  colorFor: typeof SeriesColorFn;
}) {
  const chips = events.slice(0, MAX_CHIPS);
  const overflow = events.length - chips.length;

  return (
    <div
      data-has-events={inMonth && events.length > 0 ? "true" : undefined}
      className={`min-h-24 border-gray-100 border-r border-b p-1.5 ${
        inMonth ? "bg-white" : "bg-gray-50"
      }`}
    >
      <div
        className={`mb-1 inline-flex h-5 w-5 items-center justify-center rounded-full text-xs ${
          inMonth ? "text-gray-700" : "text-gray-300"
        } ${isToday ? "bg-blue-600 font-semibold text-white" : ""}`}
      >
        {day}
      </div>
      <ul className="space-y-0.5">
        {chips.map((event) => {
          const color = colorFor(event.series);
          return (
            <li
              key={event.event_id}
              title={event.series ? `${event.title} · ${event.series}` : event.title}
              className={`truncate rounded px-1 py-0.5 text-[11px] leading-tight ${color.bg} ${color.text}`}
            >
              {event.title}
            </li>
          );
        })}
        {overflow > 0 ? <li className="px-1 text-[11px] text-gray-500">+{overflow} more</li> : null}
      </ul>
    </div>
  );
}
