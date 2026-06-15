"use client";

import { useId, useState } from "react";

interface QuickCreatePopoverProps {
  /** The day the create was initiated on (`YYYY-MM-DD`); prefills event_date. */
  date: string;
  /** Close without saving. */
  onClose: () => void;
  /**
   * Persist via the POST proxy. Resolves to `null` on success (the caller
   * refetches + closes) or a human-readable error string to show inline. The
   * caller owns the fetch so this stays presentation-only.
   */
  onSubmit: (input: {
    event_date: string;
    title: string;
    series: string | null;
    repeat_weekly_until: string | null;
  }) => Promise<string | null>;
}

/**
 * The "create on an empty day" popover (Phase 40). Fields: title (required),
 * series (optional), and an optional weekly-repeat-until date. On submit it
 * calls the POST proxy through `onSubmit`; the API's materializer caps the
 * weekly run server-side and the cap/range 422 surfaces here as inline text
 * (this layer does NOT pre-validate length/range — single owner is the API).
 *
 * All entered text is held in React state and rendered through controlled
 * inputs / text nodes — never `dangerouslySetInnerHTML`.
 */
export function QuickCreatePopover({ date, onClose, onSubmit }: QuickCreatePopoverProps) {
  const titleId = useId();
  const seriesId = useId();
  const repeatId = useId();

  const [title, setTitle] = useState("");
  const [series, setSeries] = useState("");
  const [repeat, setRepeat] = useState(false);
  const [repeatUntil, setRepeatUntil] = useState("");
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
      repeat_weekly_until: repeat && repeatUntil !== "" ? repeatUntil : null,
    });
    if (message === null) {
      // Success — the caller refetches and unmounts this popover.
      return;
    }
    setError(message);
    setSaving(false);
  }

  return (
    <DialogShell title={`New event · ${date}`} onClose={onClose}>
      <form onSubmit={handleSubmit} className="space-y-3">
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

        <div className="space-y-1">
          <label className="flex items-center gap-2 text-gray-700 text-sm">
            <input type="checkbox" checked={repeat} onChange={(e) => setRepeat(e.target.checked)} />
            Repeat weekly
          </label>
          {repeat ? (
            <input
              id={repeatId}
              type="date"
              aria-label="Repeat weekly until"
              value={repeatUntil}
              onChange={(e) => setRepeatUntil(e.target.value)}
              className="w-full rounded border border-gray-300 px-2 py-1 text-sm"
            />
          ) : null}
        </div>

        {error ? (
          <p role="alert" className="text-red-600 text-sm">
            {error}
          </p>
        ) : null}

        <div className="flex justify-end gap-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            className="rounded border border-gray-300 px-3 py-1 text-sm hover:bg-gray-50"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={saving}
            className="rounded bg-black px-3 py-1 text-sm text-white hover:bg-gray-800 disabled:opacity-50"
          >
            {saving ? "Saving…" : "Create"}
          </button>
        </div>
      </form>
    </DialogShell>
  );
}

/**
 * A minimal centered modal shell shared by the create + edit popovers. The
 * backdrop click and the Cancel button both close; the title renders as a text
 * node. Kept here (not a separate file) since only the two calendar popovers
 * use it.
 */
export function DialogShell({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  return (
    // A backdrop div is the modal container, not a native <dialog>: we render it
    // conditionally (no imperative showModal()) and close on backdrop/Cancel,
    // which the controlled-popover pattern here needs. role/aria-modal/aria-label
    // give it the right semantics without the native element.
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4"
      // biome-ignore lint/a11y/useSemanticElements: controlled modal overlay, not a native <dialog>
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      {/* Backdrop: a click outside the panel closes. */}
      <button
        type="button"
        aria-label="Close"
        tabIndex={-1}
        onClick={onClose}
        className="absolute inset-0 h-full w-full cursor-default"
      />
      <div className="relative z-10 w-full max-w-sm rounded-lg border border-gray-200 bg-white p-4 shadow-xl">
        <h2 className="mb-3 font-semibold text-base">{title}</h2>
        {children}
      </div>
    </div>
  );
}
