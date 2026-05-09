# ADR 0001 — Vector database: Milvus

- **Status:** Accepted
- **Date:** 2026-05-09
- **Deciders:** Cameron (sovITxyz)
- **Consulted:** Reference research paper *Architectural Framework for Scalable Multi-Tenant Digital Libraries with Personalized Retrieval-Augmented Generation* (in `docs/`)
- **Informed:** Future contributors

## Context and Problem Statement

sermon.guide needs a vector database that:

1. Hosts billions of vectors across ~4,000 tenants, each owning up to 10,000 books (~10M+ chunks per tenant in the worst case).
2. Supports strict per-tenant isolation enforced at the database layer.
3. Has predictable filtered-query latency (every query is filtered to one tenant's slice).
4. Is operationally feasible for a solo developer at v0 and scales to a small ops team later.
5. Has a permissive open-source license we can self-host.

## Decision Drivers

- Filtered query performance with high cardinality on a metadata field (`tenant_id`).
- Index types available — we want flat indexes available, since filtered subsets are bounded and recall matters.
- Operational maturity (managed offerings, k8s operators, Helm charts).
- License: must allow self-hosting under AGPL-3.0 product without paying a per-instance fee.
- Community + ecosystem (LlamaIndex / LangChain integrations).

## Considered Options

- **Milvus** (Apache 2.0, distributed)
- **Qdrant** (Apache 2.0, Rust)
- **Weaviate** (BSD-3, GraphQL-flavored)
- **pgvector** (PostgreSQL extension)
- **Pinecone** (managed SaaS, proprietary)

## Decision Outcome

**Chosen: Milvus** (standalone for v0, distributed mode later).

### Rationale

- **Flat indexes are first-class.** When every search is pre-filtered to a tenant's bounded subset, an HNSW graph adds overhead without buying recall. Milvus exposes flat, IVF-flat, and HNSW; we start with flat and revisit only if filtered p95 misses the latency budget.
- **Partition keys are first-class.** `tenant_id` as a partition key makes the isolation invariant something we can enforce at the schema level, not just the query level.
- **Throughput on filtered queries** — vendor benchmarks (see reference PDF, p. 5) put Milvus filtering at "Good" with p95 ~11ms in HNSW; flat will be slower per-vector but exact, and our filtered slices are small enough that the constant matters more than the asymptotics.
- **Operational shape** — Milvus has a published k8s operator and a standalone single-binary mode that runs in docker-compose for v0. We can run one infrastructure stack from laptop to production.
- **License** — Apache 2.0. Compatible with our AGPL-3.0 product license; no relicensing concerns.

### Consequences

- We accept Milvus's heavier dependency footprint (etcd + MinIO required for standalone). Phase 1 docker-compose carries this complexity; the upside is one stack from dev to prod.
- We forgo pgvector's appealing simplicity ("just use Postgres"). pgvector's filtering scales worse on the cardinalities we expect (4k tenants × millions of vectors each).
- Pinecone was rejected outright — proprietary, vendor lock-in, and cost projections at our scale (40M+ vectors) are an order of magnitude over self-hosted Milvus.

## Pros and Cons of the Options

### Milvus

- ✅ Flat index support (100% recall, predictable latency).
- ✅ Partition keys for native multi-tenancy.
- ✅ Apache 2.0; runs anywhere.
- ❌ Heavier infra footprint (etcd + MinIO).
- ❌ Steeper operational learning curve than pgvector.

### Qdrant

- ✅ Excellent filtering performance (~3ms p50 in benchmarks).
- ✅ Single Rust binary; tiny footprint.
- ❌ No first-class partition key concept; multi-tenancy via metadata only.
- ❌ Smaller ecosystem of LlamaIndex / LangChain integrations than Milvus.

### Weaviate

- ✅ GraphQL API is pleasant for some workloads.
- ❌ Schema-first model is friction when we want JSON metadata flexibility.
- ❌ Recall benchmarks weaker than Milvus / Qdrant on filtered workloads.

### pgvector

- ✅ "Just Postgres" — operationally trivial.
- ✅ Transactional consistency with the relational schema.
- ❌ HNSW filtering on millions of vectors per query is the documented weak point (see reference PDF p. 5: p95 ~20-30ms is the optimistic case).
- ❌ Index build times scale poorly past ~10M rows.

### Pinecone

- ✅ Zero ops.
- ❌ Proprietary; vendor lock-in.
- ❌ Cost at scale (4k × 10M vectors) is prohibitive vs. self-hosted Milvus.
- ❌ Cannot run in dev without a cloud account.

## More Information

- Open question on partition key (`tenant_id` vs `book_id`) tracked in ARCHITECTURE.md §7.1 and recorded in [ADR 0002](./0002-tenancy-model.md).
- Revisit this decision if filtered p95 latency exceeds 50ms on production-scale data, or if a managed Milvus offering becomes uneconomical relative to alternatives.
