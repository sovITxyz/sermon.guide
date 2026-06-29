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

function makeFile(name: string): File {
  return new File([new Uint8Array([1, 2, 3])], name, { type: "application/epub+zip" });
}

function chooseFile(name = "book.epub"): void {
  const input = document.getElementById("file-input") as HTMLInputElement;
  fireEvent.change(input, { target: { files: [makeFile(name)] } });
}

function chooseFiles(names: string[]): void {
  const input = document.getElementById("file-input") as HTMLInputElement;
  fireEvent.change(input, { target: { files: names.map(makeFile) } });
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

  it("multi-select shows one row per file, in selection order", async () => {
    routedFetch({
      upload: () => new Promise<Response>(() => {}), // never resolves
      task: () => new Promise<Response>(() => {}),
    });
    render(<Uploader />);

    chooseFiles(["genesis.epub", "exodus.epub", "leviticus.epub"]);

    expect(await screen.findByText("genesis.epub")).toBeInTheDocument();
    const names = screen.getAllByText(/\.epub$/).map((el) => el.textContent);
    expect(names).toEqual(["genesis.epub", "exodus.epub", "leviticus.epub"]);
  });

  it("throttles upload POSTs to the concurrency pool, draining the queue", async () => {
    let inFlight = 0;
    let maxInFlight = 0;
    const pending: Array<() => void> = [];
    const stub = routedFetch({
      upload: () => {
        inFlight += 1;
        maxInFlight = Math.max(maxInFlight, inFlight);
        return new Promise<Response>((resolve) => {
          pending.push(() => {
            inFlight -= 1;
            resolve(jsonResponse(accepted("t")));
          });
        });
      },
      // Never resolves: we only care about the upload POSTs here, not polling.
      task: () => new Promise<Response>(() => {}),
    });
    render(<Uploader />);

    const postCount = (): number => stub.mock.calls.filter(([url]) => url === "/api/upload").length;
    const flush = (): Promise<void> =>
      act(async () => {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });

    // Four files with a cap of three: the fourth must wait. Four (not five) is
    // deliberate — it's the batch size where an off-by-one in the spawn count
    // (e.g. re-reading a queue length the workers have already drained) would
    // start only two workers and this assertion would catch it.
    chooseFiles(["1.epub", "2.epub", "3.epub", "4.epub"]);
    await flush();

    // Only the pool's worth of POSTs are in flight; the rest are queued.
    expect(maxInFlight).toBe(3);
    expect(postCount()).toBe(3);

    // Each resolution lets exactly one queued file start, never exceeding the cap.
    while (pending.length > 0) {
      pending.shift()?.();
      await flush();
    }

    expect(postCount()).toBe(4);
    expect(maxInFlight).toBe(3);
  });

  it("caps concurrency globally across overlapping selections, not per-selection", async () => {
    let inFlight = 0;
    let maxInFlight = 0;
    const pending: Array<() => void> = [];
    const stub = routedFetch({
      upload: () => {
        inFlight += 1;
        maxInFlight = Math.max(maxInFlight, inFlight);
        return new Promise<Response>((resolve) => {
          pending.push(() => {
            inFlight -= 1;
            resolve(jsonResponse(accepted("t")));
          });
        });
      },
      task: () => new Promise<Response>(() => {}),
    });
    render(<Uploader />);

    const postCount = (): number => stub.mock.calls.filter(([url]) => url === "/api/upload").length;
    const flush = (): Promise<void> =>
      act(async () => {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });

    // Two batches dropped before any upload resolves. A per-selection pool would
    // run two workers each (four in flight); the shared pool must hold the cap.
    chooseFiles(["a1.epub", "a2.epub"]);
    chooseFiles(["b1.epub", "b2.epub"]);
    await flush();

    expect(maxInFlight).toBe(3);

    // All four still upload exactly once as slots free up.
    while (pending.length > 0) {
      pending.shift()?.();
      await flush();
    }

    expect(postCount()).toBe(4);
    expect(maxInFlight).toBe(3);
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
