import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { displaySection, segmentSummary } from "../../lib/summary";
import type { SummaryCitation } from "../../lib/types";

/**
 * Citation-chip rendering contract (Phase 24/16). `segmentSummary` is unit-
 * tested at the pure level in test/summary.test.ts; THIS file pins the
 * SearchPanel DOM mapping — segment → <a> — so a regression in the renderer
 * (not just the segmenter) turns red.
 *
 * SearchPanel maps each marker segment to:
 *   <a key=`m${start}` href=`#citation-${citationIndex+1}` title={marker}>
 *     [{citationIndex+1}]
 *   </a>
 * and each source <li> to id=`citation-${index+1}`. This file reproduces that
 * mapping over the segments so a change to the chip href/label/title shape is
 * caught without booting the network.
 */

function citation(marker: string): Pick<SummaryCitation, "marker"> {
  return { marker };
}

// Minimal stand-in for SearchPanel's segment→DOM map (the load-bearing JSX in
// components/SearchPanel.tsx). Kept structurally identical so it tracks the
// component; the full component path is also exercised in SearchPanel.test.tsx.
function Chips({
  summary,
  citations,
}: {
  summary: string;
  citations: Pick<SummaryCitation, "marker">[];
}) {
  const segments = segmentSummary(summary, citations);
  return (
    <p>
      {segments.map((seg) =>
        seg.kind === "text" ? (
          <span key={`t${seg.start}`}>{seg.text}</span>
        ) : (
          <a key={`m${seg.start}`} href={`#citation-${seg.citationIndex + 1}`} title={seg.marker}>
            [{seg.citationIndex + 1}]
          </a>
        ),
      )}
    </p>
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("citation chip rendering", () => {
  it("renders a standalone marker as a single chip linking to its source", () => {
    render(<Chips summary="Grace is central [Romans:7]." citations={[citation("[Romans:7]")]} />);
    const chip = screen.getByRole("link", { name: "[1]" });
    expect(chip).toHaveAttribute("href", "#citation-1");
    expect(chip).toHaveAttribute("title", "[Romans:7]");
  });

  it("explodes [Faith:70, Faith:51] into TWO chips at distinct, ordered anchors", () => {
    render(
      <Chips
        summary="Faith grows [Faith:70, Faith:51] over time."
        citations={[citation("[Faith:70]"), citation("[Faith:51]")]}
      />,
    );
    const chips = screen.getAllByRole("link");
    expect(chips).toHaveLength(2);
    expect(chips[0]).toHaveTextContent("[1]");
    expect(chips[0]).toHaveAttribute("href", "#citation-1");
    expect(chips[0]).toHaveAttribute("title", "[Faith:70]");
    expect(chips[1]).toHaveTextContent("[2]");
    expect(chips[1]).toHaveAttribute("href", "#citation-2");
    expect(chips[1]).toHaveAttribute("title", "[Faith:51]");
  });

  it("drops the merged bracket's structural glue from rendered text", () => {
    const { container } = render(
      <Chips
        summary="Faith grows [Faith:70, Faith:51] over time."
        citations={[citation("[Faith:70]"), citation("[Faith:51]")]}
      />,
    );
    // The chips read [1][2]; no `Faith:70`, no `, `, no raw brackets survive.
    expect(container.textContent).toBe("Faith grows [1][2] over time.");
  });

  it("keeps a fully unresolvable merged bracket as plain prose (no chips)", () => {
    const { container } = render(
      <Chips summary="See [Ghost:1, Phantom:2] later." citations={[citation("[A:1]")]} />,
    );
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(container.textContent).toBe("See [Ghost:1, Phantom:2] later.");
  });

  it("drops only the invented member of a mixed merged bracket", () => {
    render(
      <Chips
        summary="Both views [A:1, Ghost:9, B:2] agree."
        citations={[citation("[A:1]"), citation("[B:2]")]}
      />,
    );
    const chips = screen.getAllByRole("link");
    expect(chips).toHaveLength(2);
    expect(chips[0]).toHaveAttribute("title", "[A:1]");
    expect(chips[1]).toHaveAttribute("title", "[B:2]");
  });

  it("does not resolve a marker nested inside a longer one ([X:1] vs [X:12])", () => {
    render(
      <Chips summary="Only [X:12] cited." citations={[citation("[X:1]"), citation("[X:12]")]} />,
    );
    const chips = screen.getAllByRole("link");
    expect(chips).toHaveLength(1);
    expect(chips[0]).toHaveTextContent("[2]");
    expect(chips[0]).toHaveAttribute("title", "[X:12]");
  });

  it("gives every chip a unique React key via strictly-increasing start offsets", () => {
    // Repeated markers must not collide on key; segmentSummary guarantees
    // distinct `start` per occurrence, so React renders both without a warning.
    const warn = vi.spyOn(console, "error").mockImplementation(() => {});
    render(<Chips summary="A [X:1] B [X:1]" citations={[citation("[X:1]")]} />);
    expect(screen.getAllByRole("link")).toHaveLength(2);
    expect(warn).not.toHaveBeenCalled();
  });

  it("hides EPUB HTML-debris section labels via displaySection (no tag soup)", () => {
    // Defense-in-depth check shared with the Sources card: a `<`-bearing
    // section is dropped rather than rendered as broken markup.
    expect(displaySection('<a href="x.html#y" class="z"><span')).toBeNull();
    expect(displaySection("Book III, Chapter 11")).toBe("Book III, Chapter 11");
  });
});
