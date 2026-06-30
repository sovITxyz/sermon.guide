import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { Editor } from "@tiptap/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LibraryDrawer } from "../../components/editor/LibraryDrawer";
import type { SearchHit, SearchResponse } from "../../lib/types";
import { installFetch, jsonResponse } from "./helpers";

/**
 * LibraryDrawer (Phase 37) tests. The drawer reuses the SearchPanel plumbing but
 * hits the NEW /api/search proxy (raw hits, no LLM) and INSERTS a citation node
 * on a hit click. These tests assert the drawer's contract:
 *  - it POSTs the whitelist-shaped body to /api/search and renders the raw hits;
 *  - a hit row maps the RAW hit -> citation attrs and INSERTS via the editor's
 *    chain().focus().insertContent(...) — caching snippet + sourcing bookTitle
 *    from the one-shot {book_id -> title} map (a raw hit carries no title);
 *  - title + snippet render as PLAIN TEXT (zero dangerouslySetInnerHTML);
 *  - the proxy's {error} body surfaces; a client-side empty query never fetches.
 *
 * The TipTap Editor is faked to the slice the drawer uses (chain/focus/
 * insertContent/run) so the insert call + its argument can be asserted without
 * a real ProseMirror instance (jsdom can't lay one out).
 */

interface InsertCall {
  type: string;
  attrs: Record<string, unknown>;
}

interface FakeEditor {
  inserted: InsertCall[];
  focused: number;
  chain: () => FakeChain;
}
interface FakeChain {
  focus: () => FakeChain;
  insertContent: (content: InsertCall) => FakeChain;
  run: () => boolean;
}

function makeFakeEditor(): FakeEditor {
  const editor: FakeEditor = {
    inserted: [],
    focused: 0,
    chain() {
      const chain: FakeChain = {
        focus: () => {
          editor.focused += 1;
          return chain;
        },
        insertContent: (content) => {
          editor.inserted.push(content);
          return chain;
        },
        run: () => true,
      };
      return chain;
    },
  };
  return editor;
}

/** A raw POST /search hit (api/search.py SearchHit), the shape the proxy returns. */
function hit(overrides: Partial<SearchHit> = {}): SearchHit {
  return {
    book_id: "book-1",
    content_chunk: "Grace is the unearned favor of God.",
    metadata: { filename: "grace.epub", chunk_index: 12, parent_section: "Chapter 1" },
    score: 0.9,
    ...overrides,
  };
}

function searchResponse(hits: SearchHit[]): SearchResponse {
  return { hits, degraded: [] };
}

const TITLES = new Map([["book-1", "On Grace"]]);

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function renderDrawer(editor: FakeEditor) {
  const onClose = vi.fn();
  render(
    <LibraryDrawer editor={editor as unknown as Editor} bookTitles={TITLES} onClose={onClose} />,
  );
  return { onClose };
}

function renderScopedDrawer(
  editor: FakeEditor,
  scope: { book_ids?: string[]; collection_ids?: string[] },
) {
  render(
    <LibraryDrawer
      editor={editor as unknown as Editor}
      bookTitles={TITLES}
      scope={scope}
      onClose={vi.fn()}
    />,
  );
}

function submitQuery(value: string): void {
  fireEvent.change(screen.getByLabelText("Search your library"), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
}

describe("LibraryDrawer — search", () => {
  it("POSTs {query} to /api/search and renders the raw hits with title + snippet", async () => {
    const fetchStub = installFetch(() => Promise.resolve(jsonResponse(searchResponse([hit()]))));
    renderDrawer(makeFakeEditor());

    submitQuery("grace");

    await screen.findByText("On Grace");
    // Title comes from the one-shot library map; snippet is the raw content_chunk.
    expect(screen.getByText("Grace is the unearned favor of God.")).toBeInTheDocument();
    expect(screen.getByText(/Chapter 1 ·/)).toBeInTheDocument();
    // The meta line interpolates the chunk index into the same span as the
    // section label, so assert on the row's text content rather than an exact node.
    expect(screen.getByTestId("library-drawer-hit")).toHaveTextContent("chunk 12");

    const [url, init] = fetchStub.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/search");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ query: "grace" });
  });

  it("folds a non-empty scope into the /api/search POST body (Phase 49)", async () => {
    const fetchStub = installFetch(() => Promise.resolve(jsonResponse(searchResponse([hit()]))));
    renderScopedDrawer(makeFakeEditor(), { book_ids: ["b1", "b2"], collection_ids: ["c1"] });

    submitQuery("grace");
    await screen.findByText("On Grace");

    const [url, init] = fetchStub.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/search");
    expect(JSON.parse(init.body as string)).toEqual({
      query: "grace",
      book_ids: ["b1", "b2"],
      collection_ids: ["c1"],
    });
  });

  it("omits empty scope arrays — an empty selection searches the whole library", async () => {
    const fetchStub = installFetch(() => Promise.resolve(jsonResponse(searchResponse([hit()]))));
    renderScopedDrawer(makeFakeEditor(), { book_ids: [], collection_ids: [] });

    submitQuery("grace");
    await screen.findByText("On Grace");

    const [, init] = fetchStub.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ query: "grace" });
  });

  it("rejects an empty submit client-side without fetching", () => {
    const fetchStub = installFetch(() => Promise.resolve(jsonResponse(searchResponse([]))));
    renderDrawer(makeFakeEditor());

    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    expect(screen.getByRole("alert")).toHaveTextContent("Enter a question to search for.");
    expect(fetchStub).not.toHaveBeenCalled();
  });

  it("surfaces the proxy's {error} body on a non-ok response", async () => {
    installFetch(() => Promise.resolve(jsonResponse({ error: "Search failed." }, { ok: false })));
    renderDrawer(makeFakeEditor());

    submitQuery("grace");

    expect(await screen.findByRole("alert")).toHaveTextContent("Search failed.");
    expect(screen.getByRole("button", { name: "Search" })).toBeEnabled();
  });

  it("renders an empty-result message when there are zero hits", async () => {
    installFetch(() => Promise.resolve(jsonResponse(searchResponse([]))));
    renderDrawer(makeFakeEditor());

    submitQuery("nonsense");

    expect(
      await screen.findByText("No passages found in your library for that query."),
    ).toBeInTheDocument();
  });
});

describe("LibraryDrawer — insert", () => {
  it("clicking a hit inserts a citation node mapping every attr from the raw hit", async () => {
    installFetch(() => Promise.resolve(jsonResponse(searchResponse([hit()]))));
    const editor = makeFakeEditor();
    renderDrawer(editor);

    submitQuery("grace");
    const row = await screen.findByTestId("library-drawer-hit");
    fireEvent.click(row);

    expect(editor.focused).toBe(1);
    expect(editor.inserted).toHaveLength(1);
    expect(editor.inserted[0]).toEqual({
      type: "citation",
      attrs: {
        bookId: "book-1",
        chunkIndex: 12,
        bookTitle: "On Grace",
        snippet: "Grace is the unearned favor of God.",
        parentSection: "Chapter 1",
      },
    });
  });

  it("falls back to a neutral title when the hit's book is not in the library map", async () => {
    installFetch(() =>
      Promise.resolve(jsonResponse(searchResponse([hit({ book_id: "unknown-book" })]))),
    );
    const editor = makeFakeEditor();
    renderDrawer(editor);

    submitQuery("grace");
    const row = await screen.findByTestId("library-drawer-hit");
    fireEvent.click(row);

    expect((editor.inserted[0] as InsertCall).attrs.bookTitle).toBe("Untitled book");
  });

  it("maps a null parent_section straight through to the citation attrs", async () => {
    installFetch(() =>
      Promise.resolve(
        jsonResponse(
          searchResponse([
            hit({ metadata: { filename: null, chunk_index: 3, parent_section: null } }),
          ]),
        ),
      ),
    );
    const editor = makeFakeEditor();
    renderDrawer(editor);

    submitQuery("grace");
    const row = await screen.findByTestId("library-drawer-hit");
    fireEvent.click(row);

    const attrs = (editor.inserted[0] as InsertCall).attrs;
    expect(attrs.parentSection).toBeNull();
    expect(attrs.chunkIndex).toBe(3);
  });
});

describe("LibraryDrawer — security", () => {
  it("renders a hit snippet as PLAIN TEXT (no HTML injection)", async () => {
    installFetch(() =>
      Promise.resolve(
        jsonResponse(searchResponse([hit({ content_chunk: "<img src=x onerror=alert(1)>x" })])),
      ),
    );
    const { container } = (() => {
      const editor = makeFakeEditor();
      const r = render(
        <LibraryDrawer
          editor={editor as unknown as Editor}
          bookTitles={TITLES}
          onClose={vi.fn()}
        />,
      );
      return r;
    })();

    submitQuery("grace");
    await screen.findByText("<img src=x onerror=alert(1)>x");
    expect(container.querySelector("img")).toBeNull();
  });
});

describe("LibraryDrawer — close", () => {
  it("fires onClose from the Close button", async () => {
    installFetch(() => Promise.resolve(jsonResponse(searchResponse([]))));
    const onClose = vi.fn();
    render(
      <LibraryDrawer
        editor={makeFakeEditor() as unknown as Editor}
        bookTitles={TITLES}
        onClose={onClose}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });
});
