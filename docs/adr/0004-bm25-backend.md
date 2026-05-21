# ADR 0004 — BM25 backend: Postgres `tsvector`

- **Status:** Accepted
- **Date:** 2026-05-21
- **Deciders:** Cameron (sovITxyz)
- **Consulted:** Reference research paper §"Hybrid Retrieval and Reciprocal Rank Fusion"; PostgreSQL docs ch. 12 "Full Text Search"; ParadeDB `pg_search` README; Milvus 2.6 "Full-Text Search (BM25)" guide
- **Informed:** Future contributors

## Context and Problem Statement

ARCHITECTURE.md §2 locks the retrieval strategy as **hybrid: dense BGE +
sparse BM25, fused via Reciprocal Rank Fusion (RRF, k=60)**. The dense arm
is settled by [ADR 0001](./0001-vector-db-choice.md) (Milvus, flat index)
and [ADR 0003](./0003-embedding-model-choice.md) (BGE-Large). Phase 12
needs to pick the *sparse* arm: where does the lexical index live and how
do we score it.

Constraints carried over from earlier phases:

- **Tenant scoping is shared with the dense arm.** Every sparse query
  must filter by the JWT-derived user's `book_id` set, the same way
  `api/search.py` already scopes Milvus (CLAUDE.md tenant invariant,
  ARCHITECTURE.md §7.1). The sparse backend must support an efficient
  `book_id IN (<user's library>)` predicate alongside the text match.
- **No new operational surface for v0.** Phase 1 settled on a
  docker-compose stack of Milvus + etcd + MinIO + Redis + Postgres. A
  new long-running service (Elasticsearch, OpenSearch, Tantivy daemon)
  would multiply the v0 ops cost — Phase 1 already documents Milvus's
  heavier footprint as a deliberate tradeoff.
- **Per-chunk granularity.** RRF fuses lists where the *identity* is a
  single retrievable unit. Both arms must agree on what a "result" is —
  the chunk, identified by `(book_id, chunk_index)`. Anything that
  scores at document level (book/file) would have to fan back out to
  chunks before fusion.

## Decision Drivers

- **Operational footprint.** Solo-dev v0; one less long-running service
  is one less thing to monitor, backup, and version.
- **Joinability with relational data.** Tenant scoping needs the user's
  `book_id` set; that set lives in Postgres `user_library`. A backend
  that can join its index against a Postgres array push the IN-list down
  to the index scan rather than round-tripping IDs back to the API.
- **License + self-host.** Same constraint as [ADR 0001](./0001-vector-db-choice.md):
  must run under the AGPL-3.0 product without per-instance fees.
- **Quality good enough for the fusion arm.** RRF combines *ranks*, not
  raw scores — sparse-side ranking only needs to be *approximately*
  BM25-class. Catastrophic ranking failures matter; small-constant
  improvements over `ts_rank_cd` do not (golden tests would catch a
  regression either way).
- **Migration cost.** v0 corpus is already in Milvus; the sparse
  backend's bootstrap cost is whatever it takes to populate it from
  what we already have on disk + in Postgres `global_books`.

## Considered Options

- **Postgres `tsvector` + `ts_rank_cd`** on a new `chunks` table — GIN
  index, generated `tsvector` column, scoring via Postgres's built-in
  cover-density ranker.
- **Milvus 2.6 native sparse vectors with the BM25 function** — Milvus
  2.6 added `BM25EmbeddingFunction` and `SPARSE_INVERTED_INDEX`; the
  hybrid_search API would do dense+sparse+RRF (or weighted) inside
  Milvus itself.
- **Elasticsearch / OpenSearch** — the canonical BM25 implementation;
  every nontrivial production search system has had one at some point.
- **ParadeDB `pg_search` extension** — Postgres extension that embeds
  Tantivy (Lucene-class scoring + true BM25, document scoring, faceted
  search) inside Postgres.
- **In-process rank_bm25 over Postgres-stored chunk text** — Python
  library, scan-and-score at query time.

## Decision Outcome

**Chosen: Postgres `tsvector` + GIN index + `ts_rank_cd`.**

Concretely (full spec lives in ARCHITECTURE.md §3.5 + §4): a new
`chunks` table with one row per ingested chunk; a `tsv` column declared
`GENERATED ALWAYS AS to_tsvector('english', content) STORED`; a GIN
index on `tsv`. Each sparse search is a single SQL statement:

```sql
SELECT chunk_id, book_id, chunk_index, content, metadata,
       ts_rank_cd(tsv, q) AS rank
  FROM chunks, websearch_to_tsquery('english', :query) AS q
 WHERE tsv @@ q
   AND book_id = ANY(:book_ids)
 ORDER BY rank DESC
 LIMIT :limit;
```

The `book_id = ANY(:book_ids)` clause is the same tenant-scoping
invariant as the Milvus arm, parameterised on the user's
`book_id` set resolved from `user_library` per request (CLAUDE.md tenant
invariant).

### Rationale

- **No new service.** Postgres is already in the v0 docker-compose
  stack ([Phase 1](../PHASES.md)). Adding a table + an index is the
  marginal-cost-zero choice on the ops axis. Elasticsearch and a
  Tantivy daemon would each be a new long-running container, a new
  failure mode, and a new backup story.
- **Tenant scoping is push-down, not round-trip.** The `book_id = ANY(...)`
  predicate runs on the index scan against a B-tree on `chunks.book_id`,
  combined with the GIN-on-tsv via Postgres's bitmap heap scan. No
  ID exchange between services per query.
- **Per-chunk granularity is native.** The unit of retrieval is the
  chunk row, identified by `(book_id, chunk_index)` — the same key the
  dense arm produces from Milvus metadata, so RRF fusion is a plain
  dict merge in `worker/retrieval.py:rrf_fuse`.
- **`ts_rank_cd` is "BM25-class enough" for the fusion arm.** It is not
  literally BM25 — Postgres weighs term frequency, position, and cover
  density rather than k1/b-parameterised IDF. RRF cares about *ranks*,
  not absolute scores, so the difference between `ts_rank_cd` and true
  BM25 mostly disappears once it gets folded into `1/(60 + rank)`.
  Golden tests gate on top-K hit/miss, which is what we care about.
- **Operational risk is low.** GIN index build is bounded by corpus
  size (low millions of chunks in v0 — minutes). `CREATE INDEX
  CONCURRENTLY` exists if we ever need to rebuild on a hot table.
  `schema-reviewer` subagent already checks for this pattern.
- **Open path to upgrade.** If `ts_rank_cd` ever measurably underperforms
  (a future golden-test regression we can't fix by tuning), ParadeDB
  `pg_search` is a drop-in extension upgrade — same `chunks` table,
  same SQL shape, swap `to_tsvector`/`ts_rank_cd` for `bm25_*`. We are
  not locking ourselves out of true BM25.

### Consequences

- A new write path during ingest: `worker/ingest.py` must insert
  `chunks` rows alongside the Milvus vectors. Both writes happen in the
  same `global_books` transaction so a crash leaves no half-state in
  Postgres; the existing crash-window between Milvus insert and
  Postgres commit (worker/AGENTS.md "Idempotency caveat") still
  applies — orphan Milvus vectors are possible, orphan `chunks` rows
  are not.
- A backfill is needed for the Phase 11 corpus (sample.pdf etc.) that
  exists in Milvus but predates the `chunks` table.
  `worker/scripts/backfill_chunks.py` reads Milvus partitions
  per-`book_id` and populates `chunks`; idempotent via the
  `uq_chunks_book_chunk` unique constraint.
- BM25 scoring is configured for English (`'english'` text-search
  config). v0 corpus is English-first by design (ARCHITECTURE.md §1
  "English-first corpus"). When multilingual lands, we add a `language`
  column to `global_books` and pick the regconfig per-row.
- `ts_rank_cd` is not literally BM25. The ADR (and ARCHITECTURE.md §3.5)
  use "BM25" as shorthand for the sparse lexical arm, matching how the
  research paper and ARCHITECTURE.md §2 use the term. A future ADR
  amendment is the place to revisit if the shorthand becomes misleading
  — e.g. when ParadeDB lands.

## Pros and Cons of the Options

### Postgres `tsvector` + `ts_rank_cd`

- ✅ Zero new infra. Already in docker-compose.
- ✅ Tenant scoping push-down via `ANY(:book_ids)` on a B-tree.
- ✅ Per-chunk granularity native; identity matches the dense arm.
- ✅ Generated column means `tsv` stays in sync with `content` without
  application code.
- ❌ Not literally BM25 — `ts_rank_cd` is cover-density ranking. Mostly
  invisible inside RRF; documented above.
- ❌ Same Postgres now carries both relational schema and the lexical
  index. Capacity-plan accordingly at scale; v0 is nowhere near the
  limit.

### Milvus 2.6 native sparse + BM25 function

- ✅ One backend, one query path.
- ✅ Milvus 2.6 ships `hybrid_search` with built-in RRF — would shrink
  `api/search.py` to roughly one call.
- ❌ Tenant scoping for the sparse arm would need `book_id IN (...)` on
  Milvus's sparse field too — and the IN-list is still resolved from
  Postgres, so we save no round-trip. The only thing saved is the
  Postgres GIN scan, which is the cheap part.
- ❌ Forces every ingest to also compute and store sparse vectors in
  Milvus — doubles write amplification on the dense arm's hot path.
- ❌ BM25 in Milvus is newer (2.6 release) than the rest of our Milvus
  surface; the ecosystem (LlamaIndex / LangChain integrations) is
  uneven. Rejecting now keeps the dense arm boring; revisit if Milvus
  hybrid_search becomes the obvious default in v1.

### Elasticsearch / OpenSearch

- ✅ The gold standard. Real BM25, document scoring, faceted search,
  query DSL, the works.
- ❌ A new long-running JVM service in the v0 docker-compose. Heap
  tuning, mapping changes, snapshot/restore — all things we'd be
  newly on the hook for.
- ❌ Tenant scoping requires shipping the `book_id` set per query;
  pushing it down efficiently means a terms filter that scales
  with library size — no better than Postgres on this axis.
- ❌ Ops cost for the v0 envelope (a few thousand chunks per active
  user, low millions aggregate) is not justified by quality gains.

### ParadeDB `pg_search` (Tantivy in Postgres)

- ✅ True BM25 inside Postgres — best of both axes.
- ✅ Same SQL shape as `tsvector` so migration is mostly mechanical.
- ❌ Requires a Postgres extension build / image; not in the stock
  Postgres 16 image we're running. Adds a maintenance dimension
  (extension version compat across Postgres minor upgrades).
- ❌ Less battle-tested than `tsvector` at the moment. The upgrade
  path is documented above — defer until quality demands it.

### In-process `rank_bm25` over chunk text

- ✅ Pure Python, no schema changes.
- ❌ O(N) scan per query at the API layer; doesn't scale past the
  smallest libraries.
- ❌ No way to push the tenant filter into the index — the index *is*
  the in-memory scan.
- Rejected immediately; listed for completeness.

## More Information

- ARCHITECTURE.md §2 locks "hybrid: dense BGE + sparse BM25, fused via
  RRF (k=60)" — this ADR resolves the *where* and *how* for the sparse
  arm.
- ARCHITECTURE.md §3.5 documents the `chunks` table; §4 carries the row
  in the schema table.
- Revisit if (a) `ts_rank_cd` ranking quality fails a golden test we
  can't recover by query-side tuning, or (b) `pg_search` matures into
  the default Postgres-native BM25 — both routes preserve the chunks
  schema.
