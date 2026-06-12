"use client";

import { ReaderChunk } from "@/components/reader/ReaderChunk";
import {
  type ChunkRect,
  type PositionSnapshot,
  SETTLE_MS,
  appendPlan,
  atBookStart,
  compensatedScrollTop,
  initialWindowPlan,
  mergeWindow,
  prependPlan,
  reachedEnd,
  savedPositionSnapshot,
  shouldPersist,
  visiblePosition,
} from "@/lib/reader-view";
import type { ChunkItem, ChunkWindowResponse, PositionResponse } from "@/lib/types";
import Link from "next/link";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

/** How long the ?chunk=N anchor tint stays before fading back to white. */
const TINT_MS = 2400;

type ReaderStatus = "loading" | "ready" | "notFound" | "empty" | "error";

interface InitialTarget {
  chunk: number;
  ratio: number;
  tint: boolean;
}

interface ReaderProps {
  bookId: string;
  /** Parsed ?chunk=N anchor, or null to open at the saved position / start. */
  anchorChunk: number | null;
}

/**
 * Bidirectional windowed reader (B1 Web / Phase 33).
 *
 * - Plain DOM, no virtualization: ~600 text blocks with windowed fetch is
 *   within budget; revisit only on observed jank.
 * - IntersectionObserver sentinels at both ends each load the adjacent
 *   window; `start` is a chunk_index lower bound, so plans are exact ranges.
 * - Prepends compensate document scrollTop manually by the measured
 *   scrollHeight delta (Safari has no overflow-anchor).
 * - Position persistence: PUT debounced SETTLE_MS after the last scroll
 *   event, flushed with fetch keepalive on pagehide AND on effect teardown
 *   (SPA navigations never fire pagehide). Never PUTs an unchanged position.
 */
export function Reader({ bookId, anchorChunk }: ReaderProps) {
  const [status, setStatus] = useState<ReaderStatus>("loading");
  const [chunks, setChunks] = useState<readonly ChunkItem[]>([]);
  const [atEnd, setAtEnd] = useState(false);
  const [anchored, setAnchored] = useState(false);
  const [tintedChunk, setTintedChunk] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [edgeError, setEdgeError] = useState<string | null>(null);

  const containerRef = useRef<HTMLDivElement | null>(null);
  const topSentinelRef = useRef<HTMLDivElement | null>(null);
  const bottomSentinelRef = useRef<HTMLDivElement | null>(null);

  // The loaded window, readable synchronously from callbacks (plans must not
  // close over stale state); kept in lock-step with the `chunks` state.
  const chunksRef = useRef<readonly ChunkItem[]>([]);
  // Single-flight gate for edge fetches plus an epoch so a fetch started
  // before a re-init (new ?chunk= target) can never clobber the fresh window.
  const fetchingEdge = useRef(false);
  const epoch = useRef(0);
  // Scroll metrics captured just before a prepend commits; consumed by the
  // layout effect that re-anchors the viewport.
  const pendingCompensation = useRef<{ prevTop: number; prevHeight: number } | null>(null);
  const initialTarget = useRef<InitialTarget | null>(null);
  const lastSent = useRef<PositionSnapshot | null>(null);
  const settleTimer = useRef<number | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // ---- initial load -------------------------------------------------------

  useEffect(() => {
    let active = true;
    epoch.current += 1;

    // Full reset: the same instance is re-targeted when a citation link
    // changes ?chunk= on a book that is already open.
    setStatus("loading");
    setChunks([]);
    setAtEnd(false);
    setAnchored(false);
    setTintedChunk(null);
    setEdgeError(null);
    setLoadingMore(false);
    chunksRef.current = [];
    fetchingEdge.current = false;
    pendingCompensation.current = null;
    initialTarget.current = null;
    lastSent.current = null;

    const fetchWindow = (start: number, limit: number): Promise<Response> =>
      fetch(`/api/books/${encodeURIComponent(bookId)}/chunks?start=${start}&limit=${limit}`);

    async function init(): Promise<void> {
      let target: InitialTarget | null =
        anchorChunk !== null ? { chunk: anchorChunk, ratio: 0, tint: true } : null;

      // No ?chunk= anchor: open at the saved position, if any.
      if (target === null) {
        try {
          const res = await fetch(`/api/books/${encodeURIComponent(bookId)}/position`);
          if (!active) {
            return;
          }
          if (res.status === 404) {
            setStatus("notFound");
            return;
          }
          if (res.ok) {
            const data = (await res.json()) as PositionResponse;
            const saved = savedPositionSnapshot(data);
            if (saved) {
              target = { chunk: saved.chunk_index, ratio: saved.offset_ratio, tint: false };
              // Restoring the saved position must not immediately re-PUT it.
              lastSent.current = saved;
            }
          }
          // Non-404 failure: non-fatal, read from the start instead.
        } catch {
          // Network hiccup on the position read is non-fatal too.
        }
      }
      if (!active) {
        return;
      }

      const plan = initialWindowPlan(target?.chunk ?? null);
      let res: Response;
      try {
        res = await fetchWindow(plan.start, plan.limit);
      } catch {
        if (active) {
          setStatus("error");
        }
        return;
      }
      if (!active) {
        return;
      }
      if (res.status === 404) {
        setStatus("notFound");
        return;
      }
      if (!res.ok) {
        setStatus("error");
        return;
      }
      let data = (await res.json()) as ChunkWindowResponse;
      if (!active) {
        return;
      }

      // Anchor / saved position past the book's end comes back as an empty
      // window (`start` is a lower bound) — clamp to the start of the book.
      if (data.chunks.length === 0 && plan.start > 0) {
        target = null;
        try {
          res = await fetchWindow(0, plan.limit);
        } catch {
          if (active) {
            setStatus("error");
          }
          return;
        }
        if (!active) {
          return;
        }
        if (!res.ok) {
          setStatus(res.status === 404 ? "notFound" : "error");
          return;
        }
        data = (await res.json()) as ChunkWindowResponse;
        if (!active) {
          return;
        }
      }

      if (data.chunks.length === 0) {
        setStatus("empty");
        return;
      }

      initialTarget.current = target;
      chunksRef.current = data.chunks;
      setChunks(data.chunks);
      setAtEnd(reachedEnd(data.chunks.length, plan.limit));
      setStatus("ready");
    }

    void init();
    return () => {
      active = false;
    };
  }, [bookId, anchorChunk]);

  // ---- anchor scroll + tint ----------------------------------------------

  // Before first paint of the ready window: jump to the target chunk (anchor
  // or saved position), apply the saved intra-chunk offset, tint anchors.
  useLayoutEffect(() => {
    if (status !== "ready" || anchored) {
      return;
    }
    const target = initialTarget.current;
    if (target) {
      const el = containerRef.current?.querySelector(`[data-chunk-index="${target.chunk}"]`);
      if (el instanceof HTMLElement) {
        el.scrollIntoView({ block: "start" });
        if (target.ratio > 0) {
          window.scrollBy(0, Math.round(target.ratio * el.getBoundingClientRect().height));
        }
        if (target.tint) {
          setTintedChunk(target.chunk);
        }
      }
    } else {
      window.scrollTo(0, 0);
    }
    setAnchored(true);
  }, [status, anchored]);

  useEffect(() => {
    if (tintedChunk === null) {
      return;
    }
    const id = window.setTimeout(() => setTintedChunk(null), TINT_MS);
    return () => window.clearTimeout(id);
  }, [tintedChunk]);

  // ---- bidirectional windowing -------------------------------------------

  const loadEdge = useCallback(
    async (direction: "prepend" | "append"): Promise<void> => {
      if (fetchingEdge.current) {
        return;
      }
      const plan =
        direction === "prepend" ? prependPlan(chunksRef.current) : appendPlan(chunksRef.current);
      if (!plan) {
        return;
      }
      const myEpoch = epoch.current;
      fetchingEdge.current = true;
      setLoadingMore(true);
      try {
        const res = await fetch(
          `/api/books/${encodeURIComponent(bookId)}/chunks?start=${plan.start}&limit=${plan.limit}`,
        );
        if (!mounted.current || epoch.current !== myEpoch) {
          return;
        }
        if (!res.ok) {
          setEdgeError("Could not load more of the book. Scroll to retry.");
          return;
        }
        const data = (await res.json()) as ChunkWindowResponse;
        if (!mounted.current || epoch.current !== myEpoch) {
          return;
        }
        setEdgeError(null);
        const merged = mergeWindow(chunksRef.current, data.chunks, direction);
        if (merged !== chunksRef.current) {
          if (direction === "prepend") {
            // Measure BEFORE the rows above the viewport commit; the layout
            // effect below restores the visual position from the delta.
            const scroller = document.scrollingElement;
            if (scroller) {
              pendingCompensation.current = {
                prevTop: scroller.scrollTop,
                prevHeight: scroller.scrollHeight,
              };
            }
          }
          chunksRef.current = merged;
          setChunks(merged);
        }
        if (direction === "append" && reachedEnd(data.chunks.length, plan.limit)) {
          setAtEnd(true);
        }
      } catch {
        if (mounted.current && epoch.current === myEpoch) {
          setEdgeError("Network error while loading more of the book. Scroll to retry.");
        }
      } finally {
        fetchingEdge.current = false;
        if (mounted.current) {
          setLoadingMore(false);
        }
      }
    },
    [bookId],
  );

  // Manual scrollTop compensation on prepend (Safari lacks overflow-anchor):
  // runs after the DOM update but before paint, so no flicker.
  useLayoutEffect(() => {
    if (chunks.length === 0) {
      return;
    }
    const pending = pendingCompensation.current;
    if (!pending) {
      return;
    }
    pendingCompensation.current = null;
    const scroller = document.scrollingElement;
    if (scroller) {
      scroller.scrollTop = compensatedScrollTop(
        pending.prevTop,
        pending.prevHeight,
        scroller.scrollHeight,
      );
    }
  }, [chunks]);

  // Sentinel observer. Re-created whenever the window changes because
  // IntersectionObserver only reports crossings: after a merge, a sentinel
  // still inside the prefetch margin must re-fire immediately, not wait for
  // the next scroll.
  useEffect(() => {
    if (status !== "ready" || !anchored) {
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) {
            continue;
          }
          if (entry.target === topSentinelRef.current) {
            void loadEdge("prepend");
          } else if (entry.target === bottomSentinelRef.current) {
            void loadEdge("append");
          }
        }
      },
      { rootMargin: "600px 0px" },
    );
    const top = atBookStart(chunks) ? null : topSentinelRef.current;
    const bottom = atEnd ? null : bottomSentinelRef.current;
    if (top) {
      observer.observe(top);
    }
    if (bottom) {
      observer.observe(bottom);
    }
    return () => observer.disconnect();
  }, [status, anchored, chunks, atEnd, loadEdge]);

  // ---- position persistence ----------------------------------------------

  const capturePosition = useCallback((): PositionSnapshot | null => {
    const container = containerRef.current;
    // isConnected guards the unmount flush: rects of detached nodes are all
    // zeros and would persist garbage ("end of book").
    if (!container || !container.isConnected) {
      return null;
    }
    const rects: ChunkRect[] = [];
    for (const node of container.querySelectorAll<HTMLElement>("[data-chunk-index]")) {
      const index = Number(node.dataset.chunkIndex);
      if (!Number.isInteger(index) || index < 0) {
        continue;
      }
      const rect = node.getBoundingClientRect();
      rects.push({ chunk_index: index, top: rect.top, height: rect.height });
    }
    return visiblePosition(rects, 0);
  }, []);

  const persist = useCallback(
    (snapshot: PositionSnapshot, keepalive: boolean): void => {
      if (!shouldPersist(lastSent.current, snapshot)) {
        return;
      }
      fetch(`/api/books/${encodeURIComponent(bookId)}/position`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          chunk_index: snapshot.chunk_index,
          offset_ratio: snapshot.offset_ratio,
        }),
        keepalive,
      })
        .then((res) => {
          if (res.ok) {
            lastSent.current = snapshot;
          }
          // Non-ok: leave lastSent unchanged so the next settle retries.
        })
        .catch(() => {
          // Best-effort: the next scroll-settle or flush retries.
        });
    },
    [bookId],
  );

  useEffect(() => {
    if (status !== "ready" || !anchored) {
      return;
    }
    const onScroll = (): void => {
      if (settleTimer.current !== null) {
        window.clearTimeout(settleTimer.current);
      }
      settleTimer.current = window.setTimeout(() => {
        settleTimer.current = null;
        const snapshot = capturePosition();
        if (snapshot) {
          persist(snapshot, false);
        }
      }, SETTLE_MS);
    };
    const flush = (): void => {
      if (settleTimer.current !== null) {
        window.clearTimeout(settleTimer.current);
        settleTimer.current = null;
      }
      const snapshot = capturePosition();
      if (snapshot) {
        persist(snapshot, true);
      }
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("pagehide", flush);
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("pagehide", flush);
      // SPA navigations never fire pagehide — flush on teardown too.
      flush();
    };
  }, [status, anchored, capturePosition, persist]);

  // ---- render --------------------------------------------------------------

  if (status === "loading") {
    return <p className="text-gray-600 text-sm">Loading the book…</p>;
  }
  if (status === "notFound") {
    return (
      <div className="rounded-lg border border-gray-300 border-dashed p-8 text-center text-gray-600 text-sm">
        <p className="mb-2">Book not found.</p>
        <Link href="/library" className="text-blue-600 hover:underline">
          Back to your library
        </Link>
      </div>
    );
  }
  if (status === "empty") {
    return (
      <div className="rounded-lg border border-gray-300 border-dashed p-8 text-center text-gray-600 text-sm">
        This book has no readable content yet.
      </div>
    );
  }
  if (status === "error") {
    return (
      <p role="alert" className="text-red-600 text-sm">
        Could not load the book. Please refresh to try again.
      </p>
    );
  }

  return (
    <div>
      {anchored && !atBookStart(chunks) ? (
        <div ref={topSentinelRef} aria-hidden="true" className="h-px" />
      ) : null}
      <div ref={containerRef}>
        {chunks.map((chunk) => (
          <ReaderChunk
            key={chunk.chunk_index}
            chunk={chunk}
            tinted={chunk.chunk_index === tintedChunk}
          />
        ))}
      </div>
      {anchored && !atEnd ? (
        <div ref={bottomSentinelRef} aria-hidden="true" className="h-px" />
      ) : null}
      {edgeError ? (
        <p role="alert" className="py-3 text-center text-red-600 text-sm">
          {edgeError}
        </p>
      ) : null}
      {loadingMore ? <p className="py-4 text-center text-gray-500 text-sm">Loading more…</p> : null}
      {atEnd ? <p className="py-6 text-center text-gray-400 text-sm">— End of book —</p> : null}
    </div>
  );
}
