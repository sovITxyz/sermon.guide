"use client";

import { DialogShell } from "@/components/calendar/QuickCreatePopover";
import type { CalendarEvent, DocumentListItem, DocumentListResponse } from "@/lib/types";
import { useEffect, useId, useState } from "react";

interface EditEventPopoverProps {
  /** The event being edited. */
  event: CalendarEvent;
  /** Close without saving. */
  onClose: () => void;
  /**
   * Persist the edit via the PATCH proxy. Forwards `event_date`, `title`, and
   * `series` (null clears the series), plus the three-state `document_id` when
   * the link changed: a string RE-LINKS, an explicit `null` UNLINKS, and an
   * ABSENT key leaves the link alone. Resolves `null` on success (caller
   * refetches + closes) or an error string to show inline — including the Phase
   * 38 ownership 404 a cross-tenant/nonexistent `document_id` returns.
   */
  onSave: (input: {
    event_date: string;
    title: string;
    series: string | null;
    document_id?: string | null;
  }) => Promise<string | null>;
  /**
   * Delete via the DELETE proxy. Resolves `null` on success (caller refetches +
   * closes) or an error string to show inline.
   */
  onDelete: () => Promise<string | null>;
}

/** Picker state: not yet loaded, in flight, ready with the user's docs, or failed. */
type PickerState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; documents: DocumentListItem[] }
  | { kind: "error"; message: string };

/**
 * The edit/delete popover (Phase 40 + Phase 41 sermon-linking), opened from an
 * UNLINKED chip/card (a linked one navigates straight to its manuscript).
 * Prefilled from the event; Save PATCHes title/series/event_date and — when the
 * link changed — the three-state `document_id`. The picker is fed by the GET
 * /api/documents proxy (the user's own docs by construction, server-side
 * bearer-scoped). Linking sends `document_id: <id>`; unlinking sends
 * `document_id: null`. The API owns the at-least-one-of / length / OWNERSHIP
 * 422-404s — this layer only sends the structural body and surfaces whatever
 * error comes back. All entered text and every doc title render through
 * controlled inputs / text nodes, never `dangerouslySetInnerHTML`.
 */
export function EditEventPopover({ event, onClose, onSave, onDelete }: EditEventPopoverProps) {
  const titleId = useId();
  const seriesId = useId();
  const dateId = useId();
  const linkId = useId();

  const [title, setTitle] = useState(event.title);
  const [series, setSeries] = useState(event.series ?? "");
  const [eventDate, setEventDate] = useState(event.event_date);
  // The selected manuscript link: "" means UNLINKED, a non-empty value is the
  // chosen document_id. Initialized from the event (always null here, since a
  // linked chip navigates instead of opening this popover, but kept honest).
  const [documentId, setDocumentId] = useState<string>(event.document_id ?? "");
  const [picker, setPicker] = useState<PickerState>({ kind: "idle" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Fetch the user's own sermons for the picker on mount. The route handler
  // reads the HttpOnly cookie server-side and returns preview-only summaries
  // scoped to the bearer's user — there is no cross-tenant leak to guard here.
  useEffect(() => {
    let alive = true;
    setPicker({ kind: "loading" });
    fetch("/api/documents", { cache: "no-store" })
      .then(async (res) => {
        if (!alive) {
          return;
        }
        if (!res.ok) {
          setPicker({ kind: "error", message: "Could not load your sermons to link." });
          return;
        }
        const data = (await res.json()) as DocumentListResponse;
        if (alive) {
          setPicker({ kind: "ready", documents: data.documents });
        }
      })
      .catch(() => {
        if (alive) {
          setPicker({ kind: "error", message: "Could not load your sermons to link." });
        }
      });
    return () => {
      alive = false;
    };
  }, []);

  /** True when the link selection differs from the event's stored link. */
  const linkChanged = (documentId === "" ? null : documentId) !== event.document_id;

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    if (busy) {
      return;
    }
    setBusy(true);
    setError(null);
    // Only send document_id when it actually changed: an unchanged link stays
    // an ABSENT key so the PATCH never re-asserts (or re-ownership-checks) the
    // existing link. A changed-to-"" link sends an explicit null to UNLINK.
    const input: {
      event_date: string;
      title: string;
      series: string | null;
      document_id?: string | null;
    } = {
      event_date: eventDate,
      title,
      series: series.trim() === "" ? null : series,
    };
    if (linkChanged) {
      input.document_id = documentId === "" ? null : documentId;
    }
    const message = await onSave(input);
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
            Date <span className="font-normal text-gray-400">(reschedule)</span>
          </label>
          {/*
            Keyboard-accessible reschedule (Phase 42). HTML5 drag-and-drop is
            mouse-only, so this date control is the REQUIRED accessible path to
            move an event to another day: pick/type a new date and either Save
            (saves the whole form) or the dedicated "Move to date" button (which
            submits the same form → PATCH event_date → refetch + close). On
            failure the shared inline error below shows and the popover stays
            open.
          */}
          <div className="flex gap-2">
            <input
              id={dateId}
              type="date"
              value={eventDate}
              onChange={(e) => setEventDate(e.target.value)}
              className="w-full rounded border border-gray-300 px-2 py-1 text-sm"
            />
            <button
              type="submit"
              disabled={busy || eventDate === event.event_date}
              aria-label="Move to date"
              className="shrink-0 rounded border border-gray-300 px-3 py-1 text-sm hover:bg-gray-50 disabled:opacity-50"
            >
              Move
            </button>
          </div>
        </div>

        <div>
          <label htmlFor={linkId} className="mb-1 block font-medium text-gray-700 text-sm">
            Linked sermon <span className="font-normal text-gray-400">(none to unlink)</span>
          </label>
          {picker.kind === "loading" || picker.kind === "idle" ? (
            <p className="text-gray-500 text-sm">Loading your sermons…</p>
          ) : picker.kind === "error" ? (
            <p className="text-gray-500 text-sm">{picker.message}</p>
          ) : (
            // A native <select>: the option labels render as text nodes (never
            // inner HTML), and an empty value is the UNLINK choice.
            <select
              id={linkId}
              value={documentId}
              onChange={(e) => setDocumentId(e.target.value)}
              className="w-full rounded border border-gray-300 px-2 py-1 text-sm"
            >
              <option value="">No linked sermon</option>
              {picker.documents.map((doc) => (
                <option key={doc.document_id} value={doc.document_id}>
                  {doc.title}
                </option>
              ))}
            </select>
          )}
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
