import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Collection, LibraryBook } from "../../lib/types";
import { installFetch, jsonResponse } from "./helpers";

/**
 * CollectionsPanel (Phase 48) tests. The panel is a CLIENT island that lists the
 * user's collections and routes create / rename / delete / assign-books through
 * the same-origin /api/collections[/…] proxies, then `router.refresh()`. These
 * assert the COMPONENT'S CONTRACT — the exact proxy URL + method + whitelisted
 * body — not the network: `next/navigation`'s router is mocked, `fetch` is
 * stubbed per-test, and `window.confirm` is stubbed so delete intent gating is
 * deterministic.
 */

const refresh = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh }),
}));

// Imported AFTER the mock is registered.
import { CollectionsPanel } from "../../components/library/CollectionsPanel";

function makeCollection(overrides: Partial<Collection> = {}): Collection {
  return {
    collection_id: "col-1",
    name: "Commentaries",
    description: null,
    created_at: "2026-06-15T00:00:00Z",
    book_ids: [],
    ...overrides,
  };
}

function makeBook(overrides: Partial<LibraryBook> = {}): LibraryBook {
  return {
    book_id: "book-1",
    title: "Institutes",
    author: "Calvin",
    category: null,
    added_at: "2026-06-15T00:00:00Z",
    chunk_count: null,
    last_chunk_index: null,
    progress: null,
    ...overrides,
  };
}

/** A 204 No Content response (the delete-success shape). */
function noContent(): Response {
  return { ok: true, status: 204, json: () => Promise.resolve(null) } as Response;
}

function lastCalls(stub: ReturnType<typeof installFetch>): [string, RequestInit | undefined][] {
  return stub.mock.calls as [string, RequestInit | undefined][];
}

function bodyOf(init: RequestInit | undefined): unknown {
  return JSON.parse(String(init?.body));
}

beforeEach(() => {
  refresh.mockClear();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("CollectionsPanel — render", () => {
  it("renders the empty state when there are no collections", () => {
    installFetch(() => Promise.reject(new Error("no fetch expected")));
    render(<CollectionsPanel collections={[]} books={[]} />);
    expect(screen.getByText(/No collections yet/i)).toBeInTheDocument();
  });

  it("lists each collection with its name and book count", () => {
    installFetch(() => Promise.reject(new Error("no fetch expected")));
    render(
      <CollectionsPanel
        collections={[
          makeCollection({ book_ids: ["book-1", "book-2"] }),
          makeCollection({ collection_id: "col-2", name: "Sermons", book_ids: ["book-1"] }),
        ]}
        books={[makeBook(), makeBook({ book_id: "book-2", title: "City of God" })]}
      />,
    );
    // A row per collection (the Rename aria-label is unique; the name also
    // appears as a <select> option, so assert the row affordance + count).
    expect(screen.getByRole("button", { name: "Rename Commentaries" })).toBeInTheDocument();
    expect(screen.getByText("2 books")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Rename Sermons" })).toBeInTheDocument();
    expect(screen.getByText("1 book")).toBeInTheDocument();
  });
});

describe("CollectionsPanel — create", () => {
  it("opens the dialog and POSTs the whitelisted body to /api/collections, then refreshes", async () => {
    const stub = installFetch(() => Promise.resolve(jsonResponse(makeCollection())));
    render(<CollectionsPanel collections={[]} books={[]} />);

    fireEvent.click(screen.getByRole("button", { name: "New collection" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Patristics" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await vi.waitFor(() => expect(stub).toHaveBeenCalledTimes(1));
    const [url, init] = lastCalls(stub)[0] as [string, RequestInit];
    expect(url).toBe("/api/collections");
    expect(init.method).toBe("POST");
    expect(bodyOf(init)).toEqual({ name: "Patristics", description: null });
    await vi.waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
  });

  it("does NOT fire a request for a blank name (the local trim guard)", () => {
    const stub = installFetch(() => Promise.resolve(jsonResponse(makeCollection())));
    render(<CollectionsPanel collections={[]} books={[]} />);

    fireEvent.click(screen.getByRole("button", { name: "New collection" }));
    // Whitespace satisfies the input's native `required` so the submit fires —
    // the component's own trim guard is what must reject it (no fetch).
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(stub).not.toHaveBeenCalled();
    expect(screen.getByText(/Add a name for the collection/i)).toBeInTheDocument();
  });
});

describe("CollectionsPanel — rename", () => {
  it("PATCHes the edited name+description to /api/collections/[id]", async () => {
    const stub = installFetch(() => Promise.resolve(jsonResponse(makeCollection())));
    render(<CollectionsPanel collections={[makeCollection()]} books={[]} />);

    fireEvent.click(screen.getByRole("button", { name: "Rename Commentaries" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Bible Commentaries" } });
    fireEvent.change(screen.getByLabelText(/description/i), {
      target: { value: "verse by verse" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await vi.waitFor(() => expect(stub).toHaveBeenCalledTimes(1));
    const [url, init] = lastCalls(stub)[0] as [string, RequestInit];
    expect(url).toBe("/api/collections/col-1");
    expect(init.method).toBe("PATCH");
    expect(bodyOf(init)).toEqual({ name: "Bible Commentaries", description: "verse by verse" });
    await vi.waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
  });
});

describe("CollectionsPanel — delete (confirm gating)", () => {
  it("does NOT delete when the confirm is cancelled", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const stub = installFetch(() => Promise.resolve(noContent()));
    render(<CollectionsPanel collections={[makeCollection()]} books={[]} />);

    fireEvent.click(screen.getByRole("button", { name: "Delete Commentaries" }));
    await Promise.resolve();

    expect(stub).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
  });

  it("DELETEs /api/collections/[id] on confirm, then refreshes", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const stub = installFetch(() => Promise.resolve(noContent()));
    render(<CollectionsPanel collections={[makeCollection()]} books={[]} />);

    fireEvent.click(screen.getByRole("button", { name: "Delete Commentaries" }));
    await vi.waitFor(() => expect(stub).toHaveBeenCalledTimes(1));

    const [url, init] = lastCalls(stub)[0] as [string, RequestInit];
    expect(url).toBe("/api/collections/col-1");
    expect(init.method).toBe("DELETE");
    await vi.waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
  });
});

describe("CollectionsPanel — assign books", () => {
  it("POSTs the ticked book_ids to /api/collections/[id]/books and clears the selection", async () => {
    const stub = installFetch(() => Promise.resolve(jsonResponse(makeCollection())));
    render(
      <CollectionsPanel
        collections={[makeCollection()]}
        books={[makeBook(), makeBook({ book_id: "book-2", title: "City of God" })]}
      />,
    );

    fireEvent.click(screen.getByLabelText("Institutes"));
    fireEvent.change(screen.getByLabelText("Add selected books to collection"), {
      target: { value: "col-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: /add .*to collection/i }));

    await vi.waitFor(() => expect(stub).toHaveBeenCalledTimes(1));
    const [url, init] = lastCalls(stub)[0] as [string, RequestInit];
    expect(url).toBe("/api/collections/col-1/books");
    expect(init.method).toBe("POST");
    expect(bodyOf(init)).toEqual({ book_ids: ["book-1"] });
    await vi.waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
  });

  it("the whole-collection checkbox selects every member book so they can be re-assigned", async () => {
    const stub = installFetch(() => Promise.resolve(jsonResponse(makeCollection())));
    render(
      <CollectionsPanel
        collections={[
          makeCollection({ book_ids: ["book-1", "book-2"] }),
          makeCollection({ collection_id: "col-2", name: "Sermons", book_ids: [] }),
        ]}
        books={[makeBook(), makeBook({ book_id: "book-2", title: "City of God" })]}
      />,
    );

    // Tick the whole "Commentaries" collection → both member books get selected.
    fireEvent.click(screen.getByLabelText("Select all books in Commentaries"));
    expect((screen.getByLabelText("Institutes") as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText("City of God") as HTMLInputElement).checked).toBe(true);

    // Assign the now-selected pair into the empty "Sermons" collection.
    fireEvent.change(screen.getByLabelText("Add selected books to collection"), {
      target: { value: "col-2" },
    });
    fireEvent.click(screen.getByRole("button", { name: /add 2 to collection/i }));

    await vi.waitFor(() => expect(stub).toHaveBeenCalledTimes(1));
    const [url, init] = lastCalls(stub)[0] as [string, RequestInit];
    expect(url).toBe("/api/collections/col-2/books");
    expect(bodyOf(init)).toEqual({ book_ids: ["book-1", "book-2"] });
  });
});
