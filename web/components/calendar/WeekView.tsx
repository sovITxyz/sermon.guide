import type { seriesColor as SeriesColorFn } from "@/lib/calendar-view";
import { type DayCell, parseDate, weekGrid, weekdayOf } from "@/lib/dates";
import type { CalendarEvent } from "@/lib/types";

/** Sunday-first weekday headers (week starts Sunday — settled, B3). */
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"] as const;

/**
 * Up to this many full event CARDS render per day column before collapsing to
 * a "+N more" line. The week view has the most room of the three views (a tall
 * column per day), so it shows more than the month view's chip cap.
 */
const MAX_CARDS = 5;

interface WeekViewProps {
  /** The anchor `YYYY-MM-DD`; the week CONTAINING it (Sunday-aligned) renders. */
  anchor: string;
  /** Events grouped by `event_date` (`YYYY-MM-DD`) for the week. */
  eventsByDate: Map<string, CalendarEvent[]>;
  colorFor: typeof SeriesColorFn;
  /** Today's `YYYY-MM-DD`, to ring the current day. */
  todayStr: string;
  /**
   * Called when an empty area of a day column is clicked — opens the quick-
   * create popover anchored on that day's `YYYY-MM-DD`.
   */
  onCreate: (date: string) => void;
  /** Called when an event card is clicked — opens the edit/delete popover. */
  onEdit: (event: CalendarEvent) => void;
}

/**
 * The week view (Phase 40): seven day COLUMNS of full event cards, Sunday-first
 * via the pure {@link weekGrid} (so the alignment is exactly what the vitest
 * suite pins and is timezone-immune). Each column's empty space is a
 * "create on this day" button; each event card opens the edit/delete popover.
 * Event titles and series render as plain TEXT NODES only — never
 * `dangerouslySetInnerHTML`. Colors come from the shared `colorFor` so a series
 * is the same color here as in the year and month views.
 */
export function WeekView({
  anchor,
  eventsByDate,
  colorFor,
  todayStr,
  onCreate,
  onEdit,
}: WeekViewProps) {
  const cells = weekGrid(anchor);

  return (
    <div
      data-testid="calendar-week"
      className="grid grid-cols-1 gap-2 sm:grid-cols-7 sm:gap-0 sm:overflow-hidden sm:rounded-lg sm:border sm:border-gray-200"
    >
      {cells.map((cell) => (
        <WeekDay
          key={cell.date}
          cell={cell}
          events={eventsByDate.get(cell.date) ?? []}
          isToday={cell.date === todayStr}
          colorFor={colorFor}
          onCreate={onCreate}
          onEdit={onEdit}
        />
      ))}
    </div>
  );
}

function WeekDay({
  cell,
  events,
  isToday,
  colorFor,
  onCreate,
  onEdit,
}: {
  cell: DayCell;
  events: CalendarEvent[];
  isToday: boolean;
  colorFor: typeof SeriesColorFn;
  onCreate: (date: string) => void;
  onEdit: (event: CalendarEvent) => void;
}) {
  const { year, month, day } = parseDate(cell.date);
  const weekdayLabel = WEEKDAYS[weekdayOf(year, month, day)] ?? "";
  const cards = events.slice(0, MAX_CARDS);
  const overflow = events.length - cards.length;

  return (
    <div
      data-has-events={events.length > 0 ? "true" : undefined}
      className="flex min-h-40 flex-col border-gray-200 border-b bg-white sm:border-r sm:border-b-0 sm:last:border-r-0"
    >
      <div className="flex items-baseline justify-between border-gray-100 border-b bg-gray-50 px-2 py-1">
        <span className="font-medium text-gray-500 text-xs">{weekdayLabel}</span>
        <span
          className={`inline-flex h-5 w-5 items-center justify-center rounded-full text-xs ${
            isToday ? "bg-blue-600 font-semibold text-white" : "text-gray-700"
          }`}
        >
          {cell.day}
        </span>
      </div>

      <ul className="flex-1 space-y-1 p-1.5">
        {cards.map((event) => {
          const color = colorFor(event.series);
          return (
            <li key={event.event_id}>
              <button
                type="button"
                onClick={() => onEdit(event)}
                aria-label={`Edit ${event.title}`}
                className={`block w-full rounded px-1.5 py-1 text-left text-[11px] leading-tight hover:opacity-80 ${color.bg} ${color.text}`}
              >
                <span className="block truncate font-medium">{event.title}</span>
                {event.series ? (
                  <span className="block truncate opacity-75">{event.series}</span>
                ) : null}
              </button>
            </li>
          );
        })}
        {overflow > 0 ? (
          <li className="px-1.5 text-[11px] text-gray-500">+{overflow} more</li>
        ) : null}
      </ul>

      <button
        type="button"
        onClick={() => onCreate(cell.date)}
        aria-label={`Add an event on ${cell.date}`}
        className="m-1 rounded border border-gray-200 border-dashed py-1 text-gray-400 text-xs hover:border-gray-300 hover:text-gray-600"
      >
        + Add
      </button>
    </div>
  );
}
