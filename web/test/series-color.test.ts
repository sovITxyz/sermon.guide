import { describe, expect, it } from "vitest";
import { NO_SERIES_COLOR, SERIES_PALETTE, seriesColor } from "../lib/series-color";

describe("SERIES_PALETTE — literal Tailwind classes only", () => {
  it("is a non-empty closed set of fully-literal class strings", () => {
    expect(SERIES_PALETTE.length).toBeGreaterThan(0);
    for (const color of SERIES_PALETTE) {
      // Each must be a complete literal utility class (no interpolation markers)
      // so Tailwind's content scanner emits it; a runtime-built class would be
      // purged and render unstyled.
      expect(color.bg).toMatch(/^bg-[a-z]+-\d{2,3}$/);
      expect(color.text).toMatch(/^text-[a-z]+-\d{2,3}$/);
      expect(color.dot).toMatch(/^bg-[a-z]+-\d{2,3}$/);
      expect(color.bg).not.toContain("${");
      expect(color.text).not.toContain("${");
      expect(color.dot).not.toContain("${");
    }
  });
});

describe("seriesColor — deterministic series → color", () => {
  it("maps the same label to the same color every time (no shared state)", () => {
    const a = seriesColor("Romans");
    const b = seriesColor("Romans");
    expect(a).toEqual(b);
    // Independent of call interleaving with other labels.
    seriesColor("Advent");
    seriesColor("Psalms");
    expect(seriesColor("Romans")).toEqual(a);
  });

  it("returns a palette color for a non-empty label", () => {
    const color = seriesColor("Sermon on the Mount");
    expect(SERIES_PALETTE).toContainEqual(color);
    expect(color).not.toEqual(NO_SERIES_COLOR);
  });

  it("returns the neutral color for a null or empty series", () => {
    expect(seriesColor(null)).toEqual(NO_SERIES_COLOR);
    expect(seriesColor("")).toEqual(NO_SERIES_COLOR);
  });

  it("the neutral color is a literal triple, never indexed from the palette", () => {
    expect(NO_SERIES_COLOR).toEqual({
      bg: "bg-gray-100",
      text: "text-gray-700",
      dot: "bg-gray-400",
    });
  });

  it("distributes a realistic set of series labels across the whole palette", () => {
    const labels = [
      "Romans",
      "Advent",
      "Sermon on the Mount",
      "Psalms",
      "John",
      "Genesis",
      "Acts",
      "Galatians",
      "Easter",
      "Lent",
      "Holy Spirit",
      "Grace",
    ];
    const used = new Set(labels.map((l) => seriesColor(l).dot));
    // Every palette slot is reachable from this realistic label set — the hash
    // is not collapsing everything into one or two colors.
    expect(used.size).toBe(SERIES_PALETTE.length);
  });

  it("is order-stable: a label's slot is a pure function of its hash", () => {
    // Re-deriving the index by hand from the documented hash must match.
    function hashString(value: string): number {
      let hash = 0;
      for (let i = 0; i < value.length; i += 1) {
        hash = (hash * 31 + value.charCodeAt(i)) | 0;
      }
      return hash >>> 0;
    }
    for (const label of ["Romans", "Advent", "John"]) {
      const expected = SERIES_PALETTE[hashString(label) % SERIES_PALETTE.length];
      expect(seriesColor(label)).toEqual(expected);
    }
  });
});
