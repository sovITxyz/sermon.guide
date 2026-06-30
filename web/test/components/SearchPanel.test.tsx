import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SearchPanel } from "../../components/SearchPanel";
import {
  SELECTION_STORAGE_KEY,
  SelectionProvider,
} from "../../components/library/selection-context";
import type { Collection, SummaryCitation, SummaryResponse } from "../../lib/types";
import { installFetch, jsonResponse } from "./helpers";

function citation(overrides: Partial<SummaryCitation> & { marker: string }): SummaryCitation {
  return {
    book_id: "b1",
    title: "A Book",
    chunk_index: 0,
    content: "passage text",
    filename: null,
    parent_section: null,
    ...overrides,
  };
}

function collection(overrides: Partial<Collection> & { collection_id: string }): Collection {
  return {
    name: "A collection",
    description: null,
    created_at: "2026-06-15T00:00:00Z",
    book_ids: [],
    ...overrides,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  sessionStorage.clear();
});

function submitQuery(value: string): void {
  fireEvent.change(screen.getByLabelText("Question"), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
}

describe("SearchPanel", () => {
  it("rejects an empty submit client-side without fetching (error state)", () => {
    const fetchStub = installFetch(() => Promise.resolve(jsonResponse({})));
    render(<SearchPanel />);

    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    expect(screen.getByRole("alert")).toHaveTextContent("Enter a question to search for.");
    expect(fetchStub).not.toHaveBeenCalled();
  });

  it("surfaces the proxy's {error} body on a non-ok response", async () => {
    installFetch(() =>
      Promise.resolve(jsonResponse({ error: "Upstream is down." }, { ok: false })),
    );
    render(<SearchPanel />);

    submitQuery("grace");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Upstream is down.");
    // The error state clears the in-flight affordance.
    expect(screen.getByRole("button", { name: "Search" })).toBeEnabled();
  });

  it("shows 'Network error' when the fetch throws", async () => {
    installFetch(() => Promise.reject(new Error("offline")));
    render(<SearchPanel />);

    submitQuery("grace");

    expect(await screen.findByRole("alert")).toHaveTextContent("Network error. Please try again.");
  });

  it("renders the loading affordance and advances the m:ss ticker (fake timers)", async () => {
    vi.useFakeTimers();
    // A fetch that never resolves keeps the component in the searching state so
    // the interval ticker can be observed.
    installFetch(() => new Promise<Response>(() => {}));
    render(<SearchPanel />);

    fireEvent.change(screen.getByLabelText("Question"), { target: { value: "grace" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    // Button flips and the input is disabled the instant searching begins.
    const button = screen.getByRole("button", { name: "Searching…" });
    expect(button).toBeDisabled();
    expect(screen.getByLabelText("Question")).toBeDisabled();

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Searching your library… 0:00");

    // The interval ticks every 1000ms; advancing 65s reaches 1:05.
    await vi.advanceTimersByTimeAsync(65_000);
    expect(status).toHaveTextContent("Searching your library… 1:05");
  });

  it("renders the no-context message verbatim when there are zero citations", async () => {
    const summary: SummaryResponse = {
      summary: "I could not find anything about that in your library.",
      citations: [],
    };
    installFetch(() => Promise.resolve(jsonResponse(summary)));
    render(<SearchPanel />);

    submitQuery("nonsense");

    expect(
      await screen.findByText("I could not find anything about that in your library."),
    ).toBeInTheDocument();
    // No Summary/Sources structure in the empty state.
    expect(screen.queryByRole("heading", { name: "Sources" })).not.toBeInTheDocument();
  });

  it("renders a grounded summary with a resolving citation chip linked to its source", async () => {
    const summary: SummaryResponse = {
      summary: "Grace is central [Romans:7]. Indeed.",
      citations: [citation({ marker: "[Romans:7]", title: "Romans Commentary", chunk_index: 7 })],
    };
    installFetch(() => Promise.resolve(jsonResponse(summary)));
    render(<SearchPanel />);

    submitQuery("grace");

    await screen.findByRole("heading", { name: "Summary" });

    // The chip renders visible label [1] and links to the source <li> anchor.
    const chip = screen.getByRole("link", { name: "[1]" });
    expect(chip).toHaveAttribute("href", "#citation-1");
    expect(chip).toHaveAttribute("title", "[Romans:7]");

    // The source list item carries the matching anchor id.
    const sources = screen.getByRole("list");
    const items = within(sources).getAllByRole("listitem");
    expect(items).toHaveLength(1);
    expect(items[0]).toHaveAttribute("id", "citation-1");
    expect(items[0]).toHaveTextContent("Romans Commentary");
  });

  it("explodes a Phase 24 merged bracket into two distinct resolving chips", async () => {
    // [Faith:70, Faith:51] must render as TWO chips ([1] and [2]) linking to
    // #citation-1 and #citation-2 — the structural `[`, `]`, `, ` are dropped.
    const summary: SummaryResponse = {
      summary: "Faith grows [Faith:70, Faith:51] over time.",
      citations: [
        citation({ marker: "[Faith:70]", chunk_index: 70 }),
        citation({ marker: "[Faith:51]", chunk_index: 51 }),
      ],
    };
    installFetch(() => Promise.resolve(jsonResponse(summary)));
    render(<SearchPanel />);

    submitQuery("faith");

    await screen.findByRole("heading", { name: "Summary" });

    const chipOne = screen.getByRole("link", { name: "[1]" });
    const chipTwo = screen.getByRole("link", { name: "[2]" });
    expect(chipOne).toHaveAttribute("href", "#citation-1");
    expect(chipOne).toHaveAttribute("title", "[Faith:70]");
    expect(chipTwo).toHaveAttribute("href", "#citation-2");
    expect(chipTwo).toHaveAttribute("title", "[Faith:51]");

    // The merged-bracket structural glue must not survive into rendered text.
    const summaryHeading = screen.getByRole("heading", { name: "Summary" });
    const summaryCard = summaryHeading.parentElement as HTMLElement;
    expect(summaryCard).toHaveTextContent("Faith grows [1][2] over time.");
    expect(summaryCard.textContent).not.toContain("Faith:70");
    expect(summaryCard.textContent).not.toContain(", ");
  });

  it("drops an unresolvable merged member, keeping only the real chip", async () => {
    // [A:1, Ghost:9] — Ghost:9 has no citation, so only [1] is rendered.
    const summary: SummaryResponse = {
      summary: "Both views [A:1, Ghost:9] agree.",
      citations: [citation({ marker: "[A:1]", chunk_index: 1 })],
    };
    installFetch(() => Promise.resolve(jsonResponse(summary)));
    render(<SearchPanel />);

    submitQuery("views");

    await screen.findByRole("heading", { name: "Summary" });
    expect(screen.getByRole("link", { name: "[1]" })).toHaveAttribute("href", "#citation-1");
    expect(screen.queryByRole("link", { name: "[2]" })).not.toBeInTheDocument();
  });

  it("omits the scope and shows the whole-library label with no selection", async () => {
    const fetchStub = installFetch(() =>
      Promise.resolve(jsonResponse({ summary: "ok", citations: [] } satisfies SummaryResponse)),
    );
    render(<SearchPanel totalBooks={7} />);

    expect(screen.getByTestId("search-scope")).toHaveTextContent(
      "Searching all 7 books in your library.",
    );

    submitQuery("grace");
    await waitFor(() => expect(fetchStub).toHaveBeenCalled());
    const [, init] = fetchStub.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ query: "grace" });
  });

  it("folds the shared selection into the POST scope and shows the resolved count", async () => {
    // Seed the selection the way navigating from /library would (sessionStorage);
    // the provider hydrates it after mount. c1 contributes b9, so the resolved
    // distinct count is 3 (b1, b2, b9) but the POST carries the RAW selection.
    sessionStorage.setItem(
      SELECTION_STORAGE_KEY,
      JSON.stringify({ bookIds: ["b1", "b2"], collectionIds: ["c1"] }),
    );
    const fetchStub = installFetch(() =>
      Promise.resolve(jsonResponse({ summary: "ok", citations: [] } satisfies SummaryResponse)),
    );
    render(
      <SelectionProvider collections={[collection({ collection_id: "c1", book_ids: ["b9"] })]}>
        <SearchPanel totalBooks={7} />
      </SelectionProvider>,
    );

    // The hydration effect resolves the union -> "3 selected books".
    await waitFor(() =>
      expect(screen.getByTestId("search-scope")).toHaveTextContent("Searching 3 selected books."),
    );

    submitQuery("grace");
    await waitFor(() => expect(fetchStub).toHaveBeenCalled());
    const [, init] = fetchStub.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      query: "grace",
      book_ids: ["b1", "b2"],
      collection_ids: ["c1"],
    });
  });

  it("re-enables the form after a successful search so a second query can run", async () => {
    const summary: SummaryResponse = {
      summary: "Grace [Romans:7].",
      citations: [citation({ marker: "[Romans:7]", chunk_index: 7 })],
    };
    installFetch(() => Promise.resolve(jsonResponse(summary)));
    render(<SearchPanel />);

    submitQuery("grace");
    await screen.findByRole("heading", { name: "Summary" });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Search" })).toBeEnabled();
    });
    expect(screen.getByLabelText("Question")).toBeEnabled();
  });
});
