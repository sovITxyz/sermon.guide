import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SearchHistoryEntry, SearchHistoryItem } from "../../lib/types";
import { installFetch, jsonResponse } from "./helpers";

/**
 * Phase 51 — the "Recent" search-history panel. Asserts the island's contract:
 * clicking a row REOPENS the saved result via GET /api/search-history/{id} and
 * hands it to `onOpen` (the SearchWorkspace integration test then proves it
 * rehydrates SearchPanel's render) — crucially WITHOUT a second
 * /api/search-summary call (the whole point of saving the full result). A
 * per-row delete hits DELETE /api/search-history/{id} then router.refresh().
 * `next/navigation`'s router is mocked (refresh re-runs the server component).
 */

const refresh = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh }),
}));

// Imported AFTER the mock is registered.
import { RecentSearches } from "../../components/search/RecentSearches";
import { SearchWorkspace } from "../../components/search/SearchWorkspace";

function makeItem(overrides: Partial<SearchHistoryItem> = {}): SearchHistoryItem {
  return {
    history_id: "h1",
    query: "How do grace and faith relate?",
    scope_book_ids: [],
    scope_collection_ids: [],
    summary_preview: "Grace is given freely, and faith receives it.",
    created_at: "2026-06-29T13:45:07Z",
    ...overrides,
  };
}

function makeEntry(overrides: Partial<SearchHistoryEntry> = {}): SearchHistoryEntry {
  return {
    history_id: "h1",
    query: "How do grace and faith relate?",
    scope_book_ids: [],
    scope_collection_ids: [],
    result: {
      summary: "Grace is central [Romans:7].",
      citations: [
        {
          marker: "[Romans:7]",
          book_id: "b1",
          title: "Romans Commentary",
          chunk_index: 7,
          content: "passage text",
          filename: null,
          parent_section: null,
        },
      ],
    },
    created_at: "2026-06-29T13:45:07Z",
    ...overrides,
  };
}

/** A 204 No Content response (the delete success shape). */
function noContent(): Response {
  return { ok: true, status: 204, json: () => Promise.resolve(null) } as Response;
}

function calls(stub: ReturnType<typeof installFetch>): [string, RequestInit | undefined][] {
  return stub.mock.calls as [string, RequestInit | undefined][];
}

beforeEach(() => {
  refresh.mockClear();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("RecentSearches — render", () => {
  it("renders the empty state with no rows", () => {
    installFetch(() => Promise.reject(new Error("no fetch expected")));
    render(<RecentSearches items={[]} onOpen={vi.fn()} />);
    expect(screen.getByText(/No recent searches yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Delete recent search/ })).not.toBeInTheDocument();
  });

  it("renders a row per saved search with its query, preview, and a delete action", () => {
    installFetch(() => Promise.reject(new Error("no fetch expected")));
    render(
      <RecentSearches
        items={[
          makeItem(),
          makeItem({
            history_id: "h2",
            query: "What is hope?",
            summary_preview: "Hope anchors the soul.",
          }),
        ]}
        onOpen={vi.fn()}
      />,
    );
    expect(
      screen.getByRole("button", { name: "Delete recent search: How do grace and faith relate?" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Delete recent search: What is hope?" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Grace is given freely, and faith receives it.")).toBeInTheDocument();
    expect(screen.getByText("Hope anchors the soul.")).toBeInTheDocument();
  });

  it("shows a scope badge only for a scoped search", () => {
    installFetch(() => Promise.reject(new Error("no fetch expected")));
    render(
      <RecentSearches
        items={[
          makeItem({ history_id: "scoped", scope_book_ids: ["b1", "b2"] }),
          makeItem({ history_id: "whole" }),
        ]}
        onOpen={vi.fn()}
      />,
    );
    expect(screen.getByText("Scoped to 2 items")).toBeInTheDocument();
  });
});

describe("RecentSearches — reopen", () => {
  it("fetches the full entry and calls onOpen WITHOUT a /search-summary call", async () => {
    const onOpen = vi.fn();
    const stub = installFetch(() => Promise.resolve(jsonResponse(makeEntry())));
    render(<RecentSearches items={[makeItem()]} onOpen={onOpen} />);

    fireEvent.click(screen.getByRole("button", { name: /^How do grace and faith relate\?/ }));

    await waitFor(() => expect(onOpen).toHaveBeenCalledTimes(1));
    expect(onOpen).toHaveBeenCalledWith(makeEntry());

    const urls = calls(stub).map(([url]) => url);
    expect(urls).toContain("/api/search-history/h1");
    // The replay must NEVER re-run the costly summary pipeline.
    expect(urls.some((u) => u.includes("/api/search-summary"))).toBe(false);
  });

  it("encodeURIComponent's the history id in the GET url", async () => {
    const stub = installFetch(() => Promise.resolve(jsonResponse(makeEntry())));
    render(
      <RecentSearches
        items={[makeItem({ history_id: "a/b c", query: "Weird id" })]}
        onOpen={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /^Weird id/ }));
    await waitFor(() => expect(stub).toHaveBeenCalled());
    expect(calls(stub)[0]?.[0]).toBe("/api/search-history/a%2Fb%20c");
  });

  it("surfaces an error when the entry fetch fails", async () => {
    installFetch(() =>
      Promise.resolve(jsonResponse({ error: "boom" }, { ok: false, status: 500 })),
    );
    render(<RecentSearches items={[makeItem()]} onOpen={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: /^How do grace and faith relate\?/ }));
    expect(await screen.findByText(/Could not open that search/i)).toBeInTheDocument();
  });
});

describe("RecentSearches — delete", () => {
  it("deletes via DELETE /api/search-history/[id] and refreshes", async () => {
    const stub = installFetch(() => Promise.resolve(noContent()));
    render(<RecentSearches items={[makeItem()]} onOpen={vi.fn()} />);

    fireEvent.click(
      screen.getByRole("button", { name: "Delete recent search: How do grace and faith relate?" }),
    );
    await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));

    const deleteCall = calls(stub).find(
      ([url, init]) => url === "/api/search-history/h1" && init?.method === "DELETE",
    );
    expect(deleteCall).toBeDefined();
  });

  it("refreshes on a 404 (already gone) too", async () => {
    installFetch(() =>
      Promise.resolve(jsonResponse({ detail: "gone" }, { ok: false, status: 404 })),
    );
    render(<RecentSearches items={[makeItem()]} onOpen={vi.fn()} />);

    fireEvent.click(
      screen.getByRole("button", { name: "Delete recent search: How do grace and faith relate?" }),
    );
    await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
  });

  it("surfaces an error and does NOT refresh on a real delete failure", async () => {
    installFetch(() =>
      Promise.resolve(jsonResponse({ detail: "boom" }, { ok: false, status: 500 })),
    );
    render(<RecentSearches items={[makeItem()]} onOpen={vi.fn()} />);

    fireEvent.click(
      screen.getByRole("button", { name: "Delete recent search: How do grace and faith relate?" }),
    );
    await waitFor(() =>
      expect(screen.getByText(/Could not delete that search/i)).toBeInTheDocument(),
    );
    expect(refresh).not.toHaveBeenCalled();
  });
});

describe("SearchWorkspace — reopen hydrates the SearchPanel render", () => {
  it("clicking a recent row renders the saved summary with NO /search-summary call", async () => {
    const stub = installFetch((input) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.startsWith("/api/search-history/")) {
        return Promise.resolve(jsonResponse(makeEntry()));
      }
      return Promise.reject(new Error(`unexpected fetch: ${url}`));
    });

    render(<SearchWorkspace totalBooks={2} history={[makeItem()]} />);

    // No summary is rendered until a recent entry is reopened.
    expect(screen.queryByRole("heading", { name: "Summary" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /^How do grace and faith relate\?/ }));

    // The saved summary + its resolving citation chip render from the replayed
    // result — the same render path a live search uses.
    await screen.findByRole("heading", { name: "Summary" });
    expect(screen.getByRole("heading", { name: "Sources" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "[1]" })).toHaveAttribute("href", "#citation-1");

    // Reopen must be free: only the history GET fired, never /search-summary.
    const urls = calls(stub).map(([url]) => url);
    expect(urls.some((u) => u.includes("/api/search-history/"))).toBe(true);
    expect(urls.some((u) => u.includes("/api/search-summary"))).toBe(false);
  });
});
