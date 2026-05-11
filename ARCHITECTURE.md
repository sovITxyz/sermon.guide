# sermon.guide — Architecture

This is the canonical architecture document for sermon.guide. Every phase reads
this file before doing anything; the reference PDFs in `docs/` are kept for
context only and should not be re-read once their decisions land here.

## 1. Goal

A multi-tenant ebook RAG platform for theological libraries and sermon
preparation. A user (pastor, scholar, lay reader) uploads their personal
library and asks natural-language questions ("what does this say about grace?")
and receives a 1–2 paragraph grounded summary with citations back to specific
chunks in their own books.

### Scale targets (v0 design envelope)

| Dimension              | Target                                      |
| ---------------------- | ------------------------------------------- |
| Tenants                | 4,000 concurrent                            |
| Books per tenant       | up to 10,000                                |
| Aggregate vectors      | low billions across all tenants             |
| p95 search latency     | < 50ms (vector) / < 1s (search-summary E2E) |
| Cost (base infra, v0)  | ~$50/mo on a small K8s cluster              |
| LLM inference          | Pay-per-token (Gemini 1.5 Flash)            |

These are the numbers the architecture is designed for. v0 will not actually be
deployed at this scale; the point is that no decision below should preclude
reaching it later.

## 2. Locked decisions

Each row is the decision and a one-line reason. Anything not on this list is
either an [Open Question](#7-open-questions) or out of scope for v0.

| Area                  | Decision                                                | Why                                                                    |
| --------------------- | ------------------------------------------------------- | ---------------------------------------------------------------------- |
| Tenancy model         | Shared collection with metadata filtering               | Operationally simplest at 4k tenants; per-tenant collections don't scale |
| Vector DB             | Milvus, **flat** index                                  | 100% recall and predictable latency once filtered to one tenant's slice |
| Embedding model       | BGE-Large (1024d)                                       | Best open-weight retrieval quality at the size; English-first corpus    |
| Ingestion runtime     | Celery + Redis on Kubernetes with KEDA autoscaling      | Decouples bursty uploads from the API; scales workers to zero           |
| EPUB extraction       | EbookLib → pandoc → markdown                            | Avoids Tika's alt-text pollution / metadata leakage                     |
| PDF extraction        | pymupdf4llm                                             | Markdown-aware, page-structure preserving                               |
| Format detection      | python-magic                                            | MIME sniff, not extension trust                                         |
| Dedup                 | MinHash LSH on lemmatized 5-shingles, threshold 0.85    | Catches near-duplicates (different editions); ~80% storage savings      |
| Chunking              | LlamaIndex SemanticSplitterNodeParser                   | Boundary-on-meaning beats fixed token windows for theology / narrative  |
| Search (retrieval)    | Hybrid: dense BGE + sparse BM25, fused via RRF (k=60)   | Themes via dense, names/refs via sparse; RRF avoids score normalization |
| Reranking             | Cross-encoder (ms-marco-MiniLM-L-6-v2) on top-30 → top-10 | Precision pass before LLM context; cheap                              |
| Context pruning       | BGE-M3 semantic highlighting, sentence-level, threshold 0.5 | 70–80% token reduction into the LLM                                  |
| LLM                   | Gemini 1.5 Flash                                        | Cheapest high-context model; 2M token window for synthesis              |
| Frontend              | Next.js 15 (app router) + Tailwind, TypeScript strict   | Server components keep JWT in HttpOnly cookies, never in browser JS     |
| Raw file storage      | Cloudflare R2 or Backblaze B2 (S3-compatible)           | Cheap object storage for the originals; Postgres only stores pointers   |

## 3. Milvus schema — `library_vectors`

Single collection, partitioned by `book_id`. Vectors are shared globally per
book — when two users own the same deduped book, they retrieve the same
vector rows (see [§4 dedup invariant](#4-postgres-schema-sketch)). Tenant
isolation lives at the **API layer**: every search resolves the authenticated
user's `book_id` set from Postgres `user_library` and passes
`book_id IN (<set>)` as the Milvus `expr`. There is no `tenant_id` field on
the vector — vector-level tenancy would defeat the dedup story
(see [§7.1](#71-dedup-vs-isolation-milvus-partition-key)).

| Field           | Type                   | Notes                                                              |
| --------------- | ---------------------- | ------------------------------------------------------------------ |
| `id`            | INT64, primary key     | Auto-generated                                                     |
| `vector`        | FloatVector (dim=1024) | BGE-Large output (L2-normalized; metric `COSINE` ≡ inner product)  |
| `book_id`       | VarChar                | **Partition key.** FK in spirit to Postgres `global_books.book_id` |
| `content_chunk` | VarChar                | The chunk's raw markdown                                           |
| `metadata`      | JSON                   | `{filename, chunk_index, parent_section, page?}`                   |

Index: **flat** on `vector`, metric `COSINE`. HNSW would be faster for
unfiltered global searches, but every real query is filtered to one user's
book set (typically ≪ a few million vectors), and exhaustive scan over the
filtered partitions is both cheaper to operate and gives 100% recall. Revisit
only if filtered p95 latency exceeds target.

## 4. Postgres schema sketch

Five tables. SQLAlchemy models land in `worker/db/models.py` in Phase 7; this
section is the source of truth for what they look like.

| Table         | PK             | Key fields                                          | Notes                                                    |
| ------------- | -------------- | --------------------------------------------------- | -------------------------------------------------------- |
| `users`       | `user_id`      | `email`, `password_hash`, `created_at`              | bcrypt; one user = one tenant in v0                      |
| `global_books` | `book_id`     | `isbn?`, `title`, `author`, `minhash_signature`, `text_pointer` | One row per *deduplicated* book; `text_pointer` to R2/B2 |
| `user_library` | `entry_id`    | `user_id`, `book_id`, `category`, `added_at`        | M:N join; this is "user A owns book X"                   |
| `highlights`  | `highlight_id` | `user_id`, `book_id`, `content`, `vector_id?`       | Doubly-scoped: queries always filter `user_id AND book_id` |
| `collections` | `collection_id` | `user_id`, `name`, `description`                   | User-defined groupings (e.g. "commentaries", "Reformed") |

**Dedup invariant.** When user B uploads a book whose MinHash matches an
existing `global_books` row, no new vectors are written: only a `user_library`
row pointing at the existing `book_id`. This is what makes the platform cheap.

**Tenant invariant.** Every query that touches `user_library`, `highlights`,
or `collections` MUST filter by `user_id` derived from the request's JWT.
Every Milvus search MUST include `book_id IN (<set>)` in `expr`, where the
set is the JWT-authenticated user's `book_id`s loaded from `user_library`.
The `tenant-auditor` subagent (Phase 6) and `/check-tenant-leak` skill exist
to enforce this.

## 5. Request lifecycle (v0)

```
upload:
  client → POST /upload (JWT) → api saves to local/R2 → enqueue Celery task
                                                   → return task_id
  worker:
    detect format → extract markdown → MinHash signature
      ├── duplicate? → insert user_library row only.       done.
      └── new?       → chunk → embed (BGE-Large) → insert
                                  global_books + library_vectors + user_library

search-summary:
  client → POST /search-summary (JWT) → api
    → embed(query) with BGE-Large
    → resolve user's book_id set from Postgres user_library
    → Milvus filtered search (expr=book_id IN <set>)  → top-30 dense
    → Postgres tsvector BM25                          → top-30 sparse
    → RRF fuse                                        → top-30
    → cross-encoder rerank                            → top-10
    → BGE-M3 sentence highlighting (drop < 0.5)       → pruned context
    → Gemini 1.5 Flash with citation prompt           → {summary, citations}
```

## 6. Out of scope for v0

Listed so they aren't accidentally drag-built into earlier phases.

- Multi-region replication. Single-region only.
- Mobile / native clients. Web only.
- Graph RAG (multi-hop concept graphs).
- Semantic query caching (Query → Response cache with similarity ≥ 0.95). Worth
  ~30% LLM cost reduction at scale; defer until traffic data shows it pays off.
- Highlight / note import from Kindle, Logos, etc.
- Per-tenant rate limits and quotas.
- Hierarchical / parent-document retrieval beyond what semantic chunking gives.
- KEDA + production k8s manifests. Phase 1 ships `docker-compose` only.

## 7. Open Questions

These are the decisions blocking later phases. They must be resolved before
the phase that depends on them; do not drift past the "decide before" line
without an answer recorded back into this file.

### 7.1 Dedup vs isolation: Milvus partition key

**Resolved 2026-05-09: Option B — partition on `book_id`.**

Vectors are shared globally per book; the dedup story works end-to-end at the
vector layer (the cost projections in §1 depend on this). Tenant isolation
moves to the API: every search resolves the authenticated user's `book_id`
set from Postgres `user_library` and passes `book_id IN (<set>)` as the
Milvus `expr`. The `tenant_id` field is dropped from the schema (see §3).
Tradeoffs accepted: a Postgres round-trip per query and a potentially long
IN-list (fine at 10k books per user, awkward at 100k); an unfiltered Milvus
search now returns vectors across the whole platform, so the Phase 3
isolation test, `tenant-auditor` subagent, and `/check-tenant-leak` skill
become the load-bearing audit surface.

Option A (partition on `tenant_id`, vectors duplicated per user) was
considered and rejected: storage savings would only land at raw text +
Postgres, not where the bytes actually are.

### 7.2 Highlights: separate Milvus collection or same with `content_type` field?

**Decide before Phase 11.**

- **Separate collection** `highlight_vectors`: clean separation, no risk of
  highlights leaking into book-text searches and vice versa. Two indexes
  to maintain.
- **Same collection** with a `content_type` field (`"book" | "highlight"`):
  one index, every search filter must include `content_type==...`. One more
  thing for `tenant-auditor` to check.

Defer until we see how highlight queries actually look in practice.

### 7.3 LICENSE — Apache-2.0 vs AGPL-3.0

**Resolved 2026-05-09: AGPL-3.0.**

Network-copyleft. Anyone running a modified version of sermon.guide as a
service must publish their changes. This prevents proprietary SaaS forks
from absorbing the project's work without contributing back. Tradeoff: some
contributors and downstream commercial users will pass. Acceptable for a
project whose moat is being a non-commercial-friendly research tool.

MIT was considered and rejected: too permissive for a SaaS-shaped project.
