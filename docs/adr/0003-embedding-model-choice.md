# ADR 0003 — Embedding model: BGE-Large (1024d)

- **Status:** Accepted
- **Date:** 2026-05-09
- **Deciders:** Cameron (sovITxyz)
- **Consulted:** MTEB benchmark leaderboard; reference research paper §"Hybrid Search Strategy"; BAAI / FlagEmbedding model card
- **Informed:** Future contributors

## Context and Problem Statement

Pick a single embedding model used for:

- Indexing every chunk written to `library_vectors`.
- Embedding every search query at retrieval time.
- Boundary detection for LlamaIndex SemanticSplitter (Phase 5).

Constraints:

- English-first (Christian theological corpus). Multilingual is a v2 concern.
- Open-weight, runnable on CPU for v0 development; GPU-able for production.
- Compatible with sentence-transformers / FlagEmbedding ecosystem.
- 1,024-dimensional vectors are the implied target — Milvus schema is sized to this.

## Decision Drivers

- Retrieval quality on long-form prose (theology, biblical commentary).
- Inference cost. We embed once at ingest and once per query, so the per-query cost matters a lot for the LLM-orchestration p95.
- Vendor independence — we'd rather not depend on OpenAI / Voyage / Cohere SDKs at the embedding layer.
- Reranking is a separate cross-encoder pass, so the dense model only needs to be a strong recall signal, not a precision gate.

## Considered Options

- **BGE-Large** (`BAAI/bge-large-en-v1.5`) — 1024d, 335M params.
- **BGE-Base** (`BAAI/bge-base-en-v1.5`) — 768d, 110M params.
- **E5-Large-v2** (`intfloat/e5-large-v2`) — 1024d, 335M params.
- **MiniLM-L6** (`sentence-transformers/all-MiniLM-L6-v2`) — 384d, 22M params. Tiny, fast, weak.
- **OpenAI `text-embedding-3-large`** — 3072d (matryoshka), proprietary API.
- **Voyage `voyage-3-large`** — 1024d, proprietary API.

## Decision Outcome

**Chosen: BGE-Large (`BAAI/bge-large-en-v1.5`), 1024-dimensional.**

### Rationale

- Top of MTEB English retrieval among open-weight models in the size class we can actually run.
- 1024d hits a good point on the size/quality curve: sub-2KB per vector, 100M vectors = ~200GB, manageable on modest hardware. 3072d (OpenAI) would 3x storage and Milvus query memory for marginal gains.
- Open weights — we can run on CPU for dev, on a single GPU for production, or batch-embed on a spot-priced GPU pool. No API rate limits; no per-token billing on ingestion.
- Pairs naturally with **bge-reranker** for the cross-encoder pass (Phase 13) and **BGE-M3** for semantic highlighting (Phase 13) — same lineage, same tokenizer family, same prompt conventions.
- LlamaIndex SemanticSplitter accepts any sentence-transformers model, so Phase 5 chunking and Phase 6 embedding can share one cached model.

### Consequences

- We accept slower embeddings on CPU (~50-100 chunks/sec without a GPU). Phase 1 docker-compose runs CPU; production needs GPU.
- We forgo the proprietary models' ~2-5% MTEB advantage at the cost of platform independence. Acceptable.
- 1024d is locked into the Milvus schema (`FloatVector(dim=1024)`). Switching models later means a full re-embed and a schema migration.

## Pros and Cons of the Options

### BGE-Large (chosen)

- ✅ Top-tier MTEB English retrieval in the open-weight 300-400M class.
- ✅ Apache 2.0 license; runs on CPU or single GPU.
- ✅ Pairs with bge-reranker and BGE-M3 (consistent family).
- ❌ ~200ms/query on CPU (acceptable; production uses GPU).

### BGE-Base

- ✅ 3× faster on CPU.
- ❌ ~3-4 points lower on MTEB. Quality matters more than speed at our query volume.
- ❌ 768d would force a different Milvus schema; mixed-dim systems are hard.

### E5-Large-v2

- ✅ Comparable quality to BGE-Large.
- ❌ Requires "query: " / "passage: " prefix conventions; one more thing to get right and to audit.
- ❌ Marginally weaker than BGE-Large on retrieval-specific MTEB subsets.

### MiniLM-L6-v2

- ✅ Tiny, fast, ubiquitous.
- ❌ ~10 points below BGE-Large on retrieval. Fine for prototypes; not for the production retrieval gate.

### OpenAI `text-embedding-3-large`

- ✅ Best closed-weight retrieval available (small margin).
- ❌ Proprietary API; per-token billing on ingest is the showstopper at our ingestion scale.
- ❌ 3072d triples vector storage cost.

### Voyage `voyage-3-large`

- ✅ Excellent retrieval; 1024d.
- ❌ Same vendor-lock-in problem; ingestion costs at our scale are unattractive.

## More Information

- Re-evaluate annually or when MTEB rankings shift materially. The cost of switching is a full re-embed of all vectors plus a schema migration.
- Phase 13 (cross-encoder rerank, BGE-M3 highlight) depends on this choice. Don't switch dense models without revisiting both.
- See reference research paper §"Mathematical Models of Retrieval and Similarity" for the cosine-similarity-on-bounded-partitions argument that motivates 1024d.
