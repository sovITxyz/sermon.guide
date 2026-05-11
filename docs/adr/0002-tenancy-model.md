# ADR 0002 — Tenancy model: shared collection with metadata filtering

- **Status:** Accepted
- **Date:** 2026-05-09 (sub-question resolved 2026-05-09 — Option B-book)
- **Deciders:** Cameron (sovITxyz)
- **Consulted:** Reference research paper §"Multi-Tenant Vector Search and Data Isolation"; Pinecone "Multi-Tenancy in Vector Databases"; MongoDB Atlas Vector Search "Flat Indexes for Many Small Tenants" guide
- **Informed:** Future contributors

## Context and Problem Statement

We need to host vectors for ~4,000 tenants, each with up to ~10M chunks (10,000 books × ~1k chunks). We need:

- Strict isolation: a query from tenant A must never return tenant B's vectors.
- Operational viability for a solo developer.
- A path to billion-scale aggregate vectors without re-architecting.

The vector DB layer offers three multi-tenancy patterns:

1. **Store-per-tenant.** Each tenant gets their own Milvus collection (or instance).
2. **Shared collection with metadata filtering.** All vectors live in one collection; queries pre-filter on a `tenant_id` field.
3. **Namespace isolation.** Database-level partition (e.g., Pinecone namespaces, Milvus partition keys).

## Decision Drivers

- Operational simplicity at 4,000 tenants. Per-collection management of 4k+ collections (option 1) is a non-starter.
- Strict isolation. Whatever pattern we pick must survive an automated test that proves it.
- Compatibility with the dedup design (see [ADR 0001](./0001-vector-db-choice.md) and the dedup pipeline in ARCHITECTURE.md). Storing one set of vectors per *book* and pointing user libraries at them is an explicit cost-saving goal.
- Support for hybrid retrieval (dense + sparse) without complicated cross-collection coordination.

## Considered Options

- **A. Store-per-tenant** (4,000 collections).
- **B. Shared collection with metadata filtering** (one collection, `tenant_id` field; pre-filter on every query).
- **C. Namespace / partition-key isolation** (Milvus partition key).

In practice, B and C are blended: we use Milvus's *partition key* feature (database-level partitioning) AND pass an explicit IN-expression on every query, because relying on partition routing alone is not auditable in code review. The sub-question below settles which field gets the partition key.

## Decision Outcome

**Chosen: B + C combined, partition key = `book_id`** (sub-question resolved 2026-05-09 — see below). Single `library_vectors` collection. Vectors are shared globally per book; every search includes `expr=f'book_id IN ({jwt_user_library})'` where `jwt_user_library` is fetched from Postgres `user_library` for the JWT-derived `user_id`. Isolation is visible in code at every call site.

### Rationale

- Pinecone, MongoDB Atlas, and the Milvus team all converge on "one collection, metadata filter, partition key" as the standard pattern for "many small tenants" — see reference PDF §"Multi-Tenant Vector Search and Data Isolation" and Works Cited #2, #18, #20.
- Per-collection (option A) creates 4,000 × N indexes to manage; collection metadata, schema migrations, and Milvus's own coordinator all become bottlenecks.
- Partitioning on `book_id` (vs `tenant_id`) preserves the dedup story end-to-end: shared books store one set of vectors, not one per owning user. This is what the cost projections in ARCHITECTURE.md §1 assume.
- The explicit `expr` filter is the audit surface. `tenant-auditor` (Phase 6) and `/check-tenant-leak` (Phase 6) both grep for `collection.search(` calls and verify a `book_id IN (...)` clause is present and that the IN-set is sourced from Postgres `user_library`, not the request.

### Consequences

- Bug class to watch: any `collection.search(...)` without a `book_id IN (...)` filter is a CVE-class data leak (returns vectors for every book on the platform). The Phase 3 isolation test exists specifically to catch this in CI.
- Every authenticated search incurs a Postgres round-trip to resolve the user's `book_id` set. Acceptable at v0 scale; revisit with caching if it shows up in p95.
- IN-list size scales with library size. Fine at the 10k-books-per-user envelope; if a tenant approaches 100k books we revisit (cursor-paginated set, materialized lookup, or fall back to Option A).

## Sub-question — partition key on `tenant_id` or `book_id`? (Resolved)

This is **ARCHITECTURE.md §7.1**, kept here for ADR-level traceability.

- **Option B-tenant** — partition key = `tenant_id`. Vectors are duplicated per user that owns a book. Isolation is trivial (Milvus prunes whole partitions). MinHash dedup only saves text storage and Postgres rows, not vector storage — defeats much of the point.
- **Option B-book** — partition key = `book_id`. Vectors are shared across all users that own that book. Search filter becomes `book_id IN (<user's library>)`. Maximum dedup. Requires the API to fetch the user's `book_id` set per query (one Postgres round-trip) and pass a potentially long IN-list.

**Status: resolved 2026-05-09 — Option B-book.** The cost projections in ARCHITECTURE.md §1 are predicated on vector-layer dedup; B-tenant would have neutered that. The `tenant_id` field has been dropped from the Milvus schema (see ARCHITECTURE.md §3) — vectors no longer carry a tenant association.

## Pros and Cons of the Options

### A. Store-per-tenant

- ✅ Strongest possible isolation (different physical collections).
- ❌ 4k+ collections to manage. Schema migrations become a fan-out problem.
- ❌ Milvus coordinator overhead grows with collection count.
- ❌ Defeats global dedup entirely.

### B + C. Shared collection, partition-key + explicit filter

- ✅ One schema, one set of indexes.
- ✅ Partition pruning is automatic; explicit filter is audit-visible.
- ✅ Compatible with global dedup at the vector layer (under partition-on-`book_id`).
- ❌ One bug = potential cross-tenant leak. Mitigated by the Phase 3 isolation test, `tenant-auditor`, and `/check-tenant-leak`.

### C alone. Partition key only, no explicit filter

- ✅ Slightly less code.
- ❌ Code review has nothing to grep for. Removed because invisible isolation is unreviewable.

## More Information

- Phase 3 enforces this with a hard-gate isolation test.
- Phase 6 introduces `tenant-auditor` (subagent) and `/check-tenant-leak` (skill).
- Reopens if: a tenant accumulates a vector count where partition-pruned scan exceeds latency budget, in which case we reconsider option A or sharded variants.
