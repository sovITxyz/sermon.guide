import { describe, expect, it } from "vitest";
import { whitelistCreateEvent, whitelistPatchEvent } from "../lib/calendar";

describe("whitelistCreateEvent", () => {
  it("forwards exactly event_date and title when only the required fields are set", () => {
    const result = whitelistCreateEvent({ event_date: "2028-03-15", title: "Easter" });
    expect(result).toEqual({ ok: true, body: { event_date: "2028-03-15", title: "Easter" } });
  });

  it("forwards series and repeat_weekly_until when present (incl. null series)", () => {
    const result = whitelistCreateEvent({
      event_date: "2028-03-15",
      title: "Easter",
      series: "Resurrection",
      repeat_weekly_until: "2028-04-12",
    });
    expect(result).toEqual({
      ok: true,
      body: {
        event_date: "2028-03-15",
        title: "Easter",
        series: "Resurrection",
        repeat_weekly_until: "2028-04-12",
      },
    });

    const nullSeries = whitelistCreateEvent({
      event_date: "2028-03-15",
      title: "Easter",
      series: null,
    });
    expect(nullSeries).toEqual({
      ok: true,
      body: { event_date: "2028-03-15", title: "Easter", series: null },
    });
  });

  it("omits absent optional fields rather than sending null", () => {
    const result = whitelistCreateEvent({ event_date: "2028-03-15", title: "Easter" });
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect("series" in result.body).toBe(false);
      expect("repeat_weekly_until" in result.body).toBe(false);
    }
  });

  it("DROPS document_id — Phase 41 deferral, never reaches the upstream body", () => {
    const result = whitelistCreateEvent({
      event_date: "2028-03-15",
      title: "Easter",
      document_id: "11111111-1111-1111-1111-111111111111",
    });
    expect(result).toEqual({ ok: true, body: { event_date: "2028-03-15", title: "Easter" } });
  });

  it("drops other unknown keys (e.g. a smuggled event_id / user_id)", () => {
    const result = whitelistCreateEvent({
      event_date: "2028-03-15",
      title: "Easter",
      event_id: "22222222-2222-2222-2222-222222222222",
      user_id: "33333333-3333-3333-3333-333333333333",
      created_at: "2028-01-01T00:00:00Z",
    });
    expect(result).toEqual({ ok: true, body: { event_date: "2028-03-15", title: "Easter" } });
  });

  it("rejects a missing or non-string event_date / title", () => {
    expect(whitelistCreateEvent({ title: "Easter" }).ok).toBe(false);
    expect(whitelistCreateEvent({ event_date: "2028-03-15" }).ok).toBe(false);
    expect(whitelistCreateEvent({ event_date: 20280315, title: "Easter" }).ok).toBe(false);
    expect(whitelistCreateEvent({ event_date: "2028-03-15", title: 12 }).ok).toBe(false);
  });

  it("rejects a series / repeat_weekly_until that is neither string nor null", () => {
    expect(whitelistCreateEvent({ event_date: "2028-03-15", title: "Easter", series: 12 }).ok).toBe(
      false,
    );
    expect(
      whitelistCreateEvent({ event_date: "2028-03-15", title: "Easter", repeat_weekly_until: 5 })
        .ok,
    ).toBe(false);
  });

  it("rejects non-object bodies (malformed JSON parses to null upstream of this)", () => {
    for (const body of [null, undefined, [], "{}", 12, true]) {
      expect(whitelistCreateEvent(body).ok).toBe(false);
    }
  });

  it("leaves length/range/cap validation to the API — structurally-valid junk passes", () => {
    // Empty title, malformed date string, and an out-of-order repeat are all the
    // API's 422 to own; the proxy only checks primitive types.
    expect(whitelistCreateEvent({ event_date: "", title: "" }).ok).toBe(true);
    expect(whitelistCreateEvent({ event_date: "not-a-date", title: "x" }).ok).toBe(true);
    expect(
      whitelistCreateEvent({
        event_date: "2028-03-15",
        title: "x",
        repeat_weekly_until: "2028-03-01",
      }).ok,
    ).toBe(true);
  });
});

describe("whitelistPatchEvent", () => {
  it("forwards event_date, title, and series when all are present", () => {
    const result = whitelistPatchEvent({
      event_date: "2028-03-22",
      title: "Updated",
      series: "Lent",
    });
    expect(result).toEqual({
      ok: true,
      body: { event_date: "2028-03-22", title: "Updated", series: "Lent" },
    });
  });

  it("omits absent fields — present-only partial semantics", () => {
    const titleOnly = whitelistPatchEvent({ title: "Updated" });
    expect(titleOnly).toEqual({ ok: true, body: { title: "Updated" } });
    if (titleOnly.ok) {
      expect("event_date" in titleOnly.body).toBe(false);
      expect("series" in titleOnly.body).toBe(false);
    }
  });

  it("forwards series: null verbatim to DETACH the series (a meaningful value)", () => {
    const result = whitelistPatchEvent({ series: null });
    expect(result).toEqual({ ok: true, body: { series: null } });
    if (result.ok) {
      expect("series" in result.body).toBe(true);
      expect(result.body.series).toBeNull();
    }
  });

  it("forwards a present document_id UUID verbatim to RE-LINK a sermon (Phase 41)", () => {
    const result = whitelistPatchEvent({
      title: "Updated",
      document_id: "11111111-1111-1111-1111-111111111111",
    });
    expect(result).toEqual({
      ok: true,
      body: { title: "Updated", document_id: "11111111-1111-1111-1111-111111111111" },
    });
  });

  it("forwards document_id: null verbatim to UNLINK the sermon (three-state, not truthiness)", () => {
    // The single most likely Phase 41 defect: a truthiness guard would DROP this
    // null and silently break unlink. Key-presence must let an explicit null pass.
    const result = whitelistPatchEvent({ document_id: null });
    expect(result).toEqual({ ok: true, body: { document_id: null } });
    if (result.ok) {
      expect("document_id" in result.body).toBe(true);
      expect(result.body.document_id).toBeNull();
    }
  });

  it("omits document_id when ABSENT — leaves the existing link alone", () => {
    const result = whitelistPatchEvent({ title: "Updated" });
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect("document_id" in result.body).toBe(false);
    }
  });

  it("rejects a document_id that is neither string nor null", () => {
    expect(whitelistPatchEvent({ document_id: 12 }).ok).toBe(false);
    expect(whitelistPatchEvent({ document_id: {} }).ok).toBe(false);
    expect(whitelistPatchEvent({ document_id: [] }).ok).toBe(false);
  });

  it("DROPS repeat_weekly_until — not a PATCH field on the API (would 422 on extra=forbid)", () => {
    const result = whitelistPatchEvent({ title: "Updated", repeat_weekly_until: "2028-04-12" });
    expect(result).toEqual({ ok: true, body: { title: "Updated" } });
  });

  it("drops other unknown keys (event_id / user_id / created_at)", () => {
    const result = whitelistPatchEvent({
      title: "Updated",
      event_id: "22222222-2222-2222-2222-222222222222",
      user_id: "33333333-3333-3333-3333-333333333333",
      created_at: "2028-01-01T00:00:00Z",
    });
    expect(result).toEqual({ ok: true, body: { title: "Updated" } });
  });

  it("rejects wrong primitive types for the optional fields", () => {
    expect(whitelistPatchEvent({ event_date: 20280315 }).ok).toBe(false);
    expect(whitelistPatchEvent({ title: 12 }).ok).toBe(false);
    expect(whitelistPatchEvent({ series: 12 }).ok).toBe(false);
  });

  it("rejects non-object bodies", () => {
    for (const body of [null, undefined, [], "{}", 12, true]) {
      expect(whitelistPatchEvent(body).ok).toBe(false);
    }
  });

  it("leaves the at-least-one-of and length rules to the API — an empty patch passes structurally", () => {
    // The API owns the 'PATCH must set at least one field' 422 and the length
    // checks; the proxy only guarantees the structural shape.
    expect(whitelistPatchEvent({}).ok).toBe(true);
    expect(whitelistPatchEvent({ title: "" }).ok).toBe(true);
  });
});
