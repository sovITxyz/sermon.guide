import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Collection, LibraryBook } from "../../lib/types";
import { installFetch, jsonResponse } from "./helpers";

/**
 * LibraryBrowser (Phase 49) tests. The island lifts the per-book checkbox column,
 * the SelectionBar, and the CollectionsPanel under one shared SelectionProvider.
 * These assert the SELECTION CONTRACT: ticking a book updates the shared
 * selection (so the SelectionBar count + the "Search these" link appear), Clear
 * empties it, and "Add to collection" POSTs the resolved set to the chosen
 * collection's /books proxy.
 *
 * next/navigation is mocked (SelectionBar + CollectionsPanel call useRouter); the
 * sessionStorage persistence is cleared between tests so selections don't leak.
 */

const refresh = vi.fn();
const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh, push }),
}));

// Imported AFTER the mock is registered.
import { LibraryBrowser } from "../../components/library/LibraryBrowser";
import { SelectionProvider } from "../../components/library/selection-context";

function makeBook(
  overrides: Partial<LibraryBook> & { book_id: string; title: string },
): LibraryBook {
  return {
    author: null,
    category: null,
    added_at: "2026-06-15T00:00:00Z",
    chunk_count: null,
    last_chunk_index: null,
    progress: null,
    ...overrides,
  };
}

function collection(overrides: Partial<Collection> & { collection_id: string }): Collection {
  return {
    name: "Commentaries",
    description: null,
    created_at: "2026-06-15T00:00:00Z",
    book_ids: [],
    ...overrides,
  };
}

const BOOKS: LibraryBook[] = [
  makeBook({ book_id: "b1", title: "Institutes" }),
  makeBook({ book_id: "b2", title: "Confessions" }),
];

function renderBrowser(collections: Collection[] = []) {
  render(
    <SelectionProvider collections={collections}>
      <LibraryBrowser books={BOOKS} collections={collections} />
    </SelectionProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  refresh.mockReset();
  push.mockReset();
});

describe("LibraryBrowser — selection", () => {
  it("shows the whole-library hint until a book is ticked", () => {
    renderBrowser();
    expect(screen.getByTestId("selection-summary")).toHaveTextContent(
      "Searching all 2 books in your library.",
    );
  });

  it("ticking a book updates the shared selection count and reveals Search these", async () => {
    renderBrowser();

    fireEvent.click(screen.getByLabelText("Select Institutes"));

    await waitFor(() =>
      expect(screen.getByTestId("selection-summary")).toHaveTextContent("1 book selected"),
    );
    // The checkbox reflects the shared selection.
    expect(screen.getByLabelText("Select Institutes")).toBeChecked();
    expect(screen.getByLabelText("Select Confessions")).not.toBeChecked();
    // "Search these" links to /search (the selection rides over via sessionStorage).
    expect(screen.getByRole("link", { name: "Search these" })).toHaveAttribute("href", "/search");
  });

  it("Clear empties the selection back to the whole-library hint", async () => {
    renderBrowser();

    fireEvent.click(screen.getByLabelText("Select Institutes"));
    fireEvent.click(screen.getByLabelText("Select Confessions"));
    await waitFor(() =>
      expect(screen.getByTestId("selection-summary")).toHaveTextContent("2 books selected"),
    );

    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    await waitFor(() =>
      expect(screen.getByTestId("selection-summary")).toHaveTextContent(
        "Searching all 2 books in your library.",
      ),
    );
  });

  it("Add to collection POSTs the resolved set to the chosen collection's /books proxy", async () => {
    const fetchStub = installFetch(() =>
      Promise.resolve(jsonResponse(null, { ok: true, status: 200 })),
    );
    renderBrowser([collection({ collection_id: "c1", name: "Commentaries" })]);

    fireEvent.click(screen.getByLabelText("Select Institutes"));
    await waitFor(() =>
      expect(screen.getByTestId("selection-summary")).toHaveTextContent("1 book selected"),
    );

    // The CollectionsPanel also exposes an "Add to collection" control with the
    // same label, so scope to the SelectionBar region.
    const bar = within(screen.getByTestId("selection-bar"));
    fireEvent.change(bar.getByLabelText("Add selected books to collection"), {
      target: { value: "c1" },
    });
    fireEvent.click(bar.getByRole("button", { name: "Add to collection" }));

    await waitFor(() => expect(fetchStub).toHaveBeenCalled());
    const [url, init] = fetchStub.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/collections/c1/books");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ book_ids: ["b1"] });
    expect(refresh).toHaveBeenCalled();
  });
});
