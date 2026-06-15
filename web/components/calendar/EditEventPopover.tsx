"use client";

import { DialogShell } from "@/components/calendar/QuickCreatePopover";
import type { CalendarEvent } from "@/lib/types";
import { useId, useState } from "react";

interface EditEventPopoverProps {
  /** The event being edited. */
  event: CalendarEvent;
  /** Close without saving. */
  onClose: () => void;
  /**
   * Persist the edit via the PATCH proxy. Forwards `event_date`, `title`, and
   * `series` (null clears the series). Resolves `null` on success (caller
   * refetches + closes) or an error string to show inline.
   */
  onSave: (input: {
    event_date: string;
    title: string;
    series: string | null;
  }) => Promise<string | null>;
  /**
   * Delete via the DELETE proxy. Resolves `null` on success (caller refetches +
   * closes) or an error string to show inline.
   */
  onDelete: () => Promise<string | null>;
}

/**
 * The edit/delete popover (Phase 40), opened from a chip/card. Prefilled from
 * the event; Save PATCHes title/series/event_date, Delete hard-deletes. The
 * API owns the at-least-one-of / length 422s — this layer only sends the
 * structural body. Text renders through controlled inputs / text nodes, never
 * `dangerouslySetInnerHTML`.
 */
export function EditEventPopover({ event, onClose, onSave, onDelete }: EditEventPopoverProps) {
  const titleId = useId();
  const seriesId = useId();
  const dateId = useId();

  const [title, setTitle] = useState(event.title);
  const [series, setSeries] = useState(event.series ?? "");
  const [eventDate, setEventDate] = useState(event.event_date);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    if (busy) {
      return;
    }
    setBusy(true);
    setError(null);
    const message = await onSave({
      event_date: eventDate,
      title,
      series: series.trim() === "" ? null : series,
    });
    if (message === null) {
      return;
    }
    setError(message);
    setBusy(false);
  }

  async function handleDelete() {
    if (busy) {
      return;
    }
    setBusy(true);
    setError(null);
    const message = await onDelete();
    if (message === null) {
      return;
    }
    setError(message);
    setBusy(false);
  }

  return (
    <DialogShell title="Edit event" onClose={onClose}>
      <form onSubmit={handleSave} className="space-y-3">
        <div>
          <label htmlFor={titleId} className="mb-1 block font-medium text-gray-700 text-sm">
            Title
          </label>
          <input
            id={titleId}
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            required
            className="w-full rounded border border-gray-300 px-2 py-1 text-sm"
          />
        </div>

        <div>
          <label htmlFor={seriesId} className="mb-1 block font-medium text-gray-700 text-sm">
            Series <span className="font-normal text-gray-400">(blank to clear)</span>
          </label>
          <input
            id={seriesId}
            type="text"
            value={series}
            onChange={(e) => setSeries(e.target.value)}
            className="w-full rounded border border-gray-300 px-2 py-1 text-sm"
          />
        </div>

        <div>
          <label htmlFor={dateId} className="mb-1 block font-medium text-gray-700 text-sm">
            Date
          </label>
          <input
            id={dateId}
            type="date"
            value={eventDate}
            onChange={(e) => setEventDate(e.target.value)}
            className="w-full rounded border border-gray-300 px-2 py-1 text-sm"
          />
        </div>

        {error ? (
          <p role="alert" className="text-red-600 text-sm">
            {error}
          </p>
        ) : null}

        <div className="flex items-center justify-between pt-1">
          <button
            type="button"
            onClick={handleDelete}
            disabled={busy}
            className="rounded border border-red-300 px-3 py-1 text-red-600 text-sm hover:bg-red-50 disabled:opacity-50"
          >
            Delete
          </button>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded border border-gray-300 px-3 py-1 text-sm hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={busy}
              className="rounded bg-black px-3 py-1 text-sm text-white hover:bg-gray-800 disabled:opacity-50"
            >
              {busy ? "Saving…" : "Save"}
            </button>
          </div>
        </div>
      </form>
    </DialogShell>
  );
}
