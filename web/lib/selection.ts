import type { Collection } from "./types";

/**
 * Pure resolver for the shared library selection (Phase 49). No DOM, no
 * server-only imports — unit-tested in test/selection.test.ts and safe to run on
 * the edge or in the SelectionProvider's `useMemo`.
 *
 * Given the ad-hoc checked `bookIds` and the whole-collection `collectionIds`,
 * plus the user's `collections`, return the UNION of distinct book ids the
 * selection resolves to: every ad-hoc book, then every member book of each
 * selected collection. The result is deduped and stable-ordered (ad-hoc books in
 * selection order first, then collection members in collection / member order),
 * so it is a deterministic React key source and a stable count for the UI label.
 *
 * A `collectionId` that names no current collection is IGNORED — a stale id left
 * over from a deleted collection must never throw or fabricate phantom books.
 * The resolved set is ONLY a client-side count/label aid; the load-bearing
 * scoping (intersect-with-library, ownership-check each collection) happens
 * server-side, so resolving against a slightly stale `collections` snapshot here
 * can never widen the actual search.
 */
export function resolveSelection(
  bookIds: readonly string[],
  collectionIds: readonly string[],
  collections: readonly Collection[],
): string[] {
  const byId = new Map(collections.map((collection) => [collection.collection_id, collection]));
  const seen = new Set<string>();
  const out: string[] = [];
  const push = (bookId: string): void => {
    if (!seen.has(bookId)) {
      seen.add(bookId);
      out.push(bookId);
    }
  };
  for (const bookId of bookIds) {
    push(bookId);
  }
  for (const collectionId of collectionIds) {
    const collection = byId.get(collectionId);
    if (!collection) {
      continue;
    }
    for (const bookId of collection.book_ids) {
      push(bookId);
    }
  }
  return out;
}
