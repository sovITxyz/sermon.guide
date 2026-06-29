"use client";

import { resolveSelection } from "@/lib/selection";
import type { Collection } from "@/lib/types";
import {
  type ReactNode,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

/**
 * Shared library-selection context (Phase 49). The scoped-search feature needs
 * ONE selection — the books the user has ticked, plus the collections they have
 * ticked whole — to be readable on BOTH `/library` (where it is set) and
 * `/search` (where SearchPanel folds it into the POST scope). It is the minimal
 * idiomatic extension of the single-context precedent
 * (components/editor/library-membership.tsx).
 *
 * The two routes are separate server-rendered trees, so the bridge between them
 * is `sessionStorage`, not a shared React parent: the provider HYDRATES from
 * sessionStorage after mount and PERSISTS on every change, so navigating
 * /library -> /search carries the selection across. (Reading sessionStorage
 * during render would desync the first client render from the server HTML, so
 * hydration runs in an effect — the initial state is empty on both sides.)
 *
 * The default context value is an EMPTY, no-op selection (like
 * LibraryMembershipContext's empty Set): a component that reads `useSelection`
 * outside a provider — a stray render, a unit test of SearchPanel with no scope
 * — sees an empty selection (= whole library) rather than throwing.
 *
 * `bookIds`/`collectionIds` are the RAW selection forwarded to the API (which
 * re-resolves collections to their current membership + ownership server-side);
 * `resolved` is the client-side UNION used only for the count/label and React
 * keys (lib/selection.ts).
 */
export interface SelectionValue {
  bookIds: string[];
  collectionIds: string[];
  toggleBook: (bookId: string) => void;
  toggleCollection: (collectionId: string) => void;
  clear: () => void;
  resolved: string[];
}

const EMPTY_SELECTION: SelectionValue = {
  bookIds: [],
  collectionIds: [],
  toggleBook: () => {},
  toggleCollection: () => {},
  clear: () => {},
  resolved: [],
};

const SelectionContext = createContext<SelectionValue>(EMPTY_SELECTION);

/** sessionStorage key for the persisted selection (session-scoped, per tab). */
export const SELECTION_STORAGE_KEY = "sermon.guide:library-selection";

/** Keep only the string elements of an unknown value parsed from storage. */
function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((element): element is string => typeof element === "string")
    : [];
}

export function SelectionProvider({
  collections,
  children,
}: {
  collections: Collection[];
  children: ReactNode;
}) {
  const [bookIds, setBookIds] = useState<string[]>([]);
  const [collectionIds, setCollectionIds] = useState<string[]>([]);

  // Hydrate once from sessionStorage after mount (SSR has no sessionStorage).
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(SELECTION_STORAGE_KEY);
      if (raw === null) {
        return;
      }
      const parsed = JSON.parse(raw) as { bookIds?: unknown; collectionIds?: unknown };
      setBookIds(stringArray(parsed.bookIds));
      setCollectionIds(stringArray(parsed.collectionIds));
    } catch {
      // A malformed/blocked store leaves the empty initial selection in place.
    }
  }, []);

  // Persist on every change so the sibling route reads the latest on its mount.
  useEffect(() => {
    try {
      sessionStorage.setItem(SELECTION_STORAGE_KEY, JSON.stringify({ bookIds, collectionIds }));
    } catch {
      // Storage being unavailable degrades to in-memory-only; never throw.
    }
  }, [bookIds, collectionIds]);

  const toggleBook = useCallback((bookId: string) => {
    setBookIds((prev) =>
      prev.includes(bookId) ? prev.filter((id) => id !== bookId) : [...prev, bookId],
    );
  }, []);

  const toggleCollection = useCallback((collectionId: string) => {
    setCollectionIds((prev) =>
      prev.includes(collectionId)
        ? prev.filter((id) => id !== collectionId)
        : [...prev, collectionId],
    );
  }, []);

  const clear = useCallback(() => {
    setBookIds([]);
    setCollectionIds([]);
  }, []);

  const resolved = useMemo(
    () => resolveSelection(bookIds, collectionIds, collections),
    [bookIds, collectionIds, collections],
  );

  const value = useMemo<SelectionValue>(
    () => ({ bookIds, collectionIds, toggleBook, toggleCollection, clear, resolved }),
    [bookIds, collectionIds, toggleBook, toggleCollection, clear, resolved],
  );

  return <SelectionContext.Provider value={value}>{children}</SelectionContext.Provider>;
}

/** Read the shared selection. Empty no-op selection (whole library) with no provider. */
export function useSelection(): SelectionValue {
  return useContext(SelectionContext);
}
