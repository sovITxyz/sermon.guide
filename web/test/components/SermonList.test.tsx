import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DocumentFull, DocumentListItem } from "../../lib/types";
import { installFetch, jsonResponse } from "./helpers";

/**
 * SermonList (Phase 36, B2 slice C) tests — the delete/restore row actions.
 * Asserts the COMPONENT'S CONTRACT: confirm-gated soft delete hits
 * DELETE /api/documents/[id], a successful delete raises the undo toast and
 * refreshes, the undo toast restores via POST /api/documents/[id]/restore, and
 * cancelling the confirm fires no request. `next/navigation`'s router is mocked
 * (refresh is the server-component re-run that reflects the mutated list);
 * `window.confirm` is stubbed per-test so intent gating is deterministic.
 */

const refresh = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh }),
}));

// Imported AFTER the mock is registered.
import { SermonList } from "../../components/SermonList";

function makeItem(overrides: Partial<DocumentListItem> = {}): DocumentListItem {
  return {
    document_id: "doc-1",
    title: "My sermon",
    preview: "An opening line.",
    schema_version: 1,
    created_at: "2026-06-15T00:00:00Z",
    updated_at: "2026-06-15T10:00:00Z",
    ...overrides,
  };
}

function makeFullDoc(overrides: Partial<DocumentFull> = {}): DocumentFull {
  return {
    document_id: "doc-1",
    title: "My sermon",
    content: { type: "doc", content: [{ type: "paragraph" }] },
    content_text: "An opening line.",
    schema_version: 1,
    created_at: "2026-06-15T00:00:00Z",
    updated_at: "2026-06-15T10:00:00Z",
    ...overrides,
  };
}

/** A 204 No Content response (the soft-delete success shape). */
function noContent(): Response {
  return { ok: true, status: 204, json: () => Promise.resolve(null) } as Response;
}

function lastCalls(stub: ReturnType<typeof installFetch>): [string, RequestInit | undefined][] {
  return stub.mock.calls as [string, RequestInit | undefined][];
}

beforeEach(() => {
  refresh.mockClear();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("SermonList — empty + render", () => {
  it("renders the empty state with no rows", () => {
    installFetch(() => Promise.reject(new Error("no fetch expected")));
    render(<SermonList documents={[]} />);
    expect(screen.getByText(/no sermons yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Delete/ })).not.toBeInTheDocument();
  });

  it("renders a row per sermon with a delete action and editor link", () => {
    installFetch(() => Promise.reject(new Error("no fetch expected")));
    render(
      <SermonList documents={[makeItem(), makeItem({ document_id: "doc-2", title: "Second" })]} />,
    );
    expect(screen.getByRole("link", { name: /My sermon/ })).toHaveAttribute(
      "href",
      "/sermons/doc-1",
    );
    expect(screen.getByRole("button", { name: "Delete My sermon" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete Second" })).toBeInTheDocument();
  });
});

describe("SermonList — delete (confirm gating)", () => {
  it("does NOT delete when the confirm is cancelled", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const stub = installFetch(() => Promise.resolve(noContent()));
    render(<SermonList documents={[makeItem()]} />);

    fireEvent.click(screen.getByRole("button", { name: "Delete My sermon" }));
    await Promise.resolve();

    expect(stub).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
  });

  it("soft-deletes on confirm, hitting DELETE /api/documents/[id], then refreshes + shows undo", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const stub = installFetch(() => Promise.resolve(noContent()));
    render(<SermonList documents={[makeItem()]} />);

    fireEvent.click(screen.getByRole("button", { name: "Delete My sermon" }));
    // Wait for the post-delete state flush (the undo toast) rather than the
    // fetch call alone — the setUndo + router.refresh land after the await.
    const undoBtn = await screen.findByRole("button", { name: "Undo" });

    const [url, init] = lastCalls(stub)[0] as [string, RequestInit];
    expect(url).toBe("/api/documents/doc-1");
    expect(init.method).toBe("DELETE");
    expect(refresh).toHaveBeenCalledTimes(1);

    // The undo toast appears labelling the deleted sermon; the title is
    // interpolated so the copy spans text nodes — reach the <output> wrapper via
    // the Undo button and assert its full textContent.
    const toast = undoBtn.closest("output");
    expect(toast).not.toBeNull();
    expect(toast?.textContent).toContain("Deleted");
    expect(toast?.textContent).toContain("My sermon");
  });

  it("encodeURIComponent's the document id in the DELETE url", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const stub = installFetch(() => Promise.resolve(noContent()));
    render(<SermonList documents={[makeItem({ document_id: "a/b c", title: "Weird" })]} />);

    fireEvent.click(screen.getByRole("button", { name: "Delete Weird" }));
    await vi.waitFor(() => expect(stub).toHaveBeenCalledTimes(1));

    const [url] = lastCalls(stub)[0] as [string, RequestInit];
    expect(url).toBe("/api/documents/a%2Fb%20c");
  });

  it("surfaces an error and shows NO undo when the delete fails", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    installFetch(() =>
      Promise.resolve(jsonResponse({ detail: "boom" }, { ok: false, status: 500 })),
    );
    render(<SermonList documents={[makeItem()]} />);

    fireEvent.click(screen.getByRole("button", { name: "Delete My sermon" }));
    await vi.waitFor(() =>
      expect(screen.getByText(/Could not delete the sermon/i)).toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: "Undo" })).not.toBeInTheDocument();
    expect(refresh).not.toHaveBeenCalled();
  });
});

describe("SermonList — restore via undo", () => {
  it("restores via POST /api/documents/[id]/restore, drops the toast, and refreshes", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const stub = installFetch((_input, init?: RequestInit) =>
      init?.method === "DELETE"
        ? Promise.resolve(noContent())
        : Promise.resolve(jsonResponse(makeFullDoc())),
    );
    render(<SermonList documents={[makeItem()]} />);

    // Delete -> undo toast.
    fireEvent.click(screen.getByRole("button", { name: "Delete My sermon" }));
    await vi.waitFor(() =>
      expect(screen.getByRole("button", { name: "Undo" })).toBeInTheDocument(),
    );
    refresh.mockClear();

    // Undo -> restore.
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await vi.waitFor(() =>
      expect(screen.queryByRole("button", { name: "Undo" })).not.toBeInTheDocument(),
    );

    const restoreCall = lastCalls(stub).find(
      ([url, init]) => url === "/api/documents/doc-1/restore" && init?.method === "POST",
    );
    expect(restoreCall).toBeDefined();
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("dismissing the toast removes it without any fetch", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const stub = installFetch((_input, init?: RequestInit) =>
      init?.method === "DELETE"
        ? Promise.resolve(noContent())
        : Promise.resolve(jsonResponse(makeFullDoc())),
    );
    render(<SermonList documents={[makeItem()]} />);

    fireEvent.click(screen.getByRole("button", { name: "Delete My sermon" }));
    await vi.waitFor(() =>
      expect(screen.getByRole("button", { name: "Undo" })).toBeInTheDocument(),
    );
    const callsAfterDelete = stub.mock.calls.length;

    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("button", { name: "Undo" })).not.toBeInTheDocument();
    // No restore fired by a dismiss.
    expect(stub.mock.calls.length).toBe(callsAfterDelete);
  });

  it("keeps the undo toast (no double-clobber) when the restore fails", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    installFetch((_input, init?: RequestInit) =>
      init?.method === "DELETE"
        ? Promise.resolve(noContent())
        : Promise.resolve(jsonResponse({ detail: "nope" }, { ok: false, status: 500 })),
    );
    render(<SermonList documents={[makeItem()]} />);

    fireEvent.click(screen.getByRole("button", { name: "Delete My sermon" }));
    await vi.waitFor(() =>
      expect(screen.getByRole("button", { name: "Undo" })).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await vi.waitFor(() =>
      expect(screen.getByText(/Could not restore the sermon/i)).toBeInTheDocument(),
    );
    // The toast stays so the user can retry the undo.
    expect(screen.getByRole("button", { name: "Undo" })).toBeInTheDocument();
  });
});
