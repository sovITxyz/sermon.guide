"use client";

import { type ReactNode, createContext, useContext } from "react";

/**
 * Library-membership context for citation node views (Phase 37, B2 slice D).
 *
 * The degraded-badge decision ("this cited book is no longer in your library")
 * MUST NOT cost one network fetch per citation — a manuscript can carry dozens.
 * Per the pre-made decision, the editor shell resolves the owned-`book_id` set
 * ONCE with a single `/library` fetch when the doc opens and provides it here;
 * every CitationView reads the shared set from context to decide owned-vs-
 * degraded. ZERO per-citation fetches.
 *
 * The default is an EMPTY set, not null: a node view rendered outside a provider
 * (e.g. a stray render before wiring, or a test) treats every citation as
 * degraded rather than throwing — the cached snippet still renders, which is the
 * safe failure mode (degraded is purely additive UI; it never hides content).
 */
const LibraryMembershipContext = createContext<ReadonlySet<string>>(new Set<string>());

export function LibraryMembershipProvider({
  ownedBookIds,
  children,
}: {
  ownedBookIds: ReadonlySet<string>;
  children: ReactNode;
}) {
  return (
    <LibraryMembershipContext.Provider value={ownedBookIds}>
      {children}
    </LibraryMembershipContext.Provider>
  );
}

/** Read the shared owned-`book_id` set. Empty (everything degraded) with no provider. */
export function useLibraryMembership(): ReadonlySet<string> {
  return useContext(LibraryMembershipContext);
}
