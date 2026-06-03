"use client";

import { isTerminal, taskLabel, taskPhase } from "@/lib/tasks";
import type { TaskStatus, UploadAccepted } from "@/lib/types";
import Link from "next/link";
import { type DragEvent, useCallback, useRef, useState } from "react";

interface UploadItem {
  id: string;
  filename: string;
  status: string;
  duplicate: boolean;
  error: string | null;
}

const POLL_MS = 2000;

export function Uploader() {
  const [items, setItems] = useState<UploadItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const seq = useRef(0);

  const updateItem = useCallback((id: string, patch: Partial<UploadItem>) => {
    setItems((prev) => prev.map((item) => (item.id === id ? { ...item, ...patch } : item)));
  }, []);

  const poll = useCallback(
    (id: string, taskId: string) => {
      const tick = async (): Promise<void> => {
        try {
          const res = await fetch(`/api/tasks/${encodeURIComponent(taskId)}`);
          if (!res.ok) {
            updateItem(id, { error: "Lost track of this upload." });
            return;
          }
          const data = (await res.json()) as TaskStatus;
          updateItem(id, {
            status: data.status,
            duplicate: data.result?.was_duplicate ?? false,
          });
          if (!isTerminal(data.status)) {
            window.setTimeout(() => void tick(), POLL_MS);
          }
        } catch {
          updateItem(id, { error: "Network error while polling." });
        }
      };
      window.setTimeout(() => void tick(), POLL_MS);
    },
    [updateItem],
  );

  const upload = useCallback(
    async (file: File): Promise<void> => {
      seq.current += 1;
      const id = `u${seq.current}-${file.name}`;
      // Optimistic row: the file shows up as "Queued…" the instant it's chosen,
      // before the network round-trip resolves.
      setItems((prev) => [
        { id, filename: file.name, status: "PENDING", duplicate: false, error: null },
        ...prev,
      ]);
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

  const onFiles = useCallback(
    (files: FileList | null) => {
      if (!files) {
        return;
      }
      for (const file of Array.from(files)) {
        void upload(file);
      }
    },
    [upload],
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
        <p className="mb-3 text-gray-600 text-sm">Drag &amp; drop an EPUB or PDF here</p>
        <label
          htmlFor="file-input"
          className="cursor-pointer rounded bg-black px-3 py-2 font-medium text-sm text-white"
        >
          Choose a file
        </label>
        <input
          id="file-input"
          type="file"
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
