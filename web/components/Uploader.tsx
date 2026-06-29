"use client";

import { isTerminal, taskLabel, taskPhase } from "@/lib/tasks";
import type { TaskStatus, UploadAccepted } from "@/lib/types";
import Link from "next/link";
import { type DragEvent, useCallback, useEffect, useRef, useState } from "react";

interface UploadItem {
  id: string;
  filename: string;
  status: string;
  duplicate: boolean;
  error: string | null;
}

const POLL_MS = 2000;
// Cap simultaneous upload POSTs. Selecting a whole shelf of books shouldn't fire
// dozens of multipart POSTs at once — that hits the browser's per-host
// connection limit and piles onto the ingest queue. Polls (cheap GETs) stay
// unbounded; only the uploads are throttled. The next file starts as soon as an
// in-flight POST resolves.
const UPLOAD_CONCURRENCY = 3;

export function Uploader() {
  const [items, setItems] = useState<UploadItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const seq = useRef(0);

  // Component-scoped upload queue + live-worker count. Shared across every
  // onFiles call so the concurrency cap is GLOBAL: dropping a second batch
  // while the first is still uploading feeds the same queue and reuses the
  // running workers instead of spinning up another full pool.
  const uploadQueue = useRef<{ id: string; file: File }[]>([]);
  const activeWorkers = useRef(0);

  // Tracks whether the component is still mounted so the recursive poll below
  // stops when the user navigates away mid-ingest. Without this, the
  // self-rescheduling setTimeout would keep fetching /api/tasks forever (and
  // setState on an unmounted component) for any non-terminal task.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const updateItem = useCallback((id: string, patch: Partial<UploadItem>) => {
    setItems((prev) => prev.map((item) => (item.id === id ? { ...item, ...patch } : item)));
  }, []);

  const poll = useCallback(
    (id: string, taskId: string) => {
      const tick = async (): Promise<void> => {
        if (!mounted.current) {
          return;
        }
        try {
          const res = await fetch(`/api/tasks/${encodeURIComponent(taskId)}`);
          if (!mounted.current) {
            return;
          }
          if (!res.ok) {
            updateItem(id, { error: "Lost track of this upload." });
            return;
          }
          const data = (await res.json()) as TaskStatus;
          if (!mounted.current) {
            return;
          }
          updateItem(id, {
            status: data.status,
            duplicate: data.result?.was_duplicate ?? false,
          });
          if (!isTerminal(data.status)) {
            window.setTimeout(() => void tick(), POLL_MS);
          }
        } catch {
          if (mounted.current) {
            updateItem(id, { error: "Network error while polling." });
          }
        }
      };
      window.setTimeout(() => void tick(), POLL_MS);
    },
    [updateItem],
  );

  // Send one already-queued file. The optimistic row is created up front in
  // `onFiles`; this only does the POST + hands off to `poll`. Resolves once the
  // POST round-trip settles so the worker pool can pull the next file.
  const upload = useCallback(
    async (id: string, file: File): Promise<void> => {
      try {
        const form = new FormData();
        form.append("file", file);
        const res = await fetch("/api/upload", { method: "POST", body: form });
        if (!res.ok) {
          const data = (await res.json().catch(() => null)) as { error?: string } | null;
          updateItem(id, { error: data?.error ?? "Upload failed." });
          return;
        }
        const data = (await res.json()) as UploadAccepted;
        poll(id, data.task_id);
      } catch {
        updateItem(id, { error: "Network error during upload." });
      }
    },
    [poll, updateItem],
  );

  // One worker of the pool: drain the shared queue one file at a time until it
  // is empty, then retire (decrementing the live count). The `mounted` check
  // doubles as the loop guard so a worker stops pulling new files the moment the
  // user navigates away — abandoned uploads never fire their POST. `upload`
  // never rejects (it catches its own errors), so the count is always balanced.
  const runWorker = useCallback(async (): Promise<void> => {
    while (mounted.current) {
      const next = uploadQueue.current.shift();
      if (!next) {
        break;
      }
      await upload(next.id, next.file);
    }
    activeWorkers.current -= 1;
  }, [upload]);

  const onFiles = useCallback(
    (files: FileList | null) => {
      if (!files || files.length === 0) {
        return;
      }
      // Mint a stable id per file and show every row as "Queued…" at once, in
      // selection order. Appending the whole batch in one setItems keeps the
      // rows in the order they were picked (prepending each would reverse a
      // multi-select) and decouples row order from upload-start order.
      const batch = Array.from(files).map((file) => {
        seq.current += 1;
        return { id: `u${seq.current}-${file.name}`, file };
      });
      setItems((prev) => [
        ...prev,
        ...batch.map(({ id, file }) => ({
          id,
          filename: file.name,
          status: "PENDING",
          duplicate: false,
          error: null,
        })),
      ]);
      uploadQueue.current.push(...batch);

      // Top the pool up to the cap. Already-running workers will pick up the
      // freshly queued files, so we only start as many new workers as there are
      // free slots AND queued items — never more than UPLOAD_CONCURRENCY total.
      while (activeWorkers.current < UPLOAD_CONCURRENCY && uploadQueue.current.length > 0) {
        activeWorkers.current += 1;
        void runWorker();
      }
    },
    [runWorker],
  );

  const onDrop = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      setDragging(false);
      onFiles(event.dataTransfer.files);
    },
    [onFiles],
  );

  return (
    <div className="space-y-6">
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={`flex flex-col items-center justify-center rounded-lg border-2 border-dashed p-10 text-center ${
          dragging ? "border-blue-500 bg-blue-50" : "border-gray-300"
        }`}
      >
        <p className="mb-3 text-gray-600 text-sm">Drag &amp; drop EPUBs or PDFs here</p>
        <label
          htmlFor="file-input"
          className="cursor-pointer rounded bg-black px-3 py-2 font-medium text-sm text-white"
        >
          Choose files
        </label>
        <input
          id="file-input"
          type="file"
          multiple
          accept=".epub,.pdf,application/epub+zip,application/pdf"
          className="sr-only"
          onChange={(e) => {
            onFiles(e.target.files);
            e.target.value = "";
          }}
        />
      </div>

      {items.length > 0 ? (
        <ul className="divide-y divide-gray-200 rounded-lg border border-gray-200">
          {items.map((item) => {
            const phase = item.error
              ? "failed"
              : taskPhase(item.status, item.duplicate ? { was_duplicate: true } : null);
            return (
              <li
                key={item.id}
                className="flex items-center justify-between gap-4 px-4 py-3 text-sm"
              >
                <span className="truncate">{item.filename}</span>
                <span className="shrink-0 text-gray-600">{item.error ?? taskLabel(phase)}</span>
              </li>
            );
          })}
        </ul>
      ) : null}

      <Link href="/library" className="inline-block text-blue-600 text-sm hover:underline">
        View your library →
      </Link>
    </div>
  );
}
