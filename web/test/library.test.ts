import { describe, expect, it } from "vitest";
import { formatProgress, readHref } from "../lib/library";

describe("readHref", () => {
  it("links the reader root so a saved position is resumed (library rows)", () => {
    expect(readHref("0f5e3a8c-1d2b-4c3d-9e8f-7a6b5c4d3e2f")).toBe(
      "/read/0f5e3a8c-1d2b-4c3d-9e8f-7a6b5c4d3e2f",
    );
  });

  it("deep-links ?chunk=N for citation cards", () => {
    expect(readHref("abc", 42)).toBe("/read/abc?chunk=42");
  });

  it("treats chunk 0 as a real anchor, not a missing one", () => {
    expect(readHref("abc", 0)).toBe("/read/abc?chunk=0");
  });

  it("URI-encodes the book id segment", () => {
    expect(readHref("a/b?c")).toBe("/read/a%2Fb%3Fc");
  });
});

describe("formatProgress", () => {
  it("returns null when the API reports no progress (no saved position)", () => {
    expect(formatProgress(null)).toBeNull();
  });

  it("rounds to a whole percentage", () => {
    expect(formatProgress(0.424)).toBe("42%");
    expect(formatProgress(0.425)).toBe("43%");
  });

  it("renders the endpoints exactly", () => {
    expect(formatProgress(0)).toBe("0%");
    expect(formatProgress(1)).toBe("100%");
  });

  it("shows 0% for a just-started long book rather than hiding it", () => {
    expect(formatProgress(1 / 600)).toBe("0%");
  });

  it("re-clamps out-of-range values defensively (API already clamps to 1.0)", () => {
    expect(formatProgress(1.2)).toBe("100%");
    expect(formatProgress(-0.5)).toBe("0%");
  });

  it("drops non-finite garbage instead of rendering NaN%", () => {
    expect(formatProgress(Number.NaN)).toBeNull();
    expect(formatProgress(Number.POSITIVE_INFINITY)).toBeNull();
  });
});
