import type { DocumentPatch, ProseMirrorDoc } from "./types";

/**
 * Pure DECISIONS for the editor's autosave (Phase 36, B2 slice C). No DOM, no
 * timers, no fetch — just the timing constants, the dirty check, the PATCH-body
 * serialization, the ~64 KB keepalive size guard, the base_updated_at adoption,
 * and the single-flight coalescing state machine. Mirrors the reader's
 * lib/reader-view.ts split: the imperative loop (timers + fetch) stays in
 * components/SermonEditor.tsx, the rules that are easy to get wrong live here
 * and are unit-tested in test/sermon-autosave.test.ts (fake timers exercise the
 * component loop in test/components/SermonEditor.test.tsx).
 *
 * The autosave contract this encodes:
 *  - ~2 s debounce after the last edit; a 15 s max-interval ceiling so a user
 *    typing without pause still gets a save (DEBOUNCE_MS / MAX_INTERVAL_MS).
 *  - ONE in-flight PATCH at a time. Edits during a flight are coalesced into a
 *    single trailing save fired after it resolves — never parallel PATCHes
 *    (parallel writes race base_updated_at and manufacture 409s).
 *  - After every 200, adopt the response `updated_at` as the next
 *    base_updated_at (reusing the stale load value manufactures 409s too).
 *  - The pagehide keepalive flush only fires when dirty AND the serialized body
 *    is within the ~64 KB keepalive ceiling; an oversize doc SKIPS the flush
 *    (it saves on next open) rather than throwing.
 */

/** Idle time after the last edit before an autosave PATCH fires (ms). */
export const AUTOSAVE_DEBOUNCE_MS = 2000;

/**
 * Hard ceiling between autosaves while editing: continuous typing resets the
 * debounce forever, so without this a never-pausing writer would never save.
 * The first edit after a save starts the clock; once MAX_INTERVAL_MS elapses a
 * save fires even mid-typing.
 */
export const AUTOSAVE_MAX_INTERVAL_MS = 15000;

/**
 * Body-byte ceiling for the pagehide keepalive flush. The fetch keepalive spec
 * caps the in-flight keepalive body at 64 KiB across a page; an oversize
 * serialized PATCH is SKIPPED (no throw) and covered by the next-open save per
 * B2. Measured on the UTF-8 byte length of the JSON, not its character count.
 */
export const KEEPALIVE_BODY_LIMIT = 64 * 1024;

/**
 * The user's in-memory buffer: the editable title, the ProseMirror content, and
 * the per-sermon citation scope (Phase 50 — the books / collections the citation
 * drawer is limited to). The scope arrays ride the SAME autosave path as the
 * manuscript, so a Scope toggle persists through the existing debounce +
 * single-flight + 409 machinery with no separate request.
 */
export interface EditorSnapshot {
  title: string;
  content: ProseMirrorDoc;
  scopeBookIds: string[];
  scopeCollectionIds: string[];
}

/**
 * True when `next` differs from the last-saved snapshot and is therefore worth
 * a PATCH. `lastSaved === null` means nothing has been saved yet, so any buffer
 * is dirty. Compares the title, a stable JSON serialization of the content
 * (TipTap returns a fresh object every getJSON(), so reference equality is
 * useless), and the two scope arrays (Phase 50: a Scope-only change must still
 * trigger a save) — an unchanged buffer never PATCHes, mirroring reader
 * shouldPersist.
 */
export function isDirty(lastSaved: EditorSnapshot | null, next: EditorSnapshot): boolean {
  if (lastSaved === null) {
    return true;
  }
  if (lastSaved.title !== next.title) {
    return true;
  }
  if (JSON.stringify(lastSaved.scopeBookIds) !== JSON.stringify(next.scopeBookIds)) {
    return true;
  }
  if (JSON.stringify(lastSaved.scopeCollectionIds) !== JSON.stringify(next.scopeCollectionIds)) {
    return true;
  }
  return JSON.stringify(lastSaved.content) !== JSON.stringify(next.content);
}

/**
 * Build the whitelisted PATCH body: `title`, `content`, the current citation
 * scope arrays (Phase 50), and the REQUIRED `base_updated_at` concurrency token.
 * Matches the proxy whitelist (lib/documents.ts whitelistPatchDocument) so
 * nothing extra is ever sent. The scope arrays carry the CURRENT scope on every
 * save (not only when changed), so a content-only edit replays the unchanged
 * scope — an idempotent no-op server-side (the API re-clamps it), never a wipe.
 */
export function buildPatchBody(snapshot: EditorSnapshot, baseUpdatedAt: string): DocumentPatch {
  return {
    base_updated_at: baseUpdatedAt,
    title: snapshot.title,
    content: snapshot.content,
    scope_book_ids: snapshot.scopeBookIds,
    scope_collection_ids: snapshot.scopeCollectionIds,
  };
}

/** UTF-8 byte length of a serialized PATCH body — what the keepalive cap measures. */
export function serializedByteLength(body: DocumentPatch): number {
  return new TextEncoder().encode(JSON.stringify(body)).length;
}

/**
 * The pagehide keepalive flush is allowed only when the doc is dirty AND its
 * serialized body fits the keepalive ceiling. An oversize body returns false:
 * the caller SKIPS the flush silently (status stays unsaved; the next open
 * saves it) rather than throwing or sending a doomed keepalive request.
 */
export function canKeepaliveFlush(
  lastSaved: EditorSnapshot | null,
  next: EditorSnapshot,
  baseUpdatedAt: string,
): boolean {
  if (!isDirty(lastSaved, next)) {
    return false;
  }
  return serializedByteLength(buildPatchBody(next, baseUpdatedAt)) <= KEEPALIVE_BODY_LIMIT;
}

/**
 * The single-flight + coalescing state machine, pure so it is trivially
 * testable. The component holds one `FlightState` in a ref and feeds it three
 * events:
 *  - `requestSave`: an autosave (debounce or max-interval) wants to save. If a
 *    PATCH is already in flight, mark `pending` (a trailing save fires when the
 *    flight resolves) and do NOT start a parallel request.
 *  - `flightSettled`: the in-flight PATCH resolved. If edits arrived during it
 *    (`pending`), the caller should fire exactly ONE more save.
 *
 * Never returns "start a save" while another is in flight — that is the whole
 * point (parallel PATCHes race base_updated_at).
 */
export interface FlightState {
  /** A PATCH is currently awaiting its response. */
  inFlight: boolean;
  /** Edits arrived during the in-flight PATCH; one trailing save is owed. */
  pending: boolean;
}

export function idleFlight(): FlightState {
  return { inFlight: false, pending: false };
}

/**
 * Decide what to do when an autosave is requested. Returns the next state plus
 * whether the caller should actually START a PATCH now. While in flight, the
 * request is folded into `pending` (coalesced) and `start` is false.
 */
export function onSaveRequested(state: FlightState): { state: FlightState; start: boolean } {
  if (state.inFlight) {
    return { state: { inFlight: true, pending: true }, start: false };
  }
  return { state: { inFlight: true, pending: false }, start: true };
}

/**
 * Decide what to do when the in-flight PATCH settles. Returns the next state
 * plus whether a single TRAILING save should fire now (true exactly when edits
 * were coalesced during the flight). The trailing save itself goes back through
 * onSaveRequested, so it re-enters the in-flight state cleanly.
 */
export function onFlightSettled(state: FlightState): { state: FlightState; fireTrailing: boolean } {
  if (state.pending) {
    return { state: idleFlight(), fireTrailing: true };
  }
  return { state: idleFlight(), fireTrailing: false };
}
