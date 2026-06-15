import { DRAG_MIME } from "@/lib/calendar-dnd";
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
  /** Open the quick-create popover for an in-month day (`YYYY-MM-DD`). */
  onCreate: (date: string) => void;
  /** Open the edit/delete popover for an event chip. */
  onEdit: (event: CalendarEvent) => void;
  /**
   * Drag-to-reschedule (Phase 42): an event chip dragged onto an in-month day
   * cell moves that event to the cell's `YYYY-MM-DD`. CalendarView applies the
   * move optimistically and PATCHes; a same-day drop is the owner's no-op.
   */
  onMove: (eventId: string, toDate: string) => void;
}

/**
 * The single-month view: a `grid-cols-7` of tall day cells. Each in-month day
 * shows up to {@link MAX_CHIPS} series-colored event CHIPS (title as a plain
 * text node — never `dangerouslySetInnerHTML`) and a "+N more" line when there
 * are more. Clicking a chip opens the edit/delete popover (Phase 40); clicking
 * an in-month day's empty space opens the quick-create popover for that day.
 * Leading/trailing adjacent-month days are dimmed and inert. Built from the pure
 * `monthGrid` so the alignment (Sunday start, leap February) is exactly what the
 * vitest suite pins.
 */
export function MonthView({
  year,
  month,
  eventsByDate,
  colorFor,
  todayStr,
  onCreate,
  onEdit,
  onMove,
}: MonthViewProps) {
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
                date={cell.date}
                day={cell.day}
                inMonth={cell.inMonth}
                isToday={isToday}
                events={events}
                colorFor={colorFor}
                onCreate={onCreate}
                onEdit={onEdit}
                onMove={onMove}
              />
            );
          }),
        )}
      </div>
    </div>
  );
}

function MonthDay({
  date,
  day,
  inMonth,
  isToday,
  events,
  colorFor,
  onCreate,
  onEdit,
  onMove,
}: {
  date: string;
  day: number;
  inMonth: boolean;
  isToday: boolean;
  events: CalendarEvent[];
  colorFor: typeof SeriesColorFn;
  onCreate: (date: string) => void;
  onEdit: (event: CalendarEvent) => void;
  onMove: (eventId: string, toDate: string) => void;
}) {
  const chips = events.slice(0, MAX_CHIPS);
  const overflow = events.length - chips.length;

  // Drop target: only IN-MONTH cells accept a drop (adjacent-month padding
  // cells are inert). preventDefault on dragover is required for the browser to
  // fire `drop`; preventDefault on drop stops the browser's default handling.
  const dropProps = inMonth
    ? {
        onDragOver: (e: React.DragEvent<HTMLDivElement>) => {
          e.preventDefault();
          e.dataTransfer.dropEffect = "move";
        },
        onDrop: (e: React.DragEvent<HTMLDivElement>) => {
          e.preventDefault();
          const eventId = e.dataTransfer.getData(DRAG_MIME);
          if (eventId) {
            onMove(eventId, date);
          }
        },
      }
    : {};

  return (
    <div
      data-has-events={inMonth && events.length > 0 ? "true" : undefined}
      data-drop-date={inMonth ? date : undefined}
      {...dropProps}
      className={`min-h-24 border-gray-100 border-r border-b p-1.5 ${
        inMonth ? "bg-white" : "bg-gray-50"
      }`}
    >
      {inMonth ? (
        <button
          type="button"
          onClick={() => onCreate(date)}
          aria-label={`Add an event on ${date}`}
          className={`mb-1 inline-flex h-5 w-5 items-center justify-center rounded-full text-xs hover:bg-gray-100 ${
            isToday ? "bg-blue-600 font-semibold text-white hover:bg-blue-700" : "text-gray-700"
          }`}
        >
          {day}
        </button>
      ) : (
        <div className="mb-1 inline-flex h-5 w-5 items-center justify-center rounded-full text-gray-300 text-xs">
          {day}
        </div>
      )}
      <ul className="space-y-0.5">
        {chips.map((event) => {
          const color = colorFor(event.series);
          return (
            <li key={event.event_id}>
              <button
                type="button"
                draggable
                onDragStart={(e) => {
                  e.dataTransfer.setData(DRAG_MIME, event.event_id);
                  e.dataTransfer.effectAllowed = "move";
                }}
                onClick={() => onEdit(event)}
                aria-label={`Edit ${event.title}`}
                title={event.series ? `${event.title} · ${event.series}` : event.title}
                data-event-chip
                data-event-id={event.event_id}
                className={`block w-full cursor-grab truncate rounded px-1 py-0.5 text-left text-[11px] leading-tight hover:opacity-80 ${color.bg} ${color.text}`}
              >
                {event.title}
              </button>
            </li>
          );
        })}
        {overflow > 0 ? <li className="px-1 text-[11px] text-gray-500">+{overflow} more</li> : null}
      </ul>
    </div>
  );
}
