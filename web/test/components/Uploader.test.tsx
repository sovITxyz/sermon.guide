import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Uploader } from "../../components/Uploader";
import type { TaskStatus, UploadAccepted } from "../../lib/types";
import { jsonResponse } from "./helpers";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function chooseFile(name = "book.epub"): void {
  const input = document.getElementById("file-input") as HTMLInputElement;
  const file = new File([new Uint8Array([1, 2, 3])], name, { type: "application/epub+zip" });
  fireEvent.change(input, { target: { files: [file] } });
}

/**
 * Route a fetch by URL: /api/upload → accept handler, /api/tasks/* → status
 * handler. Both are typed `Response` factories so Biome stays happy.
 */
function routedFetch(handlers: {
  upload: () => Promise<Response>;
  task: (taskId: string) => Promise<Response>;
}): ReturnType<typeof vi.fn<typeof fetch>> {
  const stub = vi.fn<typeof fetch>((input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url === "/api/upload") {
      return handlers.upload();
    }
    const m = url.match(/^\/api\/tasks\/(.+)$/);
    if (m?.[1]) {
      return handlers.task(decodeURIComponent(m[1]));
    }
    return Promise.reject(new Error(`unexpected fetch: ${url}`));
  });
  vi.stubGlobal("fetch", stub);
  return stub;
}

function accepted(taskId: string): UploadAccepted {
  return { task_id: taskId, upload_id: "up1", filename: "book.epub" };
}

function status(taskId: string, s: string, wasDuplicate = false): TaskStatus {
  return {
    task_id: taskId,
    status: s,
    result:
      s === "SUCCESS" ? { book_id: "b1", was_duplicate: wasDuplicate, rows_inserted: 9 } : null,
  };
}

describe("Uploader", () => {
  it("optimistically shows a 'Queued…' row the instant a file is chosen", async () => {
    routedFetch({
      upload: () => new Promise<Response>(() => {}), // never resolves
      task: () => new Promise<Response>(() => {}),
    });
    render(<Uploader />);

    chooseFile("grace.epub");

    expect(await screen.findByText("grace.epub")).toBeInTheDocument();
    expect(screen.getByText("Queued…")).toBeInTheDocument();
  });

  it("polls to SUCCESS and lands on 'Added to library' (fake timers, 2s poll)", async () => {
    vi.useFakeTimers();
    routedFetch({
      upload: () => Promise.resolve(jsonResponse(accepted("task-1"))),
      task: () => Promise.resolve(jsonResponse(status("task-1", "SUCCESS"))),
    });
    render(<Uploader />);

    chooseFile();

    // Let the upload POST microtasks settle, then advance past the 2s poll.
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(screen.getByText("Queued…")).toBeInTheDocument();

    await act(() => vi.advanceTimersByTimeAsync(2000));
    expect(screen.getByText("Added to library")).toBeInTheDocument();
  });

  it("surfaces the deduplicated label when the task reports was_duplicate", async () => {
    vi.useFakeTimers();
    routedFetch({
      upload: () => Promise.resolve(jsonResponse(accepted("task-dup"))),
      task: () => Promise.resolve(jsonResponse(status("task-dup", "SUCCESS", true))),
    });
    render(<Uploader />);

    chooseFile();
    await act(() => vi.advanceTimersByTimeAsync(2000));

    expect(screen.getByText("Already in your library (deduplicated)")).toBeInTheDocument();
  });

  it("shows the proxy's {error} when /api/upload is not ok", async () => {
    routedFetch({
      upload: () => Promise.resolve(jsonResponse({ error: "Only EPUB or PDF." }, { ok: false })),
      task: () => Promise.reject(new Error("should not poll")),
    });
    render(<Uploader />);

    chooseFile();

    expect(await screen.findByText("Only EPUB or PDF.")).toBeInTheDocument();
  });

  it("reports the lost-track message when a poll returns a 404 (Phase 20 ownership)", async () => {
    vi.useFakeTimers();
    routedFetch({
      upload: () => Promise.resolve(jsonResponse(accepted("task-404"))),
      task: () => Promise.resolve(jsonResponse({ error: "not found" }, { ok: false, status: 404 })),
    });
    render(<Uploader />);

    chooseFile();
    await act(() => vi.advanceTimersByTimeAsync(2000));

    expect(screen.getByText("Lost track of this upload.")).toBeInTheDocument();
  });

  it("stops polling after unmount (mounted-guard ref)", async () => {
    vi.useFakeTimers();
    const stub = routedFetch({
      upload: () => Promise.resolve(jsonResponse(accepted("task-x"))),
      // Keep it non-terminal so the poll would otherwise reschedule forever.
      task: () => Promise.resolve(jsonResponse(status("task-x", "STARTED"))),
    });
    const { unmount } = render(<Uploader />);

    chooseFile();
    await act(() => vi.advanceTimersByTimeAsync(2000)); // first poll fires (STARTED)
    expect(screen.getByText("Ingesting…")).toBeInTheDocument();

    const callsBeforeUnmount = stub.mock.calls.length;
    unmount();
    await act(() => vi.advanceTimersByTimeAsync(10_000)); // would be 5 more polls if still mounted

    expect(stub.mock.calls.length).toBe(callsBeforeUnmount);
  });
});
