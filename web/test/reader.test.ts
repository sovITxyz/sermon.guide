import { describe, expect, it } from "vitest";
import { whitelistPositionUpdate } from "../lib/reader";

describe("whitelistPositionUpdate", () => {
  it("forwards chunk_index alone, omitting offset_ratio so the API clears it to NULL", () => {
    const result = whitelistPositionUpdate({ chunk_index: 12 });
    expect(result).toEqual({ ok: true, body: { chunk_index: 12 } });
    if (result.ok) {
      expect("offset_ratio" in result.body).toBe(false);
    }
  });

  it("forwards offset_ratio when present, including an explicit null", () => {
    expect(whitelistPositionUpdate({ chunk_index: 3, offset_ratio: 0.5 })).toEqual({
      ok: true,
      body: { chunk_index: 3, offset_ratio: 0.5 },
    });
    expect(whitelistPositionUpdate({ chunk_index: 3, offset_ratio: null })).toEqual({
      ok: true,
      body: { chunk_index: 3, offset_ratio: null },
    });
  });

  it("drops unknown keys — a smuggled user_id never reaches the upstream body", () => {
    const result = whitelistPositionUpdate({
      chunk_index: 7,
      offset_ratio: 0.25,
      user_id: "11111111-1111-1111-1111-111111111111",
      book_id: "22222222-2222-2222-2222-222222222222",
    });
    expect(result).toEqual({ ok: true, body: { chunk_index: 7, offset_ratio: 0.25 } });
  });

  it("rejects non-object bodies (malformed JSON parses to null upstream of this)", () => {
    for (const body of [null, undefined, [], "12", 12, true]) {
      const result = whitelistPositionUpdate(body);
      expect(result.ok).toBe(false);
    }
  });

  it("rejects wrong primitive types for chunk_index and offset_ratio", () => {
    expect(whitelistPositionUpdate({ chunk_index: "12" }).ok).toBe(false);
    expect(whitelistPositionUpdate({ offset_ratio: 0.5 }).ok).toBe(false);
    expect(whitelistPositionUpdate({ chunk_index: 1, offset_ratio: "0.5" }).ok).toBe(false);
  });

  it("leaves range validation to the API — out-of-range values pass structurally", () => {
    // chunk_index ge=0 and offset_ratio 0.0-1.0 are the API's 422 contract
    // (api/reader.py PositionUpdate); the proxy must not duplicate it.
    expect(whitelistPositionUpdate({ chunk_index: -1 }).ok).toBe(true);
    expect(whitelistPositionUpdate({ chunk_index: 0, offset_ratio: 2.5 }).ok).toBe(true);
  });
});
