"use client";

import { DialogShell } from "@/components/calendar/QuickCreatePopover";
import type { Collection, LibraryBook } from "@/lib/types";
import { useRouter } from "next/navigation";
import { useCallback, useId, useMemo, useRef, useState } from "react";

/**
 * The /library collections panel (Phase 48). A CLIENT island beside the
 * presentational LibraryTable: it lists the user's collections, creates /
 * renames / deletes them, and assigns library books into them. A collection is
 * a user-owned folder grouping books; membership is clamped to the owner's
 * library server-side (api/collections_routes.py), so this layer never
 * pre-validates ownership.
 *
 * Mutations route through the same-origin proxies (POST/PATCH/DELETE
 * /api/collections[/…]), then `router.refresh()` re-runs the /library server
 * component so the list reflects the new state (a server component cannot mutate
 * — the island owns the action + refresh). All names render as PLAIN TEXT
 * (controlled inputs / text nodes) — never dangerouslySetInnerHTML.
 *
 * Book selection lives LOCALLY in this panel (Phase 48): a checkbox per library
 * book plus a per-collection "whole collection" checkbox that ticks every book
 * already inside it. The selected set is then assigned to a chosen collection.
 * (Phase 49 lifts this into a shared SelectionProvider spanning /library +
 * /search; this panel is the standalone precursor.)
 */
export function CollectionsPanel({
  collections,
  books,
}: {
  collections: Collection[];
  books: LibraryBook[];
}) {
  const router = useRouter();
  // Books ticked for assignment (local selection — Phase 48 precursor).
  const [selected, setSelected] = useState<ReadonlySet<string>>(() => new Set());
  // The create/edit modal — null when closed.
  const [dialog, setDialog] = useState<
    { mode: "create" } | { mode: "edit"; target: Collection } | null
  >(null);
  // The collection chosen as the assignment target (the <select> value).
  const [targetId, setTargetId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // A ref mirror of `busy` so async handlers gate on the freshest value without
  // re-creating callbacks on every transition (the SermonList busyRef pattern).
  const busyRef = useRef(false);

  const selectId = useId();

  // Only library books can be assigned, so map by id once for the whole-collection
  // toggle (a collection may name a book that has since left the library).
  const libraryIds = useMemo(() => new Set(books.map((b) => b.book_id)), [books]);

  const toggleBook = useCallback((bookId: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(bookId)) {
        next.delete(bookId);
      } else {
        next.add(bookId);
      }
      return next;
    });
  }, []);

  // Tick / untick every (still-in-library) book of one collection at once.
  const toggleWholeCollection = useCallback(
    (collection: Collection) => {
      const memberIds = collection.book_ids.filter((id) => libraryIds.has(id));
      const allSelected = memberIds.length > 0 && memberIds.every((id) => selected.has(id));
      setSelected((prev) => {
        const next = new Set(prev);
        for (const id of memberIds) {
          if (allSelected) {
            next.delete(id);
          } else {
            next.add(id);
          }
        }
        return next;
      });
    },
    [libraryIds, selected],
  );

  const onDelete = useCallback(
    async (collection: Collection): Promise<void> => {
      if (busyRef.current) {
        return;
      }
      const confirmed = window.confirm(
        `Delete the collection “${collection.name}”? The books stay in your library.`,
      );
      if (!confirmed) {
        return;
      }
      setError(null);
      busyRef.current = true;
      setBusy(true);
      try {
        const res = await fetch(
          `/api/collections/${encodeURIComponent(collection.collection_id)}`,
          {
            method: "DELETE",
          },
        );
        // 204 on success; the uniform 404 means it is already gone — treat both
        // as "no longer listed" and refresh. Anything else is a real error.
        if (res.status === 204 || res.status === 404) {
          router.refresh();
        } else {
          setError("Could not delete the collection. Please try again.");
        }
      } catch {
        setError("Network error. Please try again.");
      } finally {
        busyRef.current = false;
        setBusy(false);
      }
    },
    [router],
  );

  // Persist a create or rename. Returns null on success (the dialog unmounts) or
  // an error string to show inline — mirrors QuickCreatePopover's contract.
  const onSaveDialog = useCallback(
    async (input: { name: string; description: string | null }): Promise<string | null> => {
      if (busyRef.current || dialog === null) {
        return "Please wait for the current action to finish.";
      }
      busyRef.current = true;
      setBusy(true);
      try {
        const url =
          dialog.mode === "create"
            ? "/api/collections"
            : `/api/collections/${encodeURIComponent(dialog.target.collection_id)}`;
        const res = await fetch(url, {
          method: dialog.mode === "create" ? "POST" : "PATCH",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(input),
        });
        if (res.ok) {
          setDialog(null);
          router.refresh();
          return null;
        }
        if (res.status === 404) {
          return "That collection no longer exists.";
        }
        return "Could not save the collection. Please try again.";
      } catch {
        return "Network error. Please try again.";
      } finally {
        busyRef.current = false;
        setBusy(false);
      }
    },
    [router, dialog],
  );

  const onAssign = useCallback(async (): Promise<void> => {
    if (busyRef.current || targetId === "" || selected.size === 0) {
      return;
    }
    setError(null);
    busyRef.current = true;
    setBusy(true);
    try {
      const res = await fetch(`/api/collections/${encodeURIComponent(targetId)}/books`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ book_ids: [...selected] }),
      });
      if (res.ok) {
        setSelected(new Set());
        router.refresh();
      } else if (res.status === 404) {
        setError("That collection no longer exists.");
      } else {
        setError("Could not add the books. Please try again.");
      }
    } catch {
      setError("Network error. Please try again.");
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }, [router, selected, targetId]);

  return (
    <section className="rounded-lg border border-gray-200 p-4" aria-label="Collections">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="font-semibold text-base">Collections</h2>
        <button
          type="button"
          onClick={() => {
            setError(null);
            setDialog({ mode: "create" });
          }}
          disabled={busy}
          className="rounded bg-black px-3 py-1.5 font-medium text-sm text-white disabled:opacity-50"
        >
          New collection
        </button>
      </div>

      {error ? (
        <p role="alert" className="mb-3 text-red-600 text-sm">
          {error}
        </p>
      ) : null}

      {collections.length === 0 ? (
        <p className="rounded border border-gray-300 border-dashed p-4 text-center text-gray-600 text-sm">
          No collections yet. Create one to group books in your library.
        </p>
      ) : (
        <ul className="divide-y divide-gray-100">
          {collections.map((collection) => {
            const memberIds = collection.book_ids.filter((id) => libraryIds.has(id));
            const allSelected = memberIds.length > 0 && memberIds.every((id) => selected.has(id));
            const someSelected = memberIds.some((id) => selected.has(id));
            return (
              <li
                key={collection.collection_id}
                className="flex items-start justify-between gap-3 py-2"
              >
                <label className="flex min-w-0 flex-1 items-start gap-2 text-sm">
                  <input
                    type="checkbox"
                    className="mt-1"
                    checked={allSelected}
                    aria-label={`Select all books in ${collection.name}`}
                    ref={(el) => {
                      if (el) {
                        el.indeterminate = !allSelected && someSelected;
                      }
                    }}
                    onChange={() => toggleWholeCollection(collection)}
                  />
                  <span className="min-w-0">
                    <span className="font-medium">{collection.name}</span>
                    <span className="ml-2 text-gray-500 text-xs">
                      {`${collection.book_ids.length} ${
                        collection.book_ids.length === 1 ? "book" : "books"
                      }`}
                    </span>
                    {collection.description ? (
                      <span className="mt-0.5 block text-gray-600 text-xs">
                        {collection.description}
                      </span>
                    ) : null}
                  </span>
                </label>
                <div className="flex shrink-0 items-center gap-2">
                  <button
                    type="button"
                    onClick={() => {
                      setError(null);
                      setDialog({ mode: "edit", target: collection });
                    }}
                    disabled={busy}
                    aria-label={`Rename ${collection.name}`}
                    className="rounded border border-gray-300 px-2 py-1 text-gray-600 text-xs hover:bg-gray-50 disabled:opacity-50"
                  >
                    Rename
                  </button>
                  <button
                    type="button"
                    onClick={() => void onDelete(collection)}
                    disabled={busy}
                    aria-label={`Delete ${collection.name}`}
                    className="rounded border border-gray-300 px-2 py-1 text-gray-600 text-xs hover:border-red-300 hover:text-red-600 disabled:opacity-50"
                  >
                    Delete
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {collections.length > 0 && books.length > 0 ? (
        <div className="mt-4 border-gray-100 border-t pt-3">
          <h3 className="mb-2 font-medium text-gray-700 text-sm">Add books to a collection</h3>
          <ul className="mb-3 max-h-56 space-y-1 overflow-y-auto">
            {books.map((book) => (
              <li key={book.book_id}>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={selected.has(book.book_id)}
                    onChange={() => toggleBook(book.book_id)}
                  />
                  <span className="min-w-0 truncate">{book.title}</span>
                </label>
              </li>
            ))}
          </ul>
          <div className="flex flex-wrap items-center gap-2">
            <label htmlFor={selectId} className="sr-only">
              Add selected books to collection
            </label>
            <select
              id={selectId}
              value={targetId}
              onChange={(e) => setTargetId(e.target.value)}
              className="rounded border border-gray-300 px-2 py-1 text-sm"
            >
              <option value="">Choose a collection…</option>
              {collections.map((collection) => (
                <option key={collection.collection_id} value={collection.collection_id}>
                  {collection.name}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={() => void onAssign()}
              disabled={busy || targetId === "" || selected.size === 0}
              className="rounded bg-black px-3 py-1.5 font-medium text-sm text-white disabled:opacity-50"
            >
              {selected.size > 0 ? `Add ${selected.size} to collection` : "Add to collection"}
            </button>
          </div>
        </div>
      ) : null}

      {dialog !== null ? (
        <CollectionDialog
          mode={dialog.mode}
          initialName={dialog.mode === "edit" ? dialog.target.name : ""}
          initialDescription={dialog.mode === "edit" ? (dialog.target.description ?? "") : ""}
          onSubmit={onSaveDialog}
          onClose={() => setDialog(null)}
        />
      ) : null}
    </section>
  );
}

/**
 * The create / rename modal, reusing the shared DialogShell. Holds its own
 * input state and resolves `onSubmit` (the panel owns the fetch) — mirroring
 * QuickCreatePopover: resolve `null` on success (the panel unmounts this) or an
 * error string to show inline. The empty-name guard is local (the API would 422
 * anyway — the single length owner stays the API).
 */
function CollectionDialog({
  mode,
  initialName,
  initialDescription,
  onSubmit,
  onClose,
}: {
  mode: "create" | "edit";
  initialName: string;
  initialDescription: string;
  onSubmit: (input: { name: string; description: string | null }) => Promise<string | null>;
  onClose: () => void;
}) {
  const nameId = useId();
  const descId = useId();
  const [name, setName] = useState(initialName);
  const [description, setDescription] = useState(initialDescription);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (saving) {
      return;
    }
    if (name.trim() === "") {
      setError("Add a name for the collection.");
      return;
    }
    setSaving(true);
    setError(null);
    const message = await onSubmit({
      name,
      description: description.trim() === "" ? null : description,
    });
    if (message === null) {
      // Success — the panel refreshes and unmounts this dialog.
      return;
    }
    setError(message);
    setSaving(false);
  }

  return (
    <DialogShell title={mode === "create" ? "New collection" : "Edit collection"} onClose={onClose}>
      <form onSubmit={handleSubmit} className="space-y-3">
        <div>
          <label htmlFor={nameId} className="mb-1 block font-medium text-gray-700 text-sm">
            Name
          </label>
          <input
            id={nameId}
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            className="w-full rounded border border-gray-300 px-2 py-1 text-sm"
          />
        </div>

        <div>
          <label htmlFor={descId} className="mb-1 block font-medium text-gray-700 text-sm">
            Description <span className="font-normal text-gray-400">(optional)</span>
          </label>
          <textarea
            id={descId}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={2}
            className="w-full rounded border border-gray-300 px-2 py-1 text-sm"
          />
        </div>

        {error ? (
          <p role="alert" className="text-red-600 text-sm">
            {error}
          </p>
        ) : null}

        <div className="flex items-center justify-end gap-2 pt-1">
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
            {saving ? "Saving…" : mode === "create" ? "Create" : "Save"}
          </button>
        </div>
      </form>
    </DialogShell>
  );
}
