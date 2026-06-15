import { render, screen } from "@testing-library/react";
import StarterKit from "@tiptap/starter-kit";
import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * Citation node (Phase 37) tests, in two layers:
 *
 *  1. SCHEMA ROUND-TRIP — a real headless TipTap Editor with the CitationNode +
 *     StarterKit. insertContent a citation -> editor.getJSON() carries the attrs
 *     -> a fresh editor.setContent(thatJSON) re-parses them losslessly. This is
 *     the load-bearing contract: the node must survive save -> reload -> render
 *     through documents.content JSON. Uses the REAL node (no @tiptap mock) — the
 *     ProseMirror schema runs headless in jsdom; getJSON needs no layout.
 *
 *  2. NODE-VIEW DOM — render CitationView directly (the load-bearing JSX, the
 *     pattern citation-chips.test.tsx uses) with a fake `node` and a membership
 *     context, asserting: title + snippet as PLAIN TEXT; the read link to
 *     /read/{bookId}?chunk={n} when owned; the degraded badge (no link) when the
 *     bookId is absent from the context set; ZERO dangerouslySetInnerHTML; ZERO
 *     network fetch in either branch.
 *
 * The node view's React portal (ReactNodeViewRenderer) is NOT exercised here —
 * jsdom cannot lay out a ProseMirror node view — so the view is rendered
 * directly with its props, exactly as the chip test renders its JSX in isolation.
 */

// CitationView reads node.attrs + the membership context; it renders a NodeView-
// Wrapper. Stub the wrapper to a plain <div> (forwarding props/children) so the
// view renders without a ProseMirror node-view host. The schema-round-trip layer
// imports CitationNode for the REAL Node.create, so this mock must also surface
// the genuine @tiptap/react Node/mergeAttributes/ReactNodeViewRenderer.
vi.mock("@tiptap/react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tiptap/react")>();
  return {
    ...actual,
    NodeViewWrapper: ({ children, ...rest }: { children?: React.ReactNode }) => (
      <div {...rest}>{children}</div>
    ),
  };
});

import type { NodeViewProps } from "@tiptap/react";
import { Editor } from "@tiptap/react";
import {
  type CitationAttrs,
  CitationNode,
  CitationView,
} from "../../components/editor/CitationNode";
import { LibraryMembershipProvider } from "../../components/editor/library-membership";

const ATTRS: CitationAttrs = {
  bookId: "book-123",
  chunkIndex: 42,
  bookTitle: "Knowing God",
  snippet: "Grace is the love of God shown to the unlovely.",
  parentSection: "Chapter 2",
};

/** Build a headless editor with the real citation node + StarterKit. */
function makeEditor(content?: unknown): Editor {
  return new Editor({
    extensions: [StarterKit, CitationNode],
    content: (content ?? { type: "doc", content: [{ type: "paragraph" }] }) as never,
  });
}

/** Fake the slice of NodeViewProps CitationView actually reads (node.attrs). */
function viewProps(attrs: CitationAttrs): NodeViewProps {
  return { node: { attrs } } as unknown as NodeViewProps;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("CitationNode — schema round-trip", () => {
  it("insertContent persists every attr into editor.getJSON()", () => {
    const editor = makeEditor();
    editor.commands.insertContent({ type: "citation", attrs: ATTRS });

    const json = editor.getJSON() as {
      content?: { type: string; attrs?: Record<string, unknown> }[];
    };
    const node = json.content?.find((n) => n.type === "citation");
    expect(node).toBeDefined();
    expect(node?.attrs).toMatchObject({
      bookId: "book-123",
      chunkIndex: 42,
      bookTitle: "Knowing God",
      snippet: "Grace is the love of God shown to the unlovely.",
      parentSection: "Chapter 2",
    });
    editor.destroy();
  });

  it("round-trips attrs through getJSON() -> setContent() (save -> reload)", () => {
    const first = makeEditor();
    first.commands.insertContent({ type: "citation", attrs: ATTRS });
    const saved = first.getJSON();
    first.destroy();

    // A fresh editor re-parses the saved doc — the reload path.
    const second = makeEditor(saved);
    const reparsed = second.getJSON() as {
      content?: { type: string; attrs?: Record<string, unknown> }[];
    };
    const node = reparsed.content?.find((n) => n.type === "citation");
    expect(node?.attrs).toMatchObject({
      bookId: "book-123",
      chunkIndex: 42,
      bookTitle: "Knowing God",
      snippet: "Grace is the love of God shown to the unlovely.",
      parentSection: "Chapter 2",
    });
    second.destroy();
  });

  it("preserves a null parentSection through the round-trip", () => {
    const editor = makeEditor();
    editor.commands.insertContent({
      type: "citation",
      attrs: { ...ATTRS, parentSection: null },
    });
    const json = editor.getJSON() as {
      content?: { type: string; attrs?: Record<string, unknown> }[];
    };
    const node = json.content?.find((n) => n.type === "citation");
    expect(node?.attrs?.parentSection).toBeNull();
    editor.destroy();
  });

  it("is a block-level atom in the schema", () => {
    const editor = makeEditor();
    const type = editor.schema.nodes.citation;
    expect(type).toBeDefined();
    expect(type?.isAtom).toBe(true);
    expect(type?.isBlock).toBe(true);
    editor.destroy();
  });
});

describe("CitationView — owned (in library)", () => {
  it("renders title + snippet as plain text and a read link to the cited chunk", () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);

    render(
      <LibraryMembershipProvider ownedBookIds={new Set(["book-123"])}>
        <CitationView {...viewProps(ATTRS)} />
      </LibraryMembershipProvider>,
    );

    expect(screen.getByText("Knowing God")).toBeInTheDocument();
    expect(screen.getByText("Grace is the love of God shown to the unlovely.")).toBeInTheDocument();
    expect(screen.getByText(/Chapter 2 ·/)).toBeInTheDocument();

    const link = screen.getByTestId("citation-read-link");
    expect(link).toHaveAttribute("href", "/read/book-123?chunk=42");
    expect(screen.queryByTestId("citation-degraded-badge")).not.toBeInTheDocument();

    // The node view renders purely from attrs — zero network calls.
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe("CitationView — degraded (book left the library)", () => {
  it("shows the cached snippet + a degraded badge and NO link, with no fetch", () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);

    // The owned set does NOT contain this book — the degraded branch.
    render(
      <LibraryMembershipProvider ownedBookIds={new Set(["some-other-book"])}>
        <CitationView {...viewProps(ATTRS)} />
      </LibraryMembershipProvider>,
    );

    // Cached snippet + title still render (the doc is self-contained).
    expect(screen.getByText("Knowing God")).toBeInTheDocument();
    expect(screen.getByText("Grace is the love of God shown to the unlovely.")).toBeInTheDocument();

    const badge = screen.getByTestId("citation-degraded-badge");
    expect(badge).toHaveTextContent(/no longer in your library/i);
    // No read link in the degraded state.
    expect(screen.queryByTestId("citation-read-link")).not.toBeInTheDocument();

    // ZERO per-citation fetches — the badge is decided from context.
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("treats a citation with no provider as degraded (empty default set)", () => {
    render(<CitationView {...viewProps(ATTRS)} />);
    expect(screen.getByTestId("citation-degraded-badge")).toBeInTheDocument();
    expect(screen.queryByTestId("citation-read-link")).not.toBeInTheDocument();
  });
});

describe("CitationView — security", () => {
  it("renders the snippet as PLAIN TEXT (no HTML injection, zero dangerouslySetInnerHTML)", () => {
    const malicious: CitationAttrs = {
      ...ATTRS,
      snippet: "<img src=x onerror=alert(1)>hello",
    };
    const { container } = render(
      <LibraryMembershipProvider ownedBookIds={new Set(["book-123"])}>
        <CitationView {...viewProps(malicious)} />
      </LibraryMembershipProvider>,
    );

    // The raw markup is shown as literal text, never parsed into an <img>.
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("<img src=x onerror=alert(1)>hello")).toBeInTheDocument();
  });

  it("drops a `<`-bearing EPUB tag-soup section label (displaySection guard)", () => {
    render(
      <LibraryMembershipProvider ownedBookIds={new Set(["book-123"])}>
        <CitationView {...viewProps({ ...ATTRS, parentSection: '<a href="x.html#y">' })} />
      </LibraryMembershipProvider>,
    );
    // Tag soup is dropped; only the chunk label remains.
    expect(screen.getByText("chunk 42")).toBeInTheDocument();
    expect(screen.queryByText(/x\.html/)).not.toBeInTheDocument();
  });
});
