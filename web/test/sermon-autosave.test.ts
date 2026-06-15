import { describe, expect, it } from "vitest";
import {
  AUTOSAVE_DEBOUNCE_MS,
  AUTOSAVE_MAX_INTERVAL_MS,
  type EditorSnapshot,
  KEEPALIVE_BODY_LIMIT,
  buildPatchBody,
  canKeepaliveFlush,
  idleFlight,
  isDirty,
  onFlightSettled,
  onSaveRequested,
  serializedByteLength,
} from "../lib/sermon-autosave";
import type { ProseMirrorDoc } from "../lib/types";

function doc(text: string): ProseMirrorDoc {
  return {
    type: "doc",
    content: [{ type: "paragraph", content: [{ type: "text", text }] }],
  };
}

function snap(title: string, text: string): EditorSnapshot {
  return { title, content: doc(text) };
}

describe("timing constants", () => {
  it("are the spec's 2 s debounce / 15 s max-interval", () => {
    expect(AUTOSAVE_DEBOUNCE_MS).toBe(2000);
    expect(AUTOSAVE_MAX_INTERVAL_MS).toBe(15000);
    // The max-interval ceiling must exceed the debounce or it never wins.
    expect(AUTOSAVE_MAX_INTERVAL_MS).toBeGreaterThan(AUTOSAVE_DEBOUNCE_MS);
  });

  it("the keepalive ceiling is the spec's ~64 KB", () => {
    expect(KEEPALIVE_BODY_LIMIT).toBe(65536);
  });
});

describe("isDirty", () => {
  it("is always dirty when nothing has been saved yet", () => {
    expect(isDirty(null, snap("t", "hello"))).toBe(true);
  });

  it("is clean for an identical title + content (TipTap returns fresh objects)", () => {
    const saved = snap("Sermon", "body");
    // A distinct-but-equal content object — reference equality would miss this.
    const same = snap("Sermon", "body");
    expect(saved.content).not.toBe(same.content);
    expect(isDirty(saved, same)).toBe(false);
  });

  it("is dirty on a title-only change", () => {
    expect(isDirty(snap("Old", "body"), snap("New", "body"))).toBe(true);
  });

  it("is dirty on a content-only change", () => {
    expect(isDirty(snap("t", "before"), snap("t", "after"))).toBe(true);
  });
});

describe("buildPatchBody", () => {
  it("emits exactly the three whitelisted fields with the base token", () => {
    const body = buildPatchBody(snap("Title", "x"), "2026-06-15T10:00:00Z");
    expect(Object.keys(body).sort()).toEqual(["base_updated_at", "content", "title"]);
    expect(body.base_updated_at).toBe("2026-06-15T10:00:00Z");
    expect(body.title).toBe("Title");
    expect(body.content).toEqual(doc("x"));
  });
});

describe("serializedByteLength", () => {
  it("measures UTF-8 bytes, not characters (multibyte counts >1)", () => {
    const ascii = serializedByteLength(buildPatchBody(snap("a", "a"), "b"));
    const emoji = serializedByteLength(buildPatchBody(snap("a", "\u{1F600}"), "b"));
    // The emoji body must be strictly larger by its multibyte UTF-8 expansion.
    expect(emoji).toBeGreaterThan(ascii);
  });
});

describe("canKeepaliveFlush", () => {
  const base = "2026-06-15T10:00:00Z";

  it("is false when the buffer is not dirty (nothing to flush)", () => {
    const saved = snap("t", "body");
    expect(canKeepaliveFlush(saved, snap("t", "body"), base)).toBe(false);
  });

  it("is true for a dirty, in-budget body", () => {
    expect(canKeepaliveFlush(null, snap("t", "small body"), base)).toBe(true);
  });

  it("SKIPS (false) a dirty body over the ~64 KB keepalive ceiling", () => {
    // A paragraph well past 64 KB of text.
    const huge = snap("t", "x".repeat(KEEPALIVE_BODY_LIMIT + 1));
    expect(serializedByteLength(buildPatchBody(huge, base))).toBeGreaterThan(KEEPALIVE_BODY_LIMIT);
    expect(canKeepaliveFlush(null, huge, base)).toBe(false);
  });

  it("allows a body sitting exactly at the ceiling (<= is inclusive)", () => {
    const baseline = serializedByteLength(buildPatchBody(snap("t", ""), base));
    // Pad the text so the serialized body lands exactly on the limit.
    const padded = snap("t", "x".repeat(KEEPALIVE_BODY_LIMIT - baseline));
    expect(serializedByteLength(buildPatchBody(padded, base))).toBe(KEEPALIVE_BODY_LIMIT);
    expect(canKeepaliveFlush(null, padded, base)).toBe(true);
  });
});

describe("single-flight coalescing", () => {
  it("starts a PATCH from idle", () => {
    const { state, start } = onSaveRequested(idleFlight());
    expect(start).toBe(true);
    expect(state).toEqual({ inFlight: true, pending: false });
  });

  it("never starts a parallel PATCH while one is in flight — it coalesces", () => {
    const first = onSaveRequested(idleFlight());
    // A second autosave fires while the first is still awaiting its response.
    const second = onSaveRequested(first.state);
    expect(second.start).toBe(false);
    expect(second.state).toEqual({ inFlight: true, pending: true });
    // A third request collapses into the same single pending save.
    const third = onSaveRequested(second.state);
    expect(third.start).toBe(false);
    expect(third.state).toEqual({ inFlight: true, pending: true });
  });

  it("fires exactly ONE trailing save when edits arrived during the flight", () => {
    const inFlight = onSaveRequested(idleFlight()).state;
    const withPending = onSaveRequested(inFlight).state;
    const settled = onFlightSettled(withPending);
    expect(settled.fireTrailing).toBe(true);
    expect(settled.state).toEqual(idleFlight());
  });

  it("fires no trailing save when no edits arrived during the flight", () => {
    const inFlight = onSaveRequested(idleFlight()).state;
    const settled = onFlightSettled(inFlight);
    expect(settled.fireTrailing).toBe(false);
    expect(settled.state).toEqual(idleFlight());
  });

  it("a trailing save re-enters the in-flight state cleanly (the loop drains)", () => {
    // flight 1 with a coalesced edit -> settle fires trailing
    let state = onSaveRequested(idleFlight()).state;
    state = onSaveRequested(state).state; // edit during flight 1
    const afterFirst = onFlightSettled(state);
    expect(afterFirst.fireTrailing).toBe(true);
    // The trailing save starts flight 2 from idle...
    const trailing = onSaveRequested(afterFirst.state);
    expect(trailing.start).toBe(true);
    // ...and with no further edits, flight 2 drains to idle with no trailing.
    const afterSecond = onFlightSettled(trailing.state);
    expect(afterSecond.fireTrailing).toBe(false);
    expect(afterSecond.state).toEqual(idleFlight());
  });
});
