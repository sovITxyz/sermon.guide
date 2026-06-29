"use client";

import { useId, useState } from "react";
import { DialogShell } from "./calendar/QuickCreatePopover";

interface ScheduleSermonPopoverProps {
  /**
   * The sermon's current title; prefills the event title. The event title is an
   * INDEPENDENT copy — editing it here never touches the sermon — so this works
   * even while the sermon title is mid-autosave.
   */
  defaultTitle: string;
  /** Default event date (`YYYY-MM-DD`), typically `today()` from lib/dates. */
  defaultDate: string;
  /** Close without scheduling. */
  onClose: () => void;
  /**
   * Persist via the POST proxy. Resolves to `null` on success (the caller shows
   * a confirmation and unmounts this popover) or a human-readable error string
   * to show inline. The caller owns the fetch so this stays presentation-only,
   * mirroring QuickCreatePopover's contract.
   */
  onSubmit: (input: {
    event_date: string;
    title: string;
    series: string | null;
  }) => Promise<string | null>;
}

/**
 * The "Schedule this sermon on the calendar" popover (Phase 47). The reverse
 * entry point to the calendar-first link flow: from an open sermon, pick a date
 * (defaults to today), optionally a series, and a title (prefilled from the
 * sermon), then create a calendar event already linked to this sermon in one
 * POST. The caller (SermonEditor) owns the fetch and passes the sermon's
 * `document_id`, so this component never sees it — it only collects the
 * date/title/series and resolves the caller's `onSubmit`.
 *
 * All entered text is held in React state and rendered through controlled
 * inputs / text nodes — never `dangerouslySetInnerHTML`.
 */
export function ScheduleSermonPopover({
  defaultTitle,
  defaultDate,
  onClose,
  onSubmit,
}: ScheduleSermonPopoverProps) {
  const dateId = useId();
  const titleId = useId();
  const seriesId = useId();

  const [date, setDate] = useState(defaultDate);
  const [title, setTitle] = useState(defaultTitle);
  const [series, setSeries] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (saving) {
      return;
    }
    setSaving(true);
    setError(null);
    const message = await onSubmit({
      event_date: date,
      title,
      series: series.trim() === "" ? null : series,
    });
    if (message === null) {
      // Success — the caller shows a confirmation and unmounts this popover.
      return;
    }
    setError(message);
    setSaving(false);
  }

  return (
    <DialogShell title="Schedule on calendar" onClose={onClose}>
      <form onSubmit={handleSubmit} className="space-y-3">
        <div>
          <label htmlFor={dateId} className="mb-1 block font-medium text-gray-700 text-sm">
            Date
          </label>
          <input
            id={dateId}
            type="date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
            required
            className="w-full rounded border border-gray-300 px-2 py-1 text-sm"
          />
        </div>

        <div>
          <label htmlFor={titleId} className="mb-1 block font-medium text-gray-700 text-sm">
            Event title
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
            Series <span className="font-normal text-gray-400">(optional)</span>
          </label>
          <input
            id={seriesId}
            type="text"
            value={series}
            onChange={(e) => setSeries(e.target.value)}
            className="w-full rounded border border-gray-300 px-2 py-1 text-sm"
          />
        </div>

        {error ? (
          <p role="alert" className="text-red-600 text-sm">
            {error}
          </p>
        ) : null}

        <div className="flex flex-wrap items-center justify-end gap-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            className="mr-auto rounded border border-gray-300 px-3 py-1 text-sm hover:bg-gray-50"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={saving}
            className="rounded bg-black px-3 py-1 text-sm text-white hover:bg-gray-800 disabled:opacity-50"
          >
            {saving ? "Scheduling…" : "Schedule"}
          </button>
        </div>
      </form>
    </DialogShell>
  );
}
