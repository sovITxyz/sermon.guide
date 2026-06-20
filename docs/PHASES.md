# sermon.guide — Phased Build Plan

## Progress

After each phase commit, tick the box and append: completion date, branch name, deviations/follow-ups.

- [x] Phase 0 — Repo skeleton + OSS scaffolding + ARCHITECTURE.md (completed 2026-05-09, branch `phase-0/repo-skeleton`. LICENSE: AGPL-3.0 selected.)
- [x] Phase 1 — Infrastructure (docker-compose) + infra/AGENTS.md (completed 2026-05-09, branch `phase-1/infra-compose`. Modern Compose schema, no top-level `version:`. `make up` brings up Postgres 16, Redis 7, Milvus standalone v2.6.15 + etcd v3.5.25 + MinIO RELEASE.2024-05-28; `--wait` blocks until all healthcheck-gated. Re-up after `make down` measured at ~22s.)
- [x] Phase 2 — Milvus collection bootstrap + Python tooling (completed 2026-05-09, branch `phase-2/milvus-bootstrap`. §7.1 resolved Option B — `book_id` partition; vectors shared globally per book; tenant scoping enforced at API via `book_id IN (user_library)` filter; `tenant_id` field dropped from schema. Worker PostToolUse hook live; Pyright LSP plugin recommended for in-turn type-error feedback.)
- [x] Phase 3 — Tenant isolation smoke test (HARD GATE) + /test-isolation skill (completed 2026-05-11, branch `phase-3/tenant-isolation-test`. Reconciled to §7.1 partition-on-`book_id`: two simulated tenants are two disjoint `book_id` sets, filter is `book_id in [...]` not `tenant_id`. Local gate via `cd worker && make test-isolation`; worker CI skips cleanly when Milvus unreachable (autouse fixture socket-probes port). Mutation test verified — dropping `filter=` produces 2 loud failures with the failure-mode docstring. CI-blocking variant deferred to Phase 11 when `retrieval-golden` job also needs live Milvus.)
- [x] Phase 4 — Format detection + extraction (completed 2026-05-11, branch `phase-4/format-extraction`, PR #9 rebase-merged. EbookLib → pandoc for EPUB, pymupdf4llm for PDF; MIME-sniffed via libmagic, never file extension. Sample-gated end-to-end tests skip cleanly without local copyrighted EPUB/PDF fixtures. System deps `pandoc` + `libmagic1` documented in README and `worker/AGENTS.md`. Upload-side hardening (size limits, libmagic content-vs-claim mismatch) deferred until the ingestion pipeline grows a network edge.)
- [x] Phase 5 — Semantic chunking (completed 2026-05-12, branch `phase-5/semantic-chunking`. No deviations; end-to-end verified locally with the BGE-Large cache prewarmed.)
- [x] Phase 6 — Embedding + Milvus insert + tenant-auditor subagent (completed 2026-05-12, branch `phase-6/embedding-insert`. BGE-Large via `sentence-transformers` in `worker/embedding.py`; `worker/ingest.py` exposes `ingest()` (full pipeline) and `ingest_markdown()` (chunk → embed → insert seam). Tenant scoping per §7.1: vectors carry no `user_id`; `user_library` write deferred to Phase 7. Idempotency: re-ingest of same `book_id` raises `FileExistsError` unless `--force`. Manual e2e verified: 167 rows inserted from the 276K EPUB sample, 39:12 wall clock on 4-core CPU (BGE-Large boundary-detection embeddings are the long pole — GPU swap belongs in Phase 9 once Celery + KEDA land). Phase 3 isolation test re-run clean with real data present. `tenant-auditor` Opus subagent + grep-based `/check-tenant-leak` skill shipped. Local cold-network runs need `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1` — the optional PEFT adapter probe in `sentence-transformers` can transiently fail on flaky DNS; documented in `worker/AGENTS.md`.)
- [x] Phase 7 — Postgres schema + Alembic migrations + schema-reviewer subagent (completed 2026-05-13, branch `phase-7/postgres-schema`. SQLAlchemy 2.0 typed models in `worker/db/models.py` per ARCHITECTURE.md §4 (Users, GlobalBooks, UserLibrary, Highlights, Collections); UUID PKs (client-side `default=uuid.uuid4` — no `pgcrypto` extension dependency), tz-aware `created_at`/`added_at` with `server_default=func.now()`, `user_library.book_id` FK is `ON DELETE RESTRICT` to preserve the dedup invariant when other tenants reference the same `global_books` row. Async engine in `db/session.py`; `DBSettings` (pydantic-settings, `SERMON_POSTGRES_*`) overrides `sqlalchemy.url` from `db/alembic/env.py` so retargeting is env-driven. Initial migration `0001_initial_schema.py` is hand-written (deterministic, no live-PG dependency to bootstrap) and round-trips clean: `make migrate-up` / `make migrate-down REV=base` / `make migrate-up` against the docker-compose Postgres. `schema-reviewer` Opus subagent shipped at `.claude/agents/schema-reviewer.md` with the full invariant checklist (backward compat, locking, enums, JSONB, FKs, downgrade order, tenant scoping) + an explicit "initial migration exception" branch. Verification observation: subagents added inside a session don't appear in the `subagent_type` registry until the next session boots, so the Phase 7 review was run via a general-purpose agent following the `schema-reviewer.md` playbook verbatim — clean, no findings; future Phase-7-touching sessions get the subagent natively. One cosmetic note from the review: `User.email` uses `unique=True` on the model while the migration declares a named `UniqueConstraint("email", name="uq_users_email")` — they produce the same index but a future `alembic revision --autogenerate` may propose a cosmetic rename diff; left as-is for now.)
- [x] Phase 8 — MinHash LSH dedup (completed 2026-05-13, branch `phase-8/minhash-dedup`. `worker/dedup.py` ships `signature(markdown)` over lemmatized 5-shingles (NLTK WordNet) feeding a `MinHash(num_perm=128, seed=1)`, and a `Dedup` class wrapping `datasketch.MinHashLSH` at threshold 0.85. Signatures persist as `LargeBinary` on `global_books.minhash_signature`; the LSH itself lives in memory and is rebuilt from `global_books` on first call within a process (`Dedup._fetch_rows`/`_load_from`). `worker/ingest.py` now writes `global_books` + `user_library` rows on new uploads and short-circuits to a `user_library` upsert on dedup hits (`ON CONFLICT DO NOTHING` against `uq_user_library_user_book`); `--book-id` removed from the CLI — book_id is decided by dedup. Added a sync session factory (`get_sync_session_factory`, `psycopg3`) alongside the async one so worker code avoids `asyncio.run` per call against asyncpg's loop-bound pool. New deps: `datasketch`, `nltk`, `psycopg[binary]`. Phase-8 verify e2e (`test_dedup_roundtrip_across_two_tenants`) passes: ingest under tenant_a creates vectors, same content under tenant_b returns `was_duplicate=True` with no new vectors and a `user_library` row pointing at the same `global_books` row, and a tenant_b filtered Milvus search returns the shared book's vectors per §7.1. Phase 3 isolation suite re-run clean with real data present.)
- [x] Phase 9 — Celery worker (completed 2026-05-14, branch `phase-9/celery-worker`. `worker/celery_app.py` builds the Celery app from `SERMON_REDIS_*` (broker db 0, backend db 1) with `task_acks_late=True` + `task_reject_on_worker_lost=True` + `worker_prefetch_multiplier=1` + `task_track_started=True` + `broker_transport_options.visibility_timeout=300`. `worker/tasks/ingest.py` registers `tasks.ingest.ingest_book` as a thin adapter into `ingest()` — pipeline stays the source of truth, no duplication. New deps: `celery>=5.4,<6`, `redis>=5,<7`. Makefile: `make worker` (long-running prefork), `make enqueue FILE=... TENANT=...` (test producer via `scripts/enqueue_ingest.py`; TENANT accepts UUID *or* a string label that gets uuid5'd + upserted into `users` so the FK resolves — local-dev convenience only, Phase 10 API derives `user_id` from JWT). Verify (sample.pdf, tenant label `tenant_phase9_a`): task picked up, ran the full Phase 6/8 pipeline in 363s wall on 4-core CPU, 27 vectors landed in Milvus with `book_id` partition key set, `global_books` + `user_library` rows persisted, task result `{was_duplicate: False, rows_inserted: 27}` stored in the Redis result backend. Crash-resume path verified by `kill -9` on the `ForkPoolWorker-1` child mid-embed: parent detected `WorkerLostError`, re-delivered the same `task_id`, `ForkPoolWorker-2` spawned, reprocessed within ~10s, completed cleanly. Known Phase 9 cost: pipeline is not crash-safe *between* the Milvus insert and the `global_books` write — a worker death in that window can leave orphan vectors (`library_vectors` rows with no matching `global_books` row). Dedup catches content re-uploads at the MinHash layer so most re-runs converge, but Phase 10+ should add a task-id-keyed idempotency token before opening the queue to untrusted upload traffic. Documented in `worker/celery_app.py` module docstring and `worker/AGENTS.md`.)
- [x] Phase 10 — FastAPI skeleton + JWT auth + upload + api/AGENTS.md (completed 2026-05-19, branch `phase-10/fastapi-auth-upload`. `api/` is a sibling-flat package mirroring `worker/` (uv, ruff strict, pyright strict, `[tool.uv] package = false`); `db` resolves from `../worker` via pyright `extraPaths` + pytest `pythonpath` + `PYTHONPATH=../worker` in `make dev`. Routes: `POST /auth/signup` (201, 409 on email collision), `POST /auth/login` (single 401 for missing-user vs wrong-password — email-enumeration defense), `POST /upload` (202, multipart streams to disk with `upload_max_bytes` cap and per-upload UUID subdir; filename sanitized of `\\` `/` and unsafe chars before `Path.name`), `GET /tasks/{task_id}` (Celery `AsyncResult` status + result payload on `SUCCESS`), `GET /healthz`. `tasks_client.py` is a thin Celery client against the same Redis broker/backend as `worker/celery_app.py` — `send_task("tasks.ingest.ingest_book")` by name so the api venv never pulls pymilvus/sentence-transformers/pandoc. Verified e2e against docker-compose Postgres+Redis: signed up alice + bob, alice login → JWT, upload of `sample.pdf` → task enqueued and stored at `/tmp/sermon-uploads/<upload_id>/sample.pdf`, polling `/tasks/{id}` observed `PENDING → STARTED → SUCCESS` with `{book_id, was_duplicate: false, rows_inserted: 27}` payload after Celery worker drained it; bad-password / unknown-email / missing-JWT / bogus-JWT all return 401. `tenant-auditor` clean on the api/ surface; `/security-review` flagged the Phase 9 orphan-vector window and the missing `upload_tasks(task_id, user_id)` ownership table — both deferred to Phase 11 and documented in `api/AGENTS.md`. The `.claude/settings.json` PostToolUse hook flip for `api/**/*.py` is the one Phase 10 todo not landed in this PR — auto-mode classifier blocked the edit; a follow-up commit (or operator edit) replaces the placeholder echo with `uv run --project api ruff check --fix && uv run --project api pyright`.)
- [x] Phase 11 — Vector search endpoint + golden-test infrastructure (completed 2026-05-19, branch `phase-11/vector-search`. `api/search.py` exposes `POST /search` → embed query (BGE-Large via `worker.embedding.embed` — shared loader, one model per process via `@lru_cache`) → Milvus COSINE search filtered by `book_id IN (user_library for JWT user)` per ARCHITECTURE.md §3 + §7.1 → top-K of `{book_id, content_chunk, metadata, score}`. JWT-derived `user_id` only (CLAUDE.md tenant rule); no `user_id`/`book_ids` fields on `SearchRequest` so a client cannot widen its scope. The blocking BGE encode + Milvus RTT are handed to `asyncio.to_thread` so the async handler doesn't stall the event loop. Module-level Milvus client (lazily constructed via `make_client()` from worker/) reused across requests. Score is surfaced as `score` not `distance` — for COSINE the metric value is similarity in `[-1, 1]`; calling it `distance` would mislead callers. Empty-library short-circuits to `hits=[]` *before* embedding so an unbound user doesn't pay model load and we never emit `book_id in []` (some pymilvus builds reject it). API deps grew to include `pymilvus`, `sentence-transformers`, `torch` (CPU-only via the same `[tool.uv.sources]` override worker/ uses), and `numpy` — pinned in lockstep with `worker/pyproject.toml`; `api/AGENTS.md` updated to clarify the new boundary (api/ now imports `embedding` + `bootstrap_milvus` from worker/, still must not import `worker.celery_app` / `worker.tasks.*` / `worker.ingest` / `worker.chunking` / `worker.dedup` / `worker.extractors`). Golden-test infra: `worker/tests/golden/queries.jsonl` ships 6 hand-curated rows over the local sample corpus (Lewis trilemma → Mere Christianity; apologetics → Lewis or `10 Answers for Atheists`; eschatology → Alcorn `Heaven`; mind-renewal → Groeschel `Winning the War in Your Mind`; two 1-Thess queries → `sample.pdf`/`1 Thess.PDF` which dedup to one book_id) — referenced filenames track *local* names exactly (a "Christiany" typo in the Lewis sample is documented in the row's `note`). `worker/tests/test_retrieval_golden.py:TestRetrievalAccuracy` parametrizes one test per row; each embeds the query with BGE-Large, runs a Milvus COSINE search over the golden user's library (all corpus book_ids — ranking quality, not isolation, is the gate here), and asserts at least one expected `book_id` in top-10 with `distance >= min_score` (hit/miss binary, no fuzzy partial credit). Session-scoped fixture ingests via `worker.ingest:ingest` under a deterministic `uuid5` user — Phase 8 dedup short-circuits every re-ingest so subsequent sessions resolve filename→book_id in seconds; corpus persists across runs intentionally (one user_id footprint, manual delete clears it). Five independent skip gates (queries.jsonl absent, sample(s) missing, Milvus unreachable, Postgres unreachable, model absent, WordNet absent, collection missing) each fire their own `pytest.skip` so CI logs distinguish missing-corpus from missing-infra. CI `retrieval-golden` job was wired speculatively in Phase 0 gated on `worker/tests/golden/queries.jsonl` — Phase 11's JSONL drop activates it; without Milvus+corpus in CI every row skips cleanly (matches Phase 3's CI-deferred pattern). `make test-retrieval-golden` is the local enforcement target. Verify: live API e2e — signed up `search_a` and `search_b`, bound the existing `sample` book (1-Thess content from earlier phases) to `search_a` only via direct `user_library` insert, then `POST /search` with `"the day of the Lord coming like a thief in the night"`. As `search_a`: top-3 hits all from `sample.pdf` at scores 0.766 / 0.678 / 0.653, top hit literally the "LIKE A THIEF IN THE NIGHT (1 Th 5:1-11)" section. As `search_b` (empty library): `hits=[]`. No-auth: 401 "Could not validate credentials." Phase 3 isolation test re-run clean (3/3 pass) with the new Phase 11 vectors in place. Mutation proof against the same Milvus filter: real BGE-Large query embedding → top-3 scores `[0.766, 0.678, 0.653]` (all above 0.45 floor, goldens pass); seeded random unit vector → top-3 scores `[0.016, 0.016, 0.010]` (all below 0.45 floor by ~40×, goldens fail loudly). Full corpus cold-ingest + 6 golden queries: **7/7 PASSED in 1:50:04 wall** (Mere Christianity + 10 Answers + Heaven + Winning the War + sample.pdf dedup-hit on the existing row; SemanticSplitter sentence-level embeddings dominate the wall-clock, BGE-Large chunk embeddings second). Tenant audit (`/check-tenant-leak` + `tenant-auditor` subagent): the only new query-paths introduced are `api/search.py:_run_milvus_search` (filter built from JWT-derived `book_id` set) and the `user_library` resolve (`where(UserLibraryEntry.user_id == current_user.user_id)`) — no widening surface. Deferred to a later phase: an `upload_tasks(task_id, user_id)` ownership table (Phase 10 row noted this; Phase 11 doesn't touch upload tasks so the gap stays) and a library-size cap on the search filter (user with 10K books generates a ~360KB filter expression — Milvus 2.6 accepts it but Phase 12+ should partition).
- [x] Phase 12 — Hybrid search (BM25 + RRF) (completed 2026-05-21, branch `phase-12/hybrid-search`. BM25 backend = Postgres `tsvector` on a new `chunks` table — ADR 0004 captures the decision against Milvus 2.6 sparse / Elasticsearch / ParadeDB `pg_search`; "BM25" is shorthand for `ts_rank_cd` cover-density (literally BM25 is the next-tier upgrade path). New migration `0002_chunks_bm25.py` (hand-written): `chunks(chunk_id, book_id, chunk_index, content, parent_section, filename, tsv GENERATED, created_at)` + B-tree on `book_id` + GIN on `tsv` + `uq_chunks_book_chunk(book_id, chunk_index)` + FK to `global_books` ON DELETE CASCADE; round-trips clean (`make migrate-down REV=0001` / `make migrate-up`). `worker/ingest.py` now writes `chunks` rows alongside `global_books` in *one* transaction (`_insert_book_with_chunks`) — splitting the writes would create a worse failure mode than the existing Milvus-orphan window, since a `global_books` row without chunks would survive dedup and be invisible to BM25 forever. `worker/scripts/backfill_chunks.py` rehydrates the chunks table for pre-Phase-12 books by paging through Milvus partitions (idempotent via `ON CONFLICT DO NOTHING` against the uniqueness constraint); used to backfill the existing 5-book Phase-11 corpus — 614 chunk rows landed in one pass. Algorithm lives in `worker/retrieval.py`: `dense_search` (sync Milvus COSINE), `bm25_search` (async Postgres) + `bm25_search_sync` for worker tests, `rrf_fuse(dense, sparse, *, limit, k=60)` over `(book_id, chunk_index)` identity. `_BM25_SQL` is a single parameterized `text()` clause with `bindparam("book_ids", type_=ARRAY(UUID(as_uuid=True)))` and `websearch_to_tsquery('english', :query)` so user-typed queries flow safely into the index without SQL/tsquery injection surface. `api/search.py` rewritten as a thin async wrapper: resolves the JWT user's `book_id` set from `user_library`, short-circuits empty libraries, then runs the dense arm (`asyncio.to_thread` around embed + Milvus) and the sparse arm (async `session.execute`) concurrently via `asyncio.gather`, fuses via RRF, and returns top-K. `SearchHit.score` semantics changed: Phase 11 surfaced COSINE; Phase 12 surfaces the RRF score (sum of `1/(60+rank)`). Per-arm `dense_score` / `sparse_score` are preserved on the internal `RetrievalHit` for debugging but kept out of the public schema. `api/AGENTS.md`: `worker.retrieval` added to the allowed cross-package import surface (4 modules now: `db`, `embedding`, `scripts.bootstrap_milvus`, `retrieval`); new explicit BM25-side tenant rule (`book_id = ANY(<set>)` mirroring the Milvus `book_id IN (...)` invariant). Golden test (`worker/tests/test_retrieval_golden.py`) rebuilt around `hybrid_search_sync`; `min_score` semantics shifted to **per-arm score floor against whichever arm contributed** so the 6 existing dense-strength rows keep their `0.45` COSINE floor, and the 2 new BM25-strength rows ("Groeschel", "stronghold of the mind") use `min_score: 0.0` because pinning the ts_rank_cd scale across corpus changes isn't worth the brittleness. New skip-gate: golden bails cleanly if any corpus book is missing `chunks` rows, pointing the operator at `make backfill-chunks`. Unit tests for `_build_milvus_filter` + 4 new `rrf_fuse` tests in `api/tests/test_search_unit.py` (pin the fusion math without infra). Verify: `make test-isolation` 3/3 PASSED post-backfill; `make test-retrieval-golden` **9/9 PASSED in 11:25 wall** (6 dense + 2 BM25 + 1 dim sanity); live API e2e — signed up `phase12-a` + `phase12-b`, bound user_a to the 5-book corpus, ran (a) full Lewis trilemma sentence as user_a → top-3 all Mere Christianity at RRF 0.0164/0.0161/0.0159 (dense arm carrying), (b) bare token `"Groeschel"` as user_a → top-5 all Groeschel book at RRF 0.0320/0.0311/0.0303/0.0295/0.0276 (sparse arm carrying), (c) `"Groeschel"` as user_b (empty library) → `hits=[]`, (d) tenant widening attempt — `"user_id": "<user_a>", "book_ids": ["<user_a's book>"]` injected into request body as user_b — still `hits=[]`, JWT-derived `user_id` wins, Pydantic silently dropped the extras (the `tenant-auditor`/`/check-tenant-leak` rule "user_id field is an automatic reject" remains a reviewer-enforced invariant, not a Pydantic-`extra='forbid'` one; deferred). Tenant audit clean (mechanical + opus subagent walk-through, both pass — full BM25 SQL parameterization, no body-sourced user_id/book_id, both arms scope to JWT-derived `user_library`). Library-size cap on the search filter still deferred from Phase 11; Phase 12 added a second per-query Postgres roundtrip but Milvus + Postgres still accept the IN-list at v0 scale.

  Pre-merge audit (2026-05-22): retrieval-layer latency measured at dense p50 2.15s vs hybrid p50 2.17s — BGE-Large encode dominates (2.14s, 99.6% of wall time) and the BM25 arm is 8ms/29ms p50/p95, so the `asyncio.gather` parallelism saves no wall time at v0 because the arms have wildly different costs (the "doubled per-query work" framing is true in flops but not in wall-clock). The dense-vs-hybrid contrast experiment also found that spec verify #1 ("dense misses → BM25 catches") overstates what this corpus shows: BGE-Large surfaces both BM25-strength golden queries ("Groeschel", "stronghold of the mind") cleanly on its own at COSINE 0.60+, so the goldens for those rows pass because **both** arms find the book, not because BM25 rescues a dense miss. BM25's actual demonstrated value on this corpus is **corpus-presence filtering** — queries with no lexical match (e.g. "Theodore Roosevelt") return zero from BM25 while dense returns false-positives at 0.5–0.6 COSINE; RRF cannot suppress those because there is no sparse signal to compete with. Failure modes (no graceful degradation by design): one-arm-down ⇒ 500; Milvus-down returns 500 in 12.2s (pymilvus internal retry timeout — a single blip becomes long-tail request latency); Postgres-down returns 500 in 30ms (dies at JWT validation pre-search). `asyncio.gather` lacks `return_exceptions=True` — flagged for a future ops-resilience pass. Test-debris hygiene surfaced by the backfill rerun (none tenant-reachable; predates Phase 12): 1 orphan `global_books` row (`_test_phase8_synthetic`, 0 chunks, 0 Milvus vectors — Phase 8 dedup test didn't clean up) + 3 orphan Milvus book_ids (`b_mere_christianity`, `b_1_thess`, `88ba2fe2…` from Phase 3/8 fixtures). `/security-review` clean; `make test-isolation` 3/3 re-verified post-restart.)
- [x] Phase 13 — Cross-encoder rerank + semantic highlighting (completed 2026-05-22, branch `phase-13/rerank-highlight`. `api/rerank.py` ships a `cross-encoder/ms-marco-MiniLM-L-6-v2` reranker (~90 MB, lazy `@lru_cache` like `worker.embedding._model`) that takes a `Sequence[RetrievalHit]` from the Phase 12 hybrid arm and reorders top-30 → top-N by cross-encoder relevance score. `api/highlight.py` ships a `BAAI/bge-m3` dense-head highlighter (~2.3 GB, same lazy singleton pattern) that splits each retained chunk into sentences via regex (`(?<=[.!?])\s+(?=[A-Z\"'“‘])` — see module docstring on tradeoffs vs `pysbd` / NLTK `punkt_tab`), scores sentences against the query in one batched encode, and drops sentences below threshold 0.5 (architecture-locked). `api/search.py` rewired: dense + sparse → `asyncio.gather` → `rrf_fuse(limit=RERANK_FANOUT if rerank else payload.limit)` → if rerank=true, `await asyncio.to_thread(rerank)` → `await asyncio.to_thread(highlight)`. New `rerank: bool = True` payload field — default true so the /search pipeline matches ARCHITECTURE.md §5 canonical lifecycle (hybrid → rerank → highlight) and Phase 14's `/search-summary` will get pre-pruned context without a flag; set false to compare against raw RRF for debugging. `SearchHit.score` semantics shifted again (Phase 11 COSINE → Phase 12 RRF → Phase 13 cross-encoder relevance when rerank=true, RRF when false); previous-stage scores survive on `metadata["rrf_score"]` (added by reranker) and `metadata["sentences_kept" / "sentences_total"]` (added by highlighter). Per-arm `dense_score` / `sparse_score` remain on the internal `RetrievalHit` but are not in the public schema. No new pyproject deps — both new classes (`CrossEncoder`, `SentenceTransformer`) are exported by the existing `sentence-transformers>=3` install; `api/AGENTS.md` carries the model surface (a table of three models + ~3.7 GB cold-load cost, plus a new Open Trust Gap noting the missing graceful degradation if any of the three models fail) and confirms the new modules are post-retrieval (no DB/Milvus surface). 19 new unit tests (api/tests/test_rerank_unit.py + test_highlight_unit.py) pin the deterministic glue with a monkeypatched `_model`: empty-input short-circuits (model is never loaded), top-N truncation, stable tiebreak on equal scores, RRF→metadata preservation, threshold inclusivity, whole-chunk-pruned hits dropped from output, unsplittable-chunk passthrough, sentence order preservation, prune-ratio metadata, existing-metadata-key preservation. Verify: `make test-isolation` 3/3 PASSED post-Phase-13 (load-bearing — Phase 3 hard gate); `make test-retrieval-golden` 9/9 PASSED in 4:42 wall (faster than Phase 12's 11:25 cold-ingest because the 5-book corpus is already in Milvus + chunks + Postgres); 34/34 api unit tests PASSED (19 new + 15 existing). The golden harness exercises the Phase 12 hybrid path (`hybrid_search_sync`), **not** rerank/highlight, so "reranking should improve hit rate, not degrade it" was checked separately: all 8 golden queries were re-run through the live rerank=true /search pipeline as phase12-a (owns the full corpus) — **8/8 still surface the expected book, and the expected book is rank 1 for 7 of 8 (rank 2 for the trilemma)**, i.e. rerank *tightens* precision rather than dropping recall. Result counts shrink under highlight pruning (trilemma 9, Groeschel 3, the rest 8–10) but no expected book is pruned out; the earlier "grace → 1 hit" collapse was specific to weak-match queries, not the strong-match goldens. Live API e2e against `phase12-a@example.com` (5-book corpus): (a) Lewis trilemma paraphrase — rerank=false returned top-3 [Mere Christianity 15407c, Mere Christianity 2220c, Groeschel 216c (false positive)] at RRF 0.0287/0.0164/0.0161; rerank=true returned top-3 [Mere Christianity 110c / 497c / 281c, all kept ratios 1/9, 9/186, 4/20] at cross-encoder relevance -3.37/-3.63/-3.73. Token reduction 17843 → 888 chars = **95% drop** (well past the 70–80% architecture target). The Groeschel false-positive at #3 was demoted out of the top-K and replaced with on-topic Mere Christianity chunks. (b) Theodore Roosevelt no-corpus-match query — rerank=false returned 5 garbage hits (copyright boilerplate, "ed." (4 chars), HTML frontmatter, tangential Alexander the Great mention, apologetics references) at RRF 0.0154–0.0164; rerank=true returned **0 hits** (cross-encoder scored every chunk so low that BGE-M3 highlighting then pruned every sentence below 0.5). This is the audit-flagged "dense FPs at 0.5–0.6 COSINE that RRF cannot suppress" failure mode from Phase 12; rerank + highlight is the architectural fix and it works as designed. (c) BM25-strength query "Groeschel" — rerank=true returned 2 hits, both correctly from the Groeschel book at cross-encoder relevance 4.30 / 1.55. (d) Tenant widening attempt — phase12-b (empty library) injected `user_id` + `book_ids` fields targeting phase12-a's data; returned `hits=[]`, JWT-derived user_id wins, Pydantic silently dropped extras. Tenant audit clean (`tenant-auditor` subagent walkthrough + isolation test re-verified inside the audit); `/security-review` clean (no HIGH or MEDIUM findings). Latency cost: warm /search with rerank=true is ~30 s vs ~1 s for rerank=false on CPU — BGE-M3 sentence scoring dominates (10 chunks × ~30 sentences). Acceptable for sermon prep (user is reading, not chatting); architecture-locked GPU swap is the path forward. Phase 12 carry-forwards revisited: (i) `asyncio.gather` graceful-degradation still deferred — Phase 13 adds two more sequential model calls that also raise to 500 on failure; same fail-loud posture, documented in the new "Open trust gap" row. (ii) Orphan rows (`13b3bd17` + 3 orphan Milvus book_ids) — not tenant-reachable; left as-is, no Phase 13 reason to touch them. (iii) The "Theodore Roosevelt" scenario explicitly tested — rerank+highlight is the answer, see verify (b) above.)
- [x] Phase 14 — Gemini 1.5 Flash summary agent (completed 2026-05-23, branch `phase-14/summary-agent`. New `api/summary.py` ships `POST /search-summary` → `{query, limit_chunks=20}`. Dep added: `google-genai` 1.75.0 (network SDK, no in-process model — so unlike the three sentence-transformers models there is no api↔worker pin-lockstep concern). Retrieval is **reused, not re-implemented**: the Phase 13 `search()` handler body was extracted into `search.run_search(*, query, limit, do_rerank, user_id, session) -> list[SearchHit]` (boolean param named `do_rerank` to avoid shadowing the imported `rerank` fn), and both `/search` and `/search-summary` delegate to it — behavior byte-identical, confirmed by the unchanged retrieval goldens + isolation suite. `/search-summary` calls it with `do_rerank=True` so Gemini receives the full hybrid → RRF → cross-encoder → BGE-M3-pruned context (ARCHITECTURE.md §5). Citation markers are `[book:chunk]`: `book` = `global_books.title` with `[`/`]`/`:` stripped + whitespace collapsed (filename-stem then `book-<id8>` fallbacks), de-collided with `(2)`/`(3)` suffixes so every `(book_id, chunk_index)` maps to exactly one marker; `chunk` = `chunk_index`. Title lookup is a new shared-table query (`select(GlobalBook.book_id, GlobalBook.title).where(book_id.in_(ids))`) keyed only by already-tenant-filtered book_ids — no new tenant surface (`global_books` is shared-by-design, §3/§4; a title is not user-scoped). Grounding system instruction enforces "1–2 paragraphs, cite [book:chunk] inline, use only provided context, say so if it doesn't answer"; temperature 0.2, max_output_tokens 768. Response `{summary, citations}` where `citations` is the subset of source markers that actually appear in the summary text (first-appearance order, de-duped) — invented/paraphrased markers never resolve, so no hallucinated citation is ever returned. Hallucination guard is two-layer: (1) deterministic — empty retrieval (empty library, or every chunk pruned below the highlight threshold for an off-corpus query, e.g. the Phase 12/13 "Theodore Roosevelt" case) returns a fixed "nothing found" message with `citations=[]` and **no LLM call**; (2) instructed — the grounding prompt for weak-but-nonempty context. `GOOGLE_API_KEY` is read *unprefixed* via `Field(validation_alias="GOOGLE_API_KEY")` in `settings.py` (verified empirically that the alias bypasses the `SERMON_API_` prefix); a missing key → 503 **before** the ~30s rerank, not after. The Gemini client is a lazy `@lru_cache` (no key/network at import/lint/test, mirroring the model loaders); `errors.APIError` or an empty candidate → 502; `GEMINI_MODEL = "gemini-1.5-flash"` is the single swap point. `infra/.env.example` documents the key (unprefixed, with rationale); `.claude/settings.json` deny list left **unchanged** — `Read(.env)` + `Read(.env.*)` already cover it (which is also why that template can only be edited by append, not the Read/Edit tools). 22 new unit tests (`api/tests/test_summary_unit.py`, every I/O seam monkeypatched — no key/network/DB/model): marker sanitization + collision de-dup + fallbacks, source ordering, prompt assembly, citation extraction (present-only, first-appearance order, ignores unknown markers, `[X:1]` not matched inside `[X:12]`), Gemini config wiring, 502 on API-error / empty candidate, handler 503-before-retrieval, no-context-no-LLM short-circuit, and a happy path asserting `do_rerank=True` + the JWT `user_id` are passed straight through. Verify: 56/56 api unit tests PASS (34 existing + 22 new), ruff + pyright(strict) clean; `make test-isolation` 3/3 PASS (Phase 3 hard gate, Milvus live); `/check-tenant-leak` grep sweep clean (the request-sourced-id check is empty; the only new query is the shared `global_books` lookup); `tenant-auditor` subagent — no findings; `/security-review` — no findings. **Deviation:** the phase's *live* Verify (real "what does this say about faith" query → grounded output with mapping citations; nothing-in-corpus → no confabulation) was **not run end-to-end** — no `GOOGLE_API_KEY` is set in this environment, so the Gemini round-trip is exercised only by mocked unit tests. The retrieval half is covered live by the Phase 13 goldens + isolation suite; the LLM half needs a key before this can be ticked as live-verified (live verify closed by Phase 14b). Latency: warm E2E ≈ ~30 s reranked retrieval (CPU, per the §1 `<1s` target / api/AGENTS.md open gap) + the Gemini round-trip; the architecture-locked GPU swap is the path forward.)
- [x] Phase 15 — Next.js: auth + library + web/AGENTS.md (completed 2026-06-03, branch `phase-15/web-auth-library`. Stack: Next.js 15.5 App Router, React 19, TypeScript strict (+ `noUncheckedIndexedAccess`, `noImplicitOverride`, `exactOptionalPropertyTypes`), Tailwind v3, Biome, pnpm 9, Vitest. `web/` is fully independent — talks to `api/` over HTTP only, zero Python imports. **Auth model:** the JWT is stored in an **HttpOnly + SameSite=Lax** cookie set by Next route handlers (`app/api/auth/{signup,login,logout}/route.ts`) and never reaches client JS (verified live: the cookie jar marks it `#HttpOnly_`). Every authenticated API call is proxied **server-side** — client components only ever hit same-origin `/api/*` route handlers (login/logout/upload/tasks), and the library is fetched in a server component via `lib/api-server.ts:getLibrary` — with the bearer attached from the cookie. `API_BASE_URL` is server-only (no `NEXT_PUBLIC_`); `lib/config.ts` + `lib/api-server.ts` carry `import "server-only"` so neither the origin nor the token can be inlined into a client bundle. `middleware.ts` is a **presence-only** gate over `/library` + `/upload`; real authorization is the API's 401 → `UnauthenticatedError` → redirect to `/login`. Open-redirect guard `lib/validation.ts:safeRedirectPath` rejects protocol-relative (`//evil`) and backslash (`/\evil`) targets, unit-tested. **Pages:** `/signup`, `/login` (honors `?next=` + `?registered=1`), `/library` (server component → table), `/upload` (drag-drop, optimistic pending row, polls `/api/tasks/{id}`, dedup-aware status labels). Root layout nav reflects auth state + logout. **Cross-package addition:** the library page needed a backend listing that did not exist, so this phase also adds `GET /library` to `api/` (`api/library.py`) — tenant-scoped `user_library ⋈ global_books` by JWT-derived `user_id`, mirroring the audited `search.py` resolve; the `_library_stmt` builder is pinned by a no-DB unit test (`api/tests/test_library_unit.py`) and the path added to `test_smoke.py`. `api/AGENTS.md` unchanged (only new cross-import is `db`, already allowed; route is `CurrentUserDep`-protected). **Tests:** 25 Vitest unit tests over pure helpers (cookie policy, email/password validation, Celery→UI status mapping, redirect guard) — components are covered by the live verify, not jsdom, to keep the dep surface small; 3 api unit tests for the library statement. **CI:** the pre-wired `web` job activates on `web/package.json` (pnpm 9 `--frozen-lockfile` → `tsc --noEmit` → `biome check` → `vitest run`); **no `packageManager` field** in `package.json` (it conflicts with `pnpm/action-setup@v6`'s `version: 9` pin). `next-env.d.ts` is committed at **two** references on purpose — CI runs bare `tsc` without a build, so the `/// <reference path="./.next/types/routes.d.ts" />` line that `next dev`/`next build` appends is intentionally not committed (tsconfig globs `.next/types/**/*.ts` instead, so typed routes load locally and are absent in CI). **Verify:** `pnpm tsc --noEmit` + `pnpm biome check` + `pnpm vitest run` all clean — also re-run with `.next/` removed to mirror a fresh CI checkout; `next build` compiles all 12 routes + middleware. Live e2e against the running stack (Postgres/Redis/Milvus/MinIO healthy; api via `make dev`; web dev server bound `:3001` because an unrelated service held `:3000`): signup→**201**, login→**200** with HttpOnly `sg_session`, `/library` page with cookie→**200** (empty-state), without cookie→**307** `/login?next=%2Flibrary`, upload sample EPUB→**202** `{task_id,…}` (filename sanitized to `10_Answers_for_Atheists.epub`), task poll with cookie→**200** `{PENDING}`, task/upload without cookie→**401**. Direct API check: new user `GET /library`→`{"books":[]}`, no-auth→401. `make test-isolation` 3/3 PASS (Milvus live). `tenant-auditor` subagent — no findings; `/security-review` — no findings (reviewer fuzzed the redirect guard across six bypass variants). **Deviations / follow-ups:** (i) The `web` PostToolUse hook in `.claude/settings.json` was activated after explicit user authorization — the auto-mode classifier first blocked the edit as self-modification of agent startup config (the same block Phase 10 hit for its api hook), then allowed it once the user authorized this specific change. Final command: `cd "${file%%/web/*}/web" && pnpm tsc --noEmit && pnpm biome check "$file"` on `*/web/*.{ts,tsx}` edits — the web dir is derived from the edited file's absolute path because the hook's cwd is not the repo root (an initial `cd web` failed). Verified live: a deliberate `TS2322` probe made the hook block, and a clean tree passes. (ii) The full upload→ingest→**done** status flip was not waited out live — no Celery worker was running and CPU BGE-Large ingest is tens of minutes (unchanged Phase 9/11 behavior); the enqueue + task-status proxy are verified, the ingest itself is the same worker code. (iii) Tailwind v3 (not v4) chosen for setup stability.)
- [x] Phase 14b — OpenAI-compatible LLM transport (ppq.ai) + Phase 14 live verify (completed 2026-06-04, branch `phase-14b/ppq-llm-transport`, closes issue #24. **ADR 0005** locks the transport: `google-genai` out, `openai` SDK (2.41.0, pinned `>=2,<3`) in as a single config-driven chat-completions transport over OpenAI-compatible endpoints — forced by the industry-wide `gemini-1.5-flash` retirement (Phase 14's pin was dead regardless of transport) and by ppq.ai being OpenAI-shaped (google-genai cannot speak to it); Google-direct stays reachable through its own compat endpoint, so prod runs the **same code path** the ppq live-verify exercises. `summary.py:_PROVIDERS` is the single source of truth: `google` (default) → `https://generativelanguage.googleapis.com/v1beta/openai/` + `gemini-2.5-flash` + `GOOGLE_API_KEY`; `ppq` → `https://api.ppq.ai/v1` + `google/gemini-2.5-flash` + `PPQ_API_KEY` (ids pinned, not alias-tracking — `gemini-flash-latest` drifts; catalog pre-check `curl -s https://api.ppq.ai/v1/models` confirmed the pinned id). `settings.py` adds `llm_provider` (SERMON_API_LLM_PROVIDER, Literal), `llm_model` override (SERMON_API_LLM_MODEL — spell it the active provider's way: bare on google, `google/`-prefixed on ppq), and `ppq_api_key` via unprefixed `PPQ_API_KEY` alias (same pattern + rationale as GOOGLE_API_KEY); `infra/.env.example` documents all three (append-only — deny rules). `_client()` is a lazy `@lru_cache` `openai.OpenAI(base_url=…, api_key=…)`; `_generate_summary` → `chat.completions.create(model, [system,user], temperature=0.2, max_tokens=768)`; `openai.APIError` → 502, empty choices/content → 502 (gateway-200-with-zero-choices pinned as 502-not-IndexError); the 503-before-retrieval guard keys on the **active** provider's key and names the missing env var. Grounding prompt, `[book:chunk]` citation contract, and the no-context short-circuit unchanged; `api/AGENTS.md` notes the LLM is a network call (no in-process model, no api↔worker pin-lockstep). Tests: the 22 Phase 14 units re-seamed to the chat.completions shape preserving every pinned behavior, + 8 provider-resolution pins (default=google; `_PROVIDERS` cells; ppq flip builds the real client against api.ppq.ai/v1 with PPQ_API_KEY + the `google/` model spelling; `llm_model` override wins; per-provider 503 detail; no silent key cross-pairing) — 67/67 api suite, ruff + format + pyright(strict) clean. Hook aside (own commit): the api/worker PostToolUse hooks now run `ruff format` after `check --fix` — the gap behind Phase 14's format-only CI failure; the auto-mode classifier blocked the settings.json self-edit as predicted and it was applied after explicit operator authorization. **LIVE verify (ppq.ai, `google/gemini-2.5-flash`, ~6¢):** (1) Grounded — as `phase12-a` (5-book corpus), "what does this say about faith" → 200 in 146.9 s cold / ~134 s warm ×2; two coherent paragraphs with inline `[book:chunk]` markers; 4–5 returned citations per run, all resolving to real corpus chunks (Mere Christianity 82/85/114, 10 Answers for Atheists 23/51/70/71 across runs); citations ⊆ prompt sources. **Live contract finding for Phase 16:** the model sometimes merges adjacent citations into one bracket (`[A:70, A:51]`) — the conservative extractor drops merged-only members while standalone appearances resolve, so returned citations stay 100% resolvable (no fakes, the documented v0 trade-off), but the UI must expect comma-merged inline brackets that render as plain text. (2) No-confabulation — "who was Theodore Roosevelt" → 200 with the byte-exact fixed no-context message + `citations=[]` (235 s; rerank+highlight prunes all 30 fan-out chunks before the short-circuit). The NO-LLM-call proof ran as a control triad because the spec's literal "key unset behaves identically" is impossible under the (correct, unit-pinned) 503-before-retrieval guard: unset-key control → 503 "set PPQ_API_KEY" in 0.095 s pre-retrieval; invalid-key control → **byte-identical** no-context 200 on Roosevelt (an attempted call would 502); invalid-key + faith → 502 "Summary generation failed upstream." (proves the control catches real calls and live-verifies the APIError→502 map). Latency (api/AGENTS.md row updated): warm `/search-summary` E2E ≈ 134 s = ~71–76 s retrieval/rerank/highlight on this box + **~58–64 s LLM round-trip** (gemini-2.5-flash runs thinking by default through the compat layer). Gates: `make test-isolation` 3/3 PASS; `/check-tenant-leak` sweep clean; `tenant-auditor` PASS ×4 (transport adds no query surface; key flows env→Authorization header only); `/security-review` — no findings. **Deviations:** (i) the `google` arm is **config-verified only** — no GOOGLE_API_KEY exists in this env; the transport code is identical either way (same client construction, same call, different `_PROVIDERS` row), so the residual google-arm risk is configuration-shaped, not code-shaped. (ii) the no-LLM-call control deviated from the spec's literal "unset" wording as above — replaced with the strictly stronger invalid-key triad. (iii) `phase12-a`'s dev password was reset via a direct `users.password_hash` UPDATE to run the scenarios (Phase 12 never persisted creds — correctly).)
- [x] Phase 16 — Next.js: search + summary UI (completed 2026-06-05, branch `phase-16/web-search`. **v0 done.** `/search` page = server-component shell (`app/search/page.tsx`) + `"use client"` `SearchPanel`: query input → POST same-origin `/api/search-summary` → summary panel with inline `[n]` chips anchor-linked to citation cards (title, cleaned section, chunk index, line-clamped chunk preview with Show more/less) → loading/empty/error states. Route added to the presence-gate middleware matcher and the authed nav. **Long-request UX** (Phase 14b carry-forward): the proxy (`app/api/search-summary/route.ts`) holds the upstream call with an explicit `AbortSignal.timeout(300s)` → 504 (first timeout convention in web/ — documented in `web/AGENTS.md` "Long-running proxies"), and the UI shows an elapsed `m:ss` ticker + expectation-setting copy instead of a bare spinner. The proxy forwards **only** `{query}` — `limit_chunks`/`user_id`/`book_ids` are structurally dropped, so the client cannot widen scope or fan-out. **Marker rendering** (Phase 14b carry-forward): `lib/summary.ts:segmentSummary` resolves only the exact standalone markers the API returned; comma-merged brackets and invented markers stay plain prose — and the merged case **occurred live** (`[10 Answers for Atheists:70, 10 Answers for Atheists:51]` in the verify summary, rendered as text while `[…:51]` standalone resolved to a card). **Cross-package addition** (Phase 15 precedent): `Citation.content` added to `api/summary.py` — the citation cards need a chunk preview and the text was already in `_Source.content` (the exact tenant-filtered passage the LLM saw), so no new tenant surface and no second round-trip; pinned in `test_summary_unit.py`. **Live verify** (cookie-jar drive of the real dev stack — api `make dev` :8000, web `pnpm dev` :3001, infra healthy; as `phase12-a` / 5-book corpus): no-cookie `/search` → 307 `/login?next=%2Fsearch`; no-cookie POST → 401; login → HttpOnly `sg_session`; page 200 with form + nav link; empty/malformed-body proxy validation → 400. Grounded path: **"what does this say about faith" → 200 in 138.9s warm** (matches Phase 14b ~134s), 2-paragraph summary, 3 citations (Mere Christianity 82/85, 10 Answers 51) all markers resolving, all carrying content (157–3352 chars) for previews. Empty-library: fresh user `phase16-empty@example.com` → byte-exact no-context message + `citations=[]` in **0.20s** (pre-LLM short-circuit). Gates: web `tsc`/`biome`/`vitest` 42/42 clean (28 new tests; re-run with `.next/` removed; `next build` compiles all 13 routes + middleware); api 67/67 + ruff/format/pyright(strict) clean; `make test-isolation` 3/3 PASS ×2; `/check-tenant-leak` sweep clean (no new query surface in the diff); `tenant-auditor` PASS (content provenance traced end-to-end to the JWT-scoped `user_library` filter); `/security-review` — no findings (LLM/EPUB-derived text reaches the DOM only as React-escaped JSX text nodes; no `dangerouslySetInnerHTML`). **Deviations:** (i) the spec's verify query "what does this say about grace" returns the no-context message on this corpus — the Phase 13 "grace → 1 hit" weak-match collapse now prunes to 0 under rerank+highlight — so it live-verified the empty state instead; the grounded path used Phase 14b's known-good faith query (174.9s cold for grace, full pipeline, no error). (ii) Upload+ingest of a *new* book was not re-run in-browser — the flow reuses the `phase12-a` corpus; upload UI was live-verified in Phase 15 and CPU ingest is tens of minutes (same rationale as Phase 15 deviation ii). (iii) No headless browser on the box, so "full browser flow" = HTTP drive of the rendered pages + route handlers with a cookie jar (Phase 15 precedent; components covered by the 28 pure-helper unit tests). (iv) Live finding → fix in-phase: EPUB `parent_section` metadata can be raw HTML debris (`<a href="part0002…`) — React escapes it (no XSS) but it's tag soup in a card header, so `displaySection()` drops labels containing `<`; worker-side metadata cleanup left as a post-v0 item. (v) `web/next-env.d.ts` was dirtied by the dev server appending the `.next/types` reference and restored before commit, per the Phase 15 two-reference rule.)

v1 (planned 2026-06-05 — see the **v1 Plan — Beyond Phase 16** section below for milestones, per-phase prompts, dependencies, and the parked/trigger-gated list):

- [x] Phase 16b — Remote inference: kill in-process models (completed 2026-06-09, branch `phase-16b/remote-inference`. **ADR 0006** locks the transport. **No model weights load in-process anywhere** — every inference leg is a remote API call through `worker/inference.py` (the shared transport `api/` imports). **Pinned ids + prices (DeepInfra, live-verified 2026-06-08/09):** embeddings `BAAI/bge-large-en-v1.5` ($0.01/1M, OpenAI-compatible `…/v1/openai`) — the **EXACT v0 weights**, so every stored Milvus vector stays valid (re-embed NOT required); highlight `BAAI/bge-m3` dense ($0.01/1M, one batched call/query); rerank `Qwen/Qwen3-Reranker-8B` ($0.05/1M, native `…/v1/inference/{model}` queries+documents→scores shape). **Deviation (reranker):** the 2026-06-05 pin `BAAI/bge-reranker-v2-m3` was **gone from DeepInfra** at implementation time (404, absent from catalog) — operator chose the max-accuracy `Qwen3-Reranker-8B` over the cheaper 0.6B/4B siblings (env-swappable via `SERMON_RERANK_MODEL`; a large jump over the dead 2021 MiniLM cross-encoder either way). Unprefixed `DEEPINFRA_API_KEY` (GOOGLE/PPQ precedent). **Embedding-space guard:** migration 0003 adds Postgres `meta('embedding_model_id')` seeded with the v0 model; `embedding.py` refuses to embed on env-vs-row mismatch (silent space drift = silent retrieval destruction). **Weight-parity proof:** `tests/golden/local_model_refvecs.npz` captured from the in-process sentence-transformers loaders immediately before removal; `test_embedding.py` pins (live) remote-vs-local cosine ≥ 0.999 for bge-large + bge-m3 — **9/9 PASS**. **512-token window (live finding → fix):** DeepInfra *rejects* >512-token inputs (the in-process model silently truncated to `max_seq_length=512`; truncate params probed on both endpoints — neither truncates). Replicated client-side with the model's own WordPiece tokenizer (bundled `worker/assets/bge-large-en-v1.5-tokenizer.json` + pure-Rust `tokenizers`, ~700KB, NOT a model — microseconds, no torch/GPU), trim to 510 content tokens = byte-identical to the old window; chunker routed through a `BaseEmbedding` adapter over `embed_texts` (dropped `llama-index-embeddings-openai-like`). **Resilience deviation:** spec said "one retry"; live verify hit a DeepInfra degraded window (4–35s/128-batch, intermittent ConnectError/timeout) — bumped to `max_retries=5` (openai SDK exp-backoff) + matching rerank backoff, errors now name the cause. **FK-ordering fix:** adding the `Meta` model shifted SQLAlchemy's mapper order and flipped an *implicit* parent-before-child insert in `ingest._insert_book_with_chunks` (no `relationship()`); made the parent `flush()` explicit. **LLM (extends ADR 0005):** added a `deepinfra` provider — DeepInfra serves `google/gemini-2.5-flash` over chat-completions (2.0s, `reasoning_effort=none` honored), so `SERMON_API_LLM_PROVIDER=deepinfra` runs the WHOLE stack (embeddings+rerank+highlight+LLM) on **one vendor + one key**; google/ppq stay first-class. New `SERMON_API_LLM_REASONING_EFFORT` knob. **Deleted:** torch + sentence-transformers (+ CPU-wheel override) from both pyprojects (locks regenerated, ~1.5GB lighter images); the `sermon-hf-cache` volume, the `prewarm` one-shot + `infra/scripts/prewarm_models.py`, and the `HF_*` offline env from compose + both Dockerfiles + deploy.sh. **Measured deltas (dev box, live DeepInfra):** `/search-summary` E2E **~134s → ~25s** (`reasoning_effort=none`) / **~30s** (thinking) — retrieval+rerank+highlight **~71–76s → 21.2s**, LLM **~58–64s → 3.8s** (none, 3 cites) / **8.5s** (thinking, 8 cites). **RAM ~3.7GB → 253MB** peak for the full warm pipeline (>14× cut; <1GB target met with huge margin). **Gates:** `make test-retrieval-golden` **9/9** live (same `min_score` floors — weights unchanged); worker 69 passed/2 skip + api 73 passed, ruff/format/pyright(strict) clean both; `make test-isolation` **3/3**; `/check-tenant-leak` clean; `tenant-auditor` + `/security-review` on the original diff (13-agent adversarial review: 2 confirmed findings, both fixed) and re-run on the truncation/LLM diff. **ppq capability changes (2026-06-09):** ppq now exposes `/v1/embeddings` but OpenAI embedders only (no BGE) + no rerank, and documents reasoning only on `/v1/responses` — so embeddings stay env-portable for the day ppq ships BGE, but DeepInfra is the exact-weights home today. **Deferred:** (i) instance downsize t3a.xlarge → t3a.large — AWS creds not in the dev env; the code/compose/deploy.sh are ready and `deploy.sh` performs it, so it's operator-run-shaped, not code-shaped; (ii) cost reconciliation against the live DeepInfra dashboard — estimated ~$0.006/book ingest + well under a cent/search (matches the planning estimate); the operator confirms actuals on the dashboard.)
- [x] Phase 17 — CI service containers + model cache: make the gates run for real (completed 2026-06-11, branch `phase-17/ci-real-gates`. **Prompt staleness reconciled post-16b:** no local models and no HF cache exist anymore (ADR 0006) — the "model cache" half of the title is dead; the only resources CI boots are the compose services. New `tenant-isolation (infra gates)` job: `make up` (self-creates infra/.env from the example, healthcheck `--wait`) → migrate-up → bootstrap-milvus → WordNet → isolation suite + Phase 31 storage + Phase 8 dedup suites — keyless, zero spend (synthetic vectors), with a **no-skip guard** that fails the job on any SKIPPED line so a vacuous green is impossible; `infra/.env` sourced where Postgres is touched (the 54322-vs-5432 code-default trap, now documented in worker/AGENTS.md). Golden suites: prompt option (b) **upgraded** — keyless `retrieval-golden` gains a `golden-loud-skip-guard` (::warning + job summary naming the activation path), and a new `retrieval-golden-live (DeepInfra-keyed)` job activates AUTOMATICALLY once the operator runs `gh secret set DEEPINFRA_API_KEY` (the filter job probes secret presence — secrets aren't readable in job-level `if:`); its guard tolerates only the Phase 23 corpus-gap skip, anything else is a wiring bug → red. Option (a)'s HF_HOME cache is dead post-16b and its corpus half is Phase 23's job. Changed-path gate (hand-rolled `git diff` vs base, fail-OPEN since it guards a security check) spares docs/web-only PRs the multi-minute infra boot; pushes to main always run it. Deviations: prompt's `make -C infra up` doesn't exist (root `make up` used); "the filter job exists" overstated — it was existence-checks only, real changed-path detection was new work; test_chunking epub e2e excluded from the live job (uncommitted sample + pandoc would trip the strict guard); stale HF-cache docstrings in test_retrieval_golden.py + worker/Makefile scrubbed in-phase. Gates: yaml parses; local 3-suite run vs live stack 30 passed/0 skipped; guard classification unit-checked against synthetic logs; /security-review on the workflow diff — no findings (literal-boolean secret probe, env-var-only expansion, fork PRs get no secrets on plain `pull_request`). **Verified on the PR itself:** the tenant-isolation job ran FOR REAL in CI (compose boot + migrations + bootstrap on the runner, ~1m14s, all suites executed, no-skip guard green) and the mutation proof ran IN CI — throwaway draft PR (#39) dropping `filter=expr` from both searches turned the job RED with the two documented "CVE-class data leak" assertion failures (FAILED, not SKIPPED), then was closed unmerged. **Two live findings the first runs surfaced (both fixed in-phase, and the no-skip guard caught the first exactly as designed):** (i) `nltk.download()` returns False on failure WITHOUT raising — the download died silently in 200ms with exit 0; hardened to `raise_on_error=True` + 3 retries + probe-verify; (ii) nltk 3.9 leaves wordnet as an UNEXTRACTED zip that `nltk.data.find("corpora/wordnet")` — the exact probe the suites and `worker/dedup.py` gate on — cannot resolve (the dev box only works because its copy was extracted long ago); CI now extracts post-download. Latent follow-up flagged: `worker/dedup.py`'s cold-start ensure has the same zip-only blind spot on any fresh box — fold into Phase 29's image bake (or Phase 23 runbook). **Operator actions at merge:** mark `tenant-isolation (infra gates)` REQUIRED in branch protection; optionally `gh secret set DEEPINFRA_API_KEY` to activate the live golden job.)
- [x] Phase 18 — JWT-secret startup guard + Pydantic extra='forbid' + /readyz (completed 2026-06-11, branch `phase-18/jwt-guard-readyz`. **Boot guard:** FastAPI lifespan hook (the first in api/ — Phase 19's CORS guard extends it) refuses boot when `SERMON_API_JWT_SECRET` is unset/empty or equals the public dev placeholder; refusal names both env vars. **Fail-closed posture:** new `ApiSettings.env Literal["dev","prod"]` defaults to **prod**; empty string (compose `${VAR:-}`) → prod via before-validator; typos → loud ValidationError at import, never a silent disarm; `DEV_JWT_SECRET` is a single module constant used as both field default and guard comparand with a drift-pin test; `infra/docker-compose.prod.yml` pins `SERMON_API_ENV: prod` so a stray host `dev` cannot disarm (atop the existing `:?` secret requirement); `infra/.env(.example)` sets `dev` so `make dev` keeps booting. Lying settings docstring fixed. **extra='forbid'** on all four inbound body models (Signup/Login/Search/SummaryRequest — /upload is multipart, rest are response-only): smuggled `user_id`/`book_ids` is now a hard 422 naming the field — Phase 12 deviation d closed mechanically; web proxies unaffected (exact field whitelists). **GET /readyz** (`api/readyz.py`): three concurrent connectivity probes — `SELECT 1` via the shared async engine, `has_collection` via lazy pymilvus in a thread with RPC-level timeout (wait_for can't cancel a thread), redis.asyncio PING with socket timeouts — 2s per-dep budget; 200 only when all three answer, 503 with per-dep `down` breakdown; **failure detail logged, never bodied** (Redis connection errors embed the broker password — test-pinned that secrets never reach the response); /healthz stays dependency-free; Phase 29 HEALTHCHECK + Phase 30 readinessProbe consume this. Tests 73 → **97** (guard refuse/allow/opt-out matrix, TestClient-wired proof, env posture, forbid ×4, readyz 200/503/timeout/no-leak). **Live verify:** uvicorn refused boot on default secret; booted with `openssl rand -hex 48` and with dev opt-out; live /readyz → 200 all-ok; authed POST /search with `user_id` → 422 `extra_forbidden`. **Gates:** `make test-isolation` 3/3 ×2 (build + tenant-auditor); tenant-auditor PASS (readyz leaks no DSN/credential/tenant data; JWT-only user_id posture intact; no Milvus filter touched); `/check-tenant-leak` greps clean; `/security-review` no findings — fail-closed verified down to installed uvicorn 0.39/starlette 0.48 source (lifespan failure always exits; auto-mode fallback unreachable). **Deviations:** (i) built across an interrupted session — the resumed builder kept the partial tree, fixed its one bug (missing `AsyncGenerator` import) and rebuilt local history once pre-push so all 6 commits are self-consistent; (ii) `SERMON_API_ENV=dev` appended blind to the read-denied `infra/.env`; (iii) two throwaway dev users created during live verify (`phase18-verify@`, `p18b@example.com`, dev Postgres only).)
- [x] Phase 19 — Edge rate limiting (signup/login/search-summary) + CORS prod-origin guard (completed 2026-06-11, branch `phase-19/edge-rate-limits`. **Limiter:** hand-rolled `redis.asyncio` fixed-window dependency (`api/ratelimit.py`) over the already-locked redis client — NOT slowapi (new dep + incomplete typing vs pyright strict; rationale in api/AGENTS.md); atomic INCR+EXPIRE NX+TTL pipeline in broker-Redis **logical db 2** (db 0/1 stay Celery's; tasks_client mirror deliberately untouched) so limits hold across replicas. Buckets env-tunable + boot-validated: signup_ip 5/60 + login_ip 10/60 per client IP; summary_user 5/60 per JWT `user_id` as a decorator dependency so the 429 fires BEFORE the paid retrieval+LLM pipeline. 429 = fixed detail + `Retry-After`, never names Redis/bucket/key; Redis-down fails OPEN with loud log; `SERMON_API_RATELIMIT_ENABLED=false` kill switch; /healthz + /readyz unlimited. **Scout reality-check:** Caddy already rate-limits per-IP at the edge — this layer adds what Caddy can't see (cross-replica enforcement, per-USER granularity, non-Caddy traffic); the prompt's "~134s CPU" rationale was stale post-16b. **Client-IP plumbing (was missing end-to-end):** web auth proxies now forward Caddy's X-Forwarded-For verbatim (`web/lib/http.ts`); the api honors it ONLY when `SERMON_API_TRUST_PROXY_HEADERS=true` (default off = fail-closed to TCP peer; prod compose enables it). **Two live security catches in-phase:** (i) uvicorn's default `proxy_headers=on` rewrote `request.client` from spoofed XFF for loopback peers — a hidden second trust knob; pinned `--no-proxy-headers` in both entrypoints; (ii) **tenant-auditor FAIL → FIXED:** `client_ip()` first trusted the LEFTMOST XFF entry ("Caddy discards client XFF") — true for modern Caddy (≥2.5 replaces) but if any hop ever APPENDS, leftmost is attacker-chosen and rotates the bucket per request, zeroing the limiter; switched to the **RIGHTMOST** entry (proxy-written under BOTH behaviors) + `ipaddress` shape-validation with peer fallback, four doc sites corrected, spoof rotation pinned in tests, auditor re-verdict FIXED, live-verified: 12 logins rotating leftmost spoofs against a fixed attested rightmost → 10×401 then 429 + Retry-After 60. **CORS prod-origin guard** in the Phase 18 lifespan hook: outside dev refuses boot on empty/wildcard/empty-string/loopback origins (Starlette mirrors Origin for `"*"` + credentials = credentials-for-any-site); `SERMON_API_CORS_ORIGINS` `:?`-required in prod compose. **Tests:** api 97 → **143**, web 42 → **46**; lint/pyright(strict) clean both. **Live verify:** cross-process burst — two uvicorns sharing one Redis 429 at exactly the shared threshold on both ports; summary burst 429s pre-pipeline; CORS refuse/refuse/boot matrix; spoof checks trust off AND on. **Gates:** `make test-isolation` 3/3 ×2; `/check-tenant-leak` greps clean; tenant-auditor FAIL→FIXED (items 2–5 PASS first pass); `/security-review` no findings (RESP bulk-string encoding precludes identity injection; CRLF into upstream fetch blocked by llhttp+undici; 401-vs-429 ordering adds no oracle) + 2 informational items fixed in-phase. **Parked per prompt:** email verification/CAPTCHA, per-tenant quotas. **Deviations:** built across an interrupted session (resumed builder kept partial settings.py work); `--no-proxy-headers` + rightmost-XFF hardening beyond the prompt's literal text; dev-db residue user `burst-p19b@example.com`.)
- [x] Phase 20 — Upload idempotency + upload_tasks ownership + content-type posture (completed 2026-06-11, branch `phase-20/upload-integrity`. **upload_tasks ownership (migration 0004):** one row per `POST /upload` — `task_id` (Celery UUID, **api-minted**, no column default), `user_id` FK→users CASCADE + B-tree index, nullable `book_id` carrying the worker's in-flight claim (**deliberately NO FK** to global_books — the claim names a book whose row may never land), `filename`, `created_at`. `GET /tasks/{task_id}` resolves the row scoped to the JWT user: non-owned, nonexistent, and non-UUID ids are the SAME 404 (no existence oracle), and the Celery backend is consulted **only after ownership passes** — its PENDING-for-unknown-ids behavior can no longer leak probe feedback. Ordering is load-bearing twice: the row commits BEFORE `send_task` (a crash between leaves an owned PENDING row, never an unowned running task), and ownership precedes any Celery read. **Idempotency claim (Phase 9 window closed):** the new-book path records its minted `book_id` on the row before the first non-transactional write; a redelivered task consults the claim — committed `global_books` row → converge (upsert user_library, return duplicate); uncommitted → scrub the partial Milvus vectors and re-run under the SAME `book_id` (same originals object key). Claim-less manual enqueues keep the legacy posture; residual documented: concurrent duplicate execution on visibility-timeout expiry of a still-running task (task-id-keyed, not leased). **Content-type posture — option (a):** early libmagic sniff of the first 8 KiB with the NEW rationale (don't stage attacker bytes to disk, don't burn a doomed ingest; bytes are inspected, never the client header; worker `detect()` stays authoritative) and the old docstring rewritten to match; `_ALLOWED_UPLOAD_MIMES` mirrors worker extractors per the mirror-not-import rule; python-magic + libmagic1 added. **Live verify (real api + real Celery worker on dev stack):** (i) script-renamed `.epub` (shell script, multipart even DECLARED `application/epub+zip`) → 415 naming sniffed `text/x-shellscript`, zero rows/files/queue messages; (ii) 404 contract — foreign user, unknown UUID, garbage id all byte-identical `{"detail":"Task not found."}`, owner 200 with true state, unauth 401; (iii) re-POST of an ingested EPUB → `was_duplicate: true, rows_inserted: 0`, Milvus 2→2, single global_books/user_library rows; (iv) **the Phase 9 kill -9 drill, first-ever live run** — mid-window pinned deterministically (held `ACCESS EXCLUSIVE` lock on global_books blocked the commit with 2 vectors already in Milvus + claim recorded), the active prefork child identified by socket inode and `kill -9`'d → `WorkerLostError` → immediate requeue (`task_reject_on_worker_lost`) → worker logged "redelivery with uncommitted claim … scrubbing partial vectors and re-running under the same book_id" → SUCCESS: ONE global_books row, Milvus rows == chunks == 2 (zero orphans, zero doubles), zero NEW global orphans (only the 3 known pre-Phase-21 dev orphans remain). **Gates:** migration 0004 applied + down/up round-trip on dev PG; schema-reviewer PASS 6/6 (`alembic check` clean, additive-only, single head); tenant-auditor PASS 4/4 (+3 forward notes: MIME-mirror drift hazard, no-lease residual, ownership-before-Celery ordering pinned only by backend-not-called tests); `/security-review` NO FINDINGS; `/check-tenant-leak` greps clean; `make test-isolation` 3/3 ×2; api **155** passed; worker 72 passed + live-keyed `test_ingest.py` **4/4** incl. `test_task_claim_redelivery_converges`, which had NEVER run live before (skip-gated on the missing key). **Deviations:** (i) live verify hard-stopped mid-session — `DEEPINFRA_API_KEY` was empty in `infra/.env`, so no ingest could run; operator supplied the key in-session (transcript exposure noted — consider rotating), appended via python one-liner; (ii) `make -C worker test` does NOT source `infra/.env`, so live-gated suites silently skip under it — the live run used tracked-example env + key + `SERMON_POSTGRES_PORT=54322`; **planned into Phase 23** (its `test-live` Makefile-target build item — Phase 26 stays docs-only by design); (iii) dev-db residue: users `p20a/p20b@example.com`, one FAILURE task `a56f34e3…` with clean claim (0 vectors), two tiny synthetic books in p20a's library.)
- [x] Phase 21 — parent_section HTML strip at ingest + backfill + orphan-debris sweep (completed 2026-06-11, branch `phase-21/parent-section-clean`. **Capture fix:** new `chunking.clean_heading()` — stdlib `html.parser.HTMLParser` subclass collecting text with `convert_charrefs=True` (entities unescape exactly once, no separate `html.unescape` pass — that would double-unescape), iterative so hostile nesting cannot recurse, whitespace collapsed; survives nested tags, `>` inside quoted attributes, and plain `a < b` prose; CPython 3.12 discards unterminated trailing fragments at `close()` (the pandoc 72-col heading-wrap truncation case, pinned in tests). Wired at the single capture site `_heading_offsets()` so Postgres `chunks.parent_section` and Milvus metadata receive identical clean values; **empty-after-strip headings are DROPPED from the offsets list** — chunks fall back to the nearest preceding real heading or None, `''` is never stored. `web/lib/summary.ts` `displaySection()` stays as belt-and-suspenders per prompt. **Maintenance scripts** (backfill_chunks.py conventions; dry-run default + `--execute`; Makefile targets `clean-parent-sections`/`sweep-orphans` source `../infra/.env`): `clean_parent_sections.py` reuses `clean_heading` (never reimplements), detects PG and Milvus dirt independently per book (a crash between stores converges on re-run), updates PG via parameterized `(book_id, chunk_index)` UPDATE, rewrites Milvus rows via query-with-vector → delete-by-id → reinsert in 500-row batches (auto_id PK rules out upsert; **reinserted rows mint NEW auto-ids** — `highlights.vector_id` is unwritten today so nothing breaks; documented in script + AGENTS.md); `sweep_orphans.py` classifies (a) Milvus-only book_ids and (b) fully-orphaned `global_books` rows, with a **three-layer structural refusal** on tenant-reachable candidates (`classify()` routes refs>0 to refusals, never deletes; exit 3 + fresh pre-delete re-checks; `user_library` RESTRICT FK as DB backstop) and every Milvus expr gated by a refuse-don't-escape `^[A-Za-z0-9._-]{1,64}$` allowlist. 76 new unit tests; worker suite 142 passed/21 env-gated skips; ruff + format + pyright(strict) clean. **Live data pass (dev stack):** fresh hostile-EPUB ingest (anchor+class h1, anchor-only h2, `<span>` + `&amp;` entity headings) → 3 chunks, zero `<` and zero `''` in both stores, `Grace & Truth` unescaped, anchor-only heading omitted with fallback — then fully self-cleaned (user_library/global_books/chunks/Milvus/MinIO original/throwaway user all byte-back to before-counts). Backfill: dry-run pg_dirty=186 / milvus_dirty=186 with exact per-book PG↔Milvus parity (Mere Christianity 166, Winning the War 19, sample 1; the +1 over the inventory's LIKE-probe 185 is a pure whitespace-collapse row — exactly what capture now stores), `--execute` → 0 dirty in both stores across all 1214 rows, per-book Milvus counts unchanged, spot-checks clean by `(book_id, chunk_index)`, second dry-run 0 (idempotent). **Orphan sweep — prompt debris names were STALE:** `b_mere_christianity`, `b_1_thess`, `88ba2fe2…`, `_test_phase8_synthetic` all verified ABSENT (exact + prefix + ILIKE probes, 0 rows each); the actual orphans were `cc7be4f5…` (Mere Christianity dev twin, 167 vectors), `b_phase6_real_epub` (167), `03691dda…` (1 Thess twin, 27) — dry-run matched the live inventory exactly, refs re-verified 0, `--execute` → Milvus 1575→1214 == chunks count (exact post-sweep invariant), global_books stays 5 with Milvus book_id set == global_books set, idempotence dry-run empty. **Gates:** `make test-isolation` 3/3 ×2 (0 skips); `make test-retrieval-golden` 9/9, 0 skips (metadata-only rewrite — golden floors held); tenant-auditor PASS (refusal verified structural at all three layers; diff adds no `.search`; Phase 3 filter invariant untouched); `/check-tenant-leak` greps clean; security review PASS (parser timed linear across 8 adversarial patterns incl. the CVE-2025-6069 quadratic case — patched in this interpreter; parameterized SQL only; injection payloads refused by the allowlist; no secrets printed; zero filesystem surface) + 2 non-blocking notes (500-row in-memory reinsert window is availability-only; parser linearity relies on distro-patched 3.12.3 — pin patched interpreters when Phase 29 bakes images). **Deviations/findings:** (i) stale prompt debris as above — swept exactly what the generic classifier found live, nothing else; (ii) **DEEPINFRA_API_KEY in `infra/.env` was dead** (401 — the Phase-20-exposed key, since revoked); working key recovered at operator direction from the sovit.xyz box (`~/Websites/sermon.guide/infra/.env`, fingerprint `12d9e2f8e948`) via a no-print ssh pipe; **prod EC2 `/opt/sermon/.env.prod` still carries the revoked key — open operator action** (replace line + `docker compose up -d` to recreate api/worker); (iii) port-doc inversion: dev compose Postgres actually listens on **5432** and the stale **54322** is the CODE default in `worker/db/settings.py:17` (Phase 17's framing had it backwards); `worker/AGENTS.md` remap note equally stale — align in Phase 26; (iv) golden suite warm path measured **26m23s** against its own "resolves in seconds" docstring — the dedup gate is CPU-bound pre-verdict (pandoc + chunking + hashing per book before the duplicate short-circuit); profile in Phase 23 or fix the docstring in Phase 26; (v) `DBSettings.dsn` is SQLAlchemy-shaped (`postgresql+asyncpg://`) and raw `psycopg.connect()` rejects it — minor trap for future one-off scripts.)
- [x] Phase 22 — Graceful degradation across retrieval arms + model loads (completed 2026-06-12, branch `phase-22/graceful-degradation` — first two-round phase; the round-1 build PASSED tenant gates but the resilience gate + live drill caught two real defects the plan's own premise missed: pymilvus 2.6's in-request recovery calls `reconnect` with a HARDCODED `timeout=10` BEFORE the deadline check (so the planned "client timeout config" never bounded the first Milvus-down request — the plan text's "instead of the 12 s retry long-tail" claim was wrong as written, the 12.2s tail WAS this 10s reconnect + overhead), and after failed recovery pymilvus closes the channel so every later call is a closed-channel `MilvusException` (never `grpc.RpcError`) — recovery never refires and the pinned client singleton kept the dense arm degraded UNTIL API RESTART (live: 7 probes/5+min, 2 processes). Round-2 fix: identity-guarded singleton reset on dense-arm `MilvusException` (make_client is self-validating — live `get_server_type` before the atomic publish, so no half-built client is ever visible) + `DENSE_ARM_BUDGET_SECONDS=4.0` `asyncio.wait_for` around the Milvus leg ONLY (client checkout + search RPC in the budgeted thread; the remote embed keeps its own RemoteInferenceError taxonomy/retries; orphaned-thread caveat documented); budget expiry does NOT reset from the loop side — the orphaned thread's own outcome decides (accepted by re-review). Degradation contract: gather `return_exceptions=True`, non-Exception BaseExceptions (CancelledError) re-raised; one arm down → surviving ALREADY-SCOPED arm + `degraded:["dense"|"sparse"]` (book_ids resolved ONCE from the JWT above the fan-out — no fallback re-queries, pinned by tests asserting the surviving arm got the exact library list); both down → 503 fixed-detail constant (no exception content — DSN-password canary test); rerank/highlight each catch `RemoteInferenceError` ONLY → RRF-top-K/unpruned passthrough + flag (rerank failure does NOT skip highlight); `degraded: list[str]` always present, `[]` healthy (Phase 27 metrics-friendly), additive on SearchResponse + SummaryResponse; /search-summary PROCEEDS-WITH-FLAG (decision: degraded grounding is narrower not wrong, the citation contract holds, 503 would lie about recoverability — also distinguishes outage-empty from corpus-empty). Live drill (2 full Milvus stop/start cycles, ONE api process): first-failure 5.16s/5.29s (was 10.6-11.3s), steady-down ~4.5s, recovery WITHOUT restart 9s post-healthy both cycles, down-window summary 200+flag; isolation 3/3 ×3 (round-1 gate, round-2 sweep, final drill); api 204 passed, worker 142/21. Gates: tenant-auditor PASS ×2, /check-tenant-leak PASS ×2, resilience review round-1 FAIL (the 2 defects) → round-2 PASS on all 6 substantive checks with one mechanical finding (untyped test lambda vs pyright strict; fixed in-loop as `fix(api): type the dense-leg embed seam` — recorded judgment call: NOT counted as the two-consecutive-rounds hard stop since gates round 2 was substantively green and the finding was build hygiene, not fix-thrash). Forward notes: steady-state-down requests burn the full 4s budget (~4.5s wall) instead of the predicted 2.5s fast-fail — bounded per spec but reconstruction hangs to the budget, candidate micro-tune later; pymilvus 30s-idle registry gate means sustained sub-30s traffic during an outage can delay post-restart recovery (never manifested live — both cycles recovered on the first probe); the readyz per-call 2.0s probe has the same warm-client recovery exposure (pre-existing, out of scope); dev-box env: default LLM provider google with empty GOOGLE_API_KEY in infra/.env → healthy /search-summary 503s unless SERMON_API_LLM_PROVIDER=deepinfra (pre-existing config gap, operator call); pre-existing pymilvus pin drift api `<4` vs worker `<3` left untouched. Deviations: (i) this plan-text's own "typed error via client timeout config" framing corrected by events as above; (ii) api/AGENTS.md "no graceful degradation" trust gap replaced with the honest residual (sparse-down silently loses BM25's corpus-presence FP suppression, conveyed only by the flag; arms' full-Exception breadth means an in-our-code TypeError degrades loud-in-logs/invisible-in-5xx — Phase 27 degraded-metrics is the mitigation).)
- [x] Phase 23 — Production corpus seeding plan + idempotent bulk-ingest runbook (completed 2026-06-12, branch `phase-23/production-corpus`, **operator-gated stop-at-PR — corpus rights review pending merge**. Policy: `docs/CORPUS_POLICY.md` — public-domain only (Gutenberg/CCEL), 8-entry starter corpus (Augustine Confessions, Calvin Institutes, Spurgeon All of Grace, Wesley Sermons, Bunyan Pilgrim's Progress, à Kempis Imitation, Brother Lawrence Practice of the Presence, Athanasius On the Incarnation), every manifest entry carries source + license fields, all 8 download URLs verified live HTTP 200; manifest at `worker/seeds/manifest.jsonl`, ebook FILES never tracked (land in gitignored `worker/tests/samples/`). Seeder `worker/scripts/seed_corpus.py`: deterministic `uuid5(SEED_NS, sha256(file):user_id)` task ids; `upload_tasks` claim committed BEFORE `apply_async` (api/uploads.py ordering — crash-safe, unlike claim-less `make enqueue`); ownership = the corpus-seed label user `d296b559-28f8-54d6-9577-a5539913335c` (same identity `make enqueue TENANT=corpus-seed` resolves; non-loginable placeholder hash); SERIAL by default (--max-in-flight 1) — the 300s broker visibility timeout + a free worker slot can interleave a redelivered copy with a still-running ingest and double a book's vectors (Phase 20 task-id-keyed-not-leased residual), parallel mode documented with that warning; --dry-run; 47 pure unit tests. Silent-skip trap KILLED (Phase 20 deviation ii): `make -C worker test-live` hard-fails without `../infra/.env` (exit 2 instructive), sources it per the migrate-up pattern, runs golden+ingest-e2e+embedding live suites, FAILS (exit 1) on any skip not matching the CI guard's tolerated `corpus sample(s) missing` class — that substring is now load-bearing in THREE lockstep places (golden fixture, ci.yml live-gate guard, scripts/test_live.sh; rule pinned in the script header); plain `make test` byte-identical keyless. Golden: 13 rows (+5 seeded-corpus incl. THE "grace" row — the spec's own query that pruned to zero on dev books, now PASSES grounded by All of Grace); fixture's wholesale missing-sample skip narrowed to fire only when ALL samples are absent (CI posture unchanged), otherwise per-row corpus-shape skips. LIVE SEED DRILL on this box: 2 smallest books seeded (All of Grace 72.5s/88 vectors, Brother Lawrence 31.6s/40 vectors; global_books 5→7, Milvus 1214→1342, vector==chunks parity clean); idempotent re-run converged (~1s/book `was_duplicate`, zero new rows/vectors, sweep-orphans zero candidates) — drill CAUGHT a real seeder defect (deterministic task id + retained result-backend payload made re-runs print run-1's cached SUCCESS, masking-capable) fixed in-phase (`4373884`, re-run 3 reports duplicate×2 correctly); `test-live` exit 0: 23 passed / 4 tolerated corpus-gap / ZERO key-infra skips in 28m49s; plain test 189 passed keyless; post-seed gates ALL PASS — test-isolation 3/3, /check-tenant-leak clean, live contamination audit: NO pre-existing user gained any row (the single foreign user_library row = the golden user's own dedup-granted ownership of the shared book — the correct tenant path, traced and cleared); Milvus partition spot-checks clean. Deviations: (i) enqueue_ingest's private label-derivation seam promoted to public names (pyright reportPrivateUsage, byte-identical derivations); (ii) seeded global_books.title is the filename stem (ingest sets title=path.stem) — human titles live in the manifest; (iii) the in-drill seeder fix above. Forward notes: activating the 5 seeded golden rows in CI would blow the retrieval-golden-live 25-min timeout (Calvin alone; Phase 21 finding iv) — they stay corpus-gap skips in CI by design, full coverage is `test-live` on a seeded box; the seeded corpus REMAINS on this dev box (golden rows now exercise it); operator owns: corpus-rights sign-off at merge, then production seeding per `docs/SEED_CORPUS.md`.)
- [x] Phase 24 — Comma-merged citation extraction + library-size search-filter cap (completed 2026-06-14, branch `phase-24/citations-filter-cap`, golden gate cleared 2026-06-15 after PR #53 fixed the ingest blocker — resolution at the end of this row. **Citations:** `_extract_citations` (api/summary.py) now resolves comma-merged brackets — scans `[...]` groups via `_BRACKET_GROUP`, resolves each member by greedy longest-prefix against the known source-marker set (labels CAN contain commas — `_LABEL_BANNED` strips only `[ ] :`, so naive `split(",")` would mis-resolve `[Faith, Hope:7]`; longest-prefix bounded by comma/end-of-group preserves comma-labels AND the `[X:1]`-not-inside-`[X:12]` substring guard); unresolvable members are dropped, never fabricated; single-marker behavior byte-identical. `web/lib/summary.ts segmentSummary` carries the same contract — one linked-chip segment per resolved member at a distinct start offset (repeated members get document-ordered starts, since `SearchPanel` keys chips on `start`); the Phase 14b "merged → plain text" test FLIPPED to expect chips. **Filter cap:** `worker/retrieval.py dense_search` splits the `book_id` Milvus filter into `MILVUS_FILTER_BOOK_ID_CHUNK`=1000 (env `SERMON_MILVUS_FILTER_BOOK_ID_CHUNK`) contiguous non-overlapping scoped slices, each pulling its own top-limit, merged to the global top-K by cosine — recall preserved, NO silent cap (a cap would drop part of a user's library = correctness + tenant-trust regression); the union of slice filters == the input `book_id` set exactly; libraries ≤ chunk size take the unchanged single-search fast path; BM25 arm left unchunked (bound `= ANY(:book_ids)` array — one param, no 360 KB string). api/AGENTS.md "No library cap" trust-gap row updated to the implemented strategy. **Gates — all Phase-24-applicable PASS:** tenant-auditor PASS (chunk union == input, every sub-search scoped, empty-library guard intact, `user_id` JWT-derived, sparse arm scoped); `/check-tenant-leak` clean (one production Milvus search site, always `book_id`-filtered; zero request-sourced tenant ids); `make test-isolation` **3/3**; **synthetic 10K filter-cap live 7/7 — 10,500 books / 11 slices / 0.215s / full recall, no expr-length rejection**; fast suites api **48** / web **95** (incl. the flipped merged-bracket cases). **Golden-gate resolution (2026-06-15):** the golden run had ERRORED on a pre-existing INGEST bug surfaced by the Phase-23 corpus — Milvus rejected a **355,687-char `content_chunk` (cap 65535)** from a large seeded EPUB, failing the session corpus fixture before any Phase 24 search code ran. Fixed OUT OF BAND in **PR #53 (e9422ac, `worker/chunking.py`)** — a sub-split pass that breaks any chunk over a safe byte trigger below Milvus's hard 65535 cap, leaving normal books untouched. After #53 merged to main, `make test-retrieval-golden` was re-run on the combined state (#52 + #53; #52 touches `worker/retrieval.py` so the combined run was required, verified on a throwaway branch off fixed main, clean merge): **GREEN — 14 passed in 2250.54s (37.5 min)**, cold-ingesting Calvin/Athanasius/Bunyan via the sub-split with full retrieval recall, confirming both the fix and that the chunked filter cap is a no-op at golden corpus size. The looping pre-fix corpus-seed drill that had originally surfaced this bug was stopped (targeted PID kill), the broker purged of 13 stuck messages, and `sweep-orphans` verified zero orphans / vector==chunk parity before and after the run. Deviation: golden green was achieved by merging the independent ingest fix (#53) first, not by any change within Phase 24 scope — Phase 24's own diff is unchanged from the gates above.)
- [x] Phase 25 — Web component + Playwright E2E coverage for search/citations/upload (completed 2026-06-15, branch `phase-25/web-e2e`, web-only — no tenant gates. **Component tests** (decision: `@testing-library/react` + vitest/jsdom over Playwright component mode — reuses the one `pnpm test` runner, minimal dep surface; REVERSES the prior AGENTS.md "pure helpers only / no jsdom" posture, rationale recorded there): a `vitest.workspace.ts` split — `lib` project (node env, `test/**/*.test.ts`) + `components` project (jsdom, `test/components/**/*.test.tsx`, `@vitejs/plugin-react`) — runs both in one `pnpm test` (118 tests, 95 lib + 23 component). Covers SearchPanel states (loading-ticker/empty/error/grounded), the Phase 24 merged-bracket citation chips (`[A:1, A:2]`→two resolving chips, unresolvable dropped), and Uploader form+poll. Vitest-2.1 gotchas recorded: no `projects` InlineConfig field (that's v3 — used `defineWorkspace`); `@testing-library waitFor` deadlocks under fake timers (use `act(() => vi.advanceTimersByTimeAsync())`). **E2E** (`@playwright/test`, separate `pnpm e2e`, never collected by vitest): login→search→grounded summary with resolving chips, login→upload→task-status asserting the Phase-20 own-200/other-404 contract. **LLM stub:** pre-made "test provider row" wasn't satisfiable (base_url was hardcoded), so added a TEST-ONLY `SERMON_API_LLM_BASE_URL` knob (api/settings.py→summary.py `_client`; None=prod behavior, not wired into any prod config) — the live/nightly path points a real api at `e2e/support/stub-llm.mjs`. **CI boundary (documented, spec-permitted):** the web CI job has no Postgres/Milvus/worker, so CI E2E runs against an in-memory `e2e/support/fake-api.mjs` speaking the exact wire shapes (auth, grounded summary whose `[book:chunk]` markers resolve to chips, upload, ownership-404); the real-api+stub-LLM path is manual/nightly. Ports: web 3100 (never 3000), fake-api 8081, stub-llm 8099. **Verify:** `pnpm test` 118 green; typecheck/biome clean; negative control proven (break chip href → 3 component tests RED → revert GREEN); Playwright 4/4 headless green on chromium. Two spec/harness defects found+fixed in-phase (fix-forward): (i) `getByRole("alert")` matched Next's route-announcer too → scoped to the validation text; (ii) `makeUser()` used the reserved `.test` TLD which real pydantic `EmailStr` 422s → `.com`. Cold-start hardened: a genuinely-cold `pnpm e2e` (`.next` wiped) flaked on 30s nav timeouts under parallel workers' simultaneous on-demand compiles → `workers:1` in CI + raised nav/test/webServer timeouts → cold run now 4/4 in 18.9s, no retries. Gate (light, web-only): harness security review PASS — no committed secrets (stub keys are literal `stub-key-ignored`/`di-test`), stubs bind 127.0.0.1 test-only, no prod-path contamination, no new `dangerouslySetInnerHTML`. Deviation: added the `SERMON_API_LLM_BASE_URL` api knob (test-harness-only, default None) — the only non-web change.)
- [x] Phase 26 — Doc-rot sweep (README status, shipped-gate phrasing, :3000 workaround) (completed 2026-06-19, branch `phase-26/doc-rot`, docs-only — no code, no tenant gates. Fixed stale point-in-time claims that survived past the phases they described: README.md headline status flipped from "Phase 0 (repo skeleton)" to "v0 complete; v1/v2 work landed (through Phase 43 merged)", the "Quick start (when phases land)" heading + per-command "Phase N+" annotations + the "only some of these commands work yet" caveat dropped (all listed commands — phases 1/2/4/10/15 — have landed), and the pandoc note reframed to include the Phase 43 .docx round-trip; CONTRIBUTING.md "/test-isolation … Skill ships in Phase 3" and "/check-tenant-leak … Skill ships in Phase 6" stripped of the shipped-future qualifier (both skills live under `.claude/skills/`); root AGENTS.md dropped the "(Phase 6)"/"(Phase 3)" phase-tags on the now-shipped `/check-tenant-leak`, `tenant-auditor`, and `make test-isolation`; web/AGENTS.md promoted the memory-only dev-box port guidance into the committed Toolchain section — the :3000 conflict, the `pnpm dev --port 3001` workaround, and the "never `pkill -f 'next dev'` unqualified" rule. docs/PHASES.md append-only history/spec left untouched per the spec's grep accounting (every "Phase 0"/"ships in Phase" hit there is historical changelog or this phase's own quoted spec text). Deviations: none.)
- [x] Phase 27 — Structured logging + metrics + error tracking (completed 2026-06-19, branch `phase-27/observability` — structured logging via `structlog` (JSON renderer, single-line records carrying `correlation_id` + `level` + `timestamp`), bridged from stdlib logging through a `foreign_pre_chain` where `ExtraAdder` runs BEFORE a deny-list redaction processor so secrets in `extra={}` are scrubbed; `log_stage` fields carry only `book_id`/upload-`filename`, never JWT/password/DSN/broker-URL/API-key/email/query/body. Correlation-id: inbound `X-Correlation-ID` is bounded (`<=200` chars, `isprintable()` blocks CRLF header injection) and echoed verbatim, absent → fresh uuid4 minted + echoed, bound to structlog contextvars so in-request logs carry it (known bounded gap: an UNHANDLED 500 does NOT echo the header — Starlette's `ServerErrorMiddleware` wraps outside `CorrelationMiddleware`; the latency histogram still records the 500 via `finally`, and the id is still bound for logs; all handled 2xx/404/502 responses echo). Metrics: Prometheus `/metrics` (text exposition) exposes four families — `sermon_api_request_duration_seconds` (per-route histogram), `sermon_retrieval_stage_duration_seconds` (per-stage embed/dense/sparse/rerank/highlight/llm), `sermon_retrieval_degraded_total` (degraded counter, pairs with Phase 22's `degraded` flag), `sermon_celery_queue_depth` (Redis LLEN gauge, fail-soft non-500 on scrape error); all labels are static low-cardinality (stage/arm/queue + route TEMPLATE/method/status) — no `user_id`/`book_id`/query/raw-path as labels. Sentry: off-by-default (silent no-op when DSN empty), `send_default_pii=False` on both api (Starlette+FastAPI integrations) and worker (Celery) inits, with a `before_send` deny-list scrubber. Redaction guarantee: deny-listed structured keys (DSN/password/token/etc.) → `[REDACTED]` before JSON render and before Sentry transmission; the metrics Redis-scrape `exc_info=True` path was empirically verified not to leak the broker password. Gate: PASS — no critical/high PII or secret leaks, tenant scoping unchanged (search.py instrumentation is purely additive timing/`degraded` wrappers around already-`book_ids`-scoped calls; Milvus `book_id in [...]`, BM25 `book_id = ANY(...)`, and `user_library` JWT-`user_id` scoping intact). Verify: api `pytest` 326 passed, worker keyless 208 passed/35 live-skipped; `/metrics` 200 + all four families confirmed via live TestClient. Deps added: api `structlog`, `prometheus-client`, `sentry-sdk[fastapi]`; worker `structlog`, `sentry-sdk[celery]`. Deviations: (i) the unhandled-500 correlation-id-header gap above, with the 5xx header-echo path left untested (existing test pins only 404); (ii) `make test-isolation` skips under the keyless posture (no live Milvus) — the static diff proves no query-shape change. Deferred to a live operator trace: full `/search-summary` with a real DeepInfra key, and live Sentry event delivery to a real DSN.)
- [x] Phase 28 — Backup + restore tooling (Postgres, Milvus, MinIO) (completed 2026-06-15, branch `phase-28/backup-restore` — losing the box no longer means losing everything; M3 phase pulled forward to protect the now-irreplaceable manuscripts. **`make backup` / `make restore`** (infra/scripts/{backup,restore,lib}.sh): Postgres via `pg_dump -Fc` in the container; **Milvus 2.6 via the official zilliztech/milvus-backup v0.5.16** (sha256-pinned static binary, one-shot on the compose network — the supported consistency-aware path: per-collection GC pause + flush + binlog copy, restore re-imports through Milvus with `--drop_exist_collection --rebuild_index`; raw etcd-snapshot+bucket-copy is documented as a restore-fragile fallback only); MinIO originals via `mc mirror`. **Load-bearing gotcha handled:** milvus-backup writes its output INTO the MinIO bucket (inside the volume `make nuke` destroys), so backup.sh mirrors it OUT to the host BACKUP_DIR and deletes the in-MinIO copy; restore mirrors it back into a fresh MinIO first. Artifacts → host `infra/backups/` (gitignored, env-overridable BACKUP_DIR, NOT a volume — survives nuke), timestamped with a `latest` symlink + MANIFEST; off-box rsync documented. Security hardening in-build: backup.yaml carries no MinIO creds (injected at runtime via `--set`), mc creds passed via `-e` env not interpolated into `sh -c` (argv/shell-injection vector closed), binary + mc/alpine images pinned, no `set -x`/cred echoing. **Gates:** security-review PASS (zero committed secrets/artifacts — `git check-ignore` verified against two real on-disk backups containing the pg dump's password_hash + originals PII; creds only from infra/.env, never logged; restore hardcodes the compose container/services so it can't target a wrong host), correctness review PASS (all three stores captured, host BACKUP_DIR outside nuke's reach, restore order PG→MinIO→Milvus with exactly one `make up` after nuke, shellcheck clean). **THE DRILL, actually run on the live stack — PASS:** backup-FIRST with verified artifacts (pg dump 3.2 MiB/all 11 tables, milvus 17.3 MiB/27 files/3754 rows, originals 32.3 MiB/13 files) + a pre-nuke inventory, then `make nuke` (destroyed all 5 volumes; backup survived on host ZFS) → `make up` (verified truly empty) → `make restore` → **POST-RESTORE byte-identical to PRE-NUKE**: users=4, global_books=11, chunks=3754, Milvus library_vectors=3754, user_library=13 (per-user counts + full book list all matching); witness Augustine Confessions recovered 192 chunks + 192 vectors; booted api → `/search` "our heart is restless…" found the witness (score 0.89, full dense+sparse+RRF+rerank, degraded=[]); negative control empty-library user got 0 hits; **`make test-isolation` 3/3 AFTER restore** (book_id partition-key tenant scoping survived recovery). Restore completed on the first attempt — no recovery path needed. NOTE: `make backup`/`restore` live at the repo-root Makefile (not infra/Makefile — the root is where `make up`/`nuke` already live). The seeded corpus is intact post-drill.)
- [x] Phase 29 — App Dockerfiles + image-build CI (completed 2026-06-19, branch `phase-29/app-images`. **Prompt staleness reconciled (like Phase 17):** the title's "models baked, HF offline" is DEAD post-16b/ADR 0006 — there are no local model weights, no torch import, no HF cache to bake or set offline flags for; embeddings/rerank are remote DeepInfra and the LLM was already remote (ADR 0005). The ONLY baked corpus is NLTK WordNet+omw (dedup), already extracted in worker/Dockerfile; both api+worker Dockerfile headers already state "NO model weights live in image". So this phase reduced to: close the api image's real docx gap, confirm worker/web images, and add the missing image-build CI. **Images (all 3 multi-stage, pinned bases — no `:latest`, run as non-root where practical):** (1) **api** (root context; imports worker.db + worker/convert*) gained the Phase 43 .docx round-trip deps it was missing — pandoc + a pinned Node 22 (NodeSource via curl/gnupg, purged after) + a hermetic `npm ci --omit=dev` of the committed `worker/convert_node/{package.json,package-lock.json}` in a dedicated `node_deps` stage (host node_modules is stripped by `.dockerignore **/node_modules`, rebuilt clean in-image), plus a `HEALTHCHECK` against **/readyz** (NOT /healthz — only readyz flips on Postgres down, per Phase 18) using the venv Python (no curl in the runtime layer); non-root `appuser` uid 10001; pypandoc was already in api/uv.lock. (2) **worker** already correct (slim multi-stage, uv-pinned, pandoc/libmagic/ca-certs, WordNet baked+extracted, Celery CMD) — added Node 22 + `npm ci --omit=dev` of convert_node (AGENTS.md lists the bundle as a worker dep; skipping would be a deviation); compose's `celery inspect ping` is the worker liveness probe (no Dockerfile HEALTHCHECK, by design). (3) **web** already correct and UNCHANGED in substance: Next 15 `output:"standalone"` (next.config.ts already set), node 20-slim builder+runner (NOT bumped to 22 — Next env gotcha), pnpm 9 pinned explicitly (no packageManager field per web/AGENTS.md), frozen-lockfile, `API_BASE_URL` stays a RUNTIME env. **CI — new `.github/workflows/images.yml`** (ci.yml + codeql.yml left intact): a `changes` job emits api/worker/web booleans via a hand-rolled `git diff` changed-path gate (matching ci.yml's no-third-party-action convention; fails OPEN — packaging isn't a security gate), then 3 build jobs each gated on its flag. **pull_request = BUILD-ONLY** (`push: false`, no GHCR login, no secrets → fork PRs stay green; the login + push steps are `if: github.event_name != 'pull_request'`). **push to main = BUILD + PUSH to GHCR** via the built-in `GITHUB_TOKEN` (no PAT) with `permissions: packages: write`, tagging each image `latest` (default-branch-only) + the commit `sha` via docker/metadata-action; build contexts are root for api/worker (`-f api/Dockerfile .`) and `web/` for web; gha build cache per scope; `concurrency` cancel-in-progress. **GHCR names hardcoded lowercase** (`ghcr.io/sovitxyz/sermon-{api,worker,web}`) because the owner `sovITxyz` is mixed-case and GHCR rejects un-lowered names. **`.dockerignore`:** no functional change — added a clarifying comment that the `**/node_modules` glob is INTENTIONAL (strips host convert_node node_modules so the image rebuilds a clean platform-correct copy via npm ci; the committed package.json/lockfile sit beside it and arrive via `COPY worker/`); verified convert_node source + reference.docx survive the filter. **Gates / verify:** actionlint clean on all workflows (run via docker); images.yml YAML parses; **local `docker build -f api/Dockerfile .` at root context succeeded** and a **real .docx round-trip ran IN the built container** — pandoc 3.1.11.1 + Node v22.23.0 + @tiptap present, reference.docx baked, JSON→docx(9729 bytes)→JSON preserved the heading structure (the Phase 43 fidelity contract, now proven shippable). **Deviations:** (i) title staleness as above — flipped with the Phase-17-style reconciliation note; (ii) worker+web Dockerfiles were edited-not-rewritten (only api had a real gap); (iii) the `web/next-env.d.ts` host-side `pnpm build` regen was reverted before commit (Next rewrites the routes ref line — harmless in the hermetic image build). **Operator action at merge:** the first push to main creates the three GHCR packages under `sovitxyz`; set them to the desired visibility (private by default).)
- [x] Phase 30 — KEDA + k8s manifests gated on /readyz (completed 2026-06-19, branch `phase-30/k8s-keda` — provider-portable **raw manifests + kustomize** (`infra/k8s/base/` + `overlays/prod/`) for `api`/`worker`/`web` on the Phase 29 GHCR images (`ghcr.io/sovitxyz/sermon-{api,worker,web}`); chose raw-manifests-over-Helm (no chart/ADR needed at this scale — kustomize base+overlay gives the env split). **Probes:** api readiness=`/readyz` (held NotReady while Postgres/Milvus/Redis are down, per Phase 18) + liveness=`/healthz` (dependency-free, stays green through dep outages); web probes `/`; worker liveness is `celery inspect ping` exec (no readiness — no Service routes to it). **KEDA:** a `ScaledObject` autoscales the **worker** Deployment only on Redis Celery-queue depth (§2 locked decision — scale workers to zero), `min=0`/`max=10`, `cooldownPeriod 300s ≥ broker visibility_timeout`; the Redis password is pulled via `TriggerAuthentication` from the Secret (no inline secret). **Secrets posture:** `SERMON_API_JWT_SECRET` required and sourced from the `sermon-secrets` Secret via secretRef (api boot guard refuses prod start when unset or == DEV_JWT_SECRET); `SERMON_API_LLM_PROVIDER=deepinfra` pinned (dodges the google-default 503 trap); `secret.example.yaml` is REPLACE_ME placeholders only, excluded from the kustomization, and `.gitignore` blocks real `secret.yaml`/`*.secret.yaml` — no real secret values committed. **Exposure:** api is ClusterIP-only (its unauthenticated `/metrics` is NOT internet-reachable), worker has no Service, web is ClusterIP + a TLS Ingress routing only `/` → web:3000; a `migrate-job` runs Alembic before rollout. **Pod security:** pod-level `runAsNonRoot` + `seccompProfile RuntimeDefault`, container `allowPrivilegeEscalation:false` + `drop: [ALL]`, no privileged/hostNetwork/hostPath, resource requests on every container; prod overlay pins immutable `:sha` image tags. **Verify — PROVEN, not deferred:** the build env had only docker, but docker+network let me install kustomize 5.4.3 + kubeconform 0.6.7 + kind 0.23 + kubectl 1.30 and run a **FULL live cluster test** rather than deferring to the operator runbook. `kustomize build` renders base+prod cleanly (10 resources each, Secret/Job excluded), `kubeconform -strict` validates 13/13 (Skipped:0, incl. KEDA CRDs against real kedacore schemas), and on a **live kind cluster with KEDA 2.14** all four acceptance criteria PASSED: worker scales 0→10 on a celery-list burst (capped at maxReplicaCount), holds the 300s cooldown then scales to 0, a dead-dependency rollout (`/readyz` 503) is held NotReady while the old Ready pod keeps serving, and liveness (`/healthz`) stays green with 0 restarts through the outage. Gate PASS (no critical/high on the k8s surface). **Residual (medium/low, non-gating, in `infra/k8s/README.md`):** no default-deny NetworkPolicy (in-cluster lateral access to api:8000 incl. /metrics is unrestricted — the 'only web reaches api' precondition is enforced by Service type, not the network), `readOnlyRootFilesystem:false` on api/web, base manifests use `:latest` (mitigated by the overlay sha pins). **Deviations:** raw-manifests-over-Helm (kustomize, no chart/ADR); live kind+KEDA test run despite a docker-only base env. LAST v1 phase — v1 hardening tail complete.)

v2 — Sermon Workflow (planned 2026-06-10 from the v2 Product Backlog; see the **v2 Plan —
Sermon Workflow** section below for the execution model, ordered queue, and per-phase
prompts. Milestones: M4 Read 31–33 · M5 Write 34–37 · M6 Schedule 38–42 · M7 Round-trip 43–46):

- [x] Phase 31 — Originals persistence: stop losing uploads (completed 2026-06-11, branch `phase-31/originals-persistence` — first v2 phase, proof of the autonomous execution model. minio-py `>=7.2,<8` over boto3 (py.typed → zero pyright relaxations, small dep tree, plain S3 so the R2/B2 swap stays endpoint+credentials); fail-loud posture on both write paths (`OriginalsStorageError` propagates — durability is the point, log-and-continue is silent data loss); new books upload BEFORE the `global_books` txn so a stored pointer never dangles (orphan-object-on-crash accepted — same posture as the Phase 9 Milvus orphan-vector window); dup-hits backfill only `WHERE text_pointer IS NULL` — race-safe, never overwrites, never duplicates an object, and is the only recovery path pre-31 books will ever get. 15 new tests (sanitization/key-shape pure units + live-gated MinIO round-trip/never-overwrite per the repo skip-guard pattern, all 15 live-green on the dev box); worker suite 72 passed/14 pre-existing env-gated skips; `make test-isolation` 3/3 ×2 (build + tenant-auditor runs); `/check-tenant-leak` grep sweep clean; tenant-auditor PASS with 2 forward notes (the second-uploader's sanitized filename becomes the shared object key — treat as non-authoritative and re-run tenant gates when the B1 read tier lands; the hand-maintained sanitizer mirror with `api/uploads.py` has no equality test guarding drift); `/security-review` no findings (sub-threshold provenance note: backfilled bytes come from a MinHash-similar re-upload, not byte-identical — decide the authoritative-bytes policy when reads land). Deviations: (i) the never-used `text_pointer` pass-through kwarg removed from `ingest()`/`ingest_markdown` — the key is derived, single write site; (ii) sanitizer adds a 255-char cap beyond the exact api mirror (S3 key-length guard); (iii) `infra/.env.example` appended via python one-liner — shell redirection is permission-denied on `.env*` paths.)
- [x] Phase 32 — Reader API: windowed chunks + reading positions (completed 2026-06-11, branch `phase-32/reader-api` — migration **0005** (`reading_positions`; prompt assumed 0004, scooped by `0004_upload_tasks`, renumbered per the race rule). In-phase decisions per the close-out contract: **offset_ratio IN** the first cut — nullable `Float`, validated 0.0–1.0 at the PUT model (no DB CHECK; schema has none anywhere), one migration instead of a second migration-bearing phase later; **chunk_count COMPUTED per request** — no denorm onto `global_books`, no backfill/drift, `ix_chunks_book_id` existed since 0002 so no new index; no separate `reading_positions` index — the `uq_reading_positions_user_book` backing btree covers the doubly-scoped lookup and the `/library` join prefix. Endpoints: `GET /books/{book_id}/chunks?start&limit` (default 40, limit>100 silently capped per the Verify wording — documented divergence from SearchRequest's le=100-422; `start` is a chunk_index lower bound, not OFFSET — deep-link-friendly for Phase 33 and rides `uq_chunks_book_chunk`), `GET/PUT /books/{book_id}/position` (pg `ON CONFLICT ON CONSTRAINT` upsert, full-replace semantics — omitted offset_ratio stores NULL, ratio is chunk-relative; empty position = 200-with-nulls per the TaskStatusResponse precedent, 404 reserved exclusively for the ownership gate), `GET /library` + `chunk_count`/`last_chunk_index`/`progress` (=(last+1)/count clamped to 1.0, all default-None → Phase 15 web table unbroken). Tenant posture: non-UUID/nonexistent/non-owned collapse to byte-identical 404s on all three routes (uploads.py precedent), gate-before-read pinned via executed-kind log, the B1 join trap pinned VERBATIM in test_library_unit.py (`ON reading_positions.user_id = user_library.user_id AND …book_id`). Gates all PASS: tenant-auditor (JWT-only user_id provenance end-to-end), `/check-tenant-leak` (4-step sweep, 550 grep lines reviewed), `/security-review` (NaN/Inf offset_ratio rejected by ge/le — empirically verified; no injection, no oracle), schema-reviewer (additive-only, no locking risk, zero model↔migration drift, downgrade real). Live: alembic 0003→0005 + downgrade/re-upgrade round-trip on compose Postgres; `make test-isolation` 3/3 ×2; worker 142 passed/21 env-gated skips; api 187 passed (+35: 24 new test_reader_unit.py, 11 extended); two-user join trap checked LIVE — 27/27 (shared deduped book, no position bleed either direction, PUT-twice = exactly 1 row, exact-rows cleanup). Deviations: (i) `api/pyproject.toml` pyright include gained `library.py` — pre-existing gap, it was never directly type-checked (checks clean) — plus new `reader.py`; (ii) `ReadingPosition` exported from `worker/db/__init__.py` per the UploadTask precedent. Forward notes (sub-threshold, → Phase 36 with the planned autosave rate bucket): int32-overflow `chunk_index`/`start` (≥2^31) → 500 not 422; PUT accepts chunk_index beyond the book's actual length (clamped only at display by `_progress`).)
- [x] Phase 33 — Reader UI: /read/[bookId] windowed scroll + entry points (completed 2026-06-11, branch `phase-33/reader-ui` — react-markdown `^10.1.0`, NO rehype-raw: raw HTML renders as inert literal text (verified in the installed package source — hast text nodes, `defaultUrlTransform` strips `javascript:`/`data:` hrefs), img stubbed to an alt-text `<span>` (no element, no fetch), links `rel="noopener noreferrer"`; `dangerouslySetInnerHTML` still ZERO in web/ source. Proxies per the tasks/[taskId] exemplar with one documented deviation: 404s pass through BYTE-IDENTICAL via `lib/http.ts passthroughResponse` (preserves the api's no-existence-oracle body; all other errors keep the `{error}` re-wrap); PUT body rebuilt from scratch by `whitelistPositionUpdate` — proven load-bearing LIVE (smuggled `user_id`+`evil` keys: proxy 200-with-drop vs direct-api 422 extra_forbidden). Reader: document-scroll windowing, IO sentinels rootMargin 600px recreated per merge, prepend requests the EXACT gap (never a 422), scrollTop compensation via useLayoutEffect delta re-anchor (Safari lacks overflow-anchor), single-flight + epoch guard against citation re-targets; ?chunk=N parsed strict `/^\d+$/` safe-integer (else saved-position), anchor lands `max(0, N−5)` with bg-blue-50 tint fading at 2400ms; debounce 1500ms scroll-settle + pagehide keepalive flush + effect-teardown flush (SPA navs never fire pagehide) guarded by `container.isConnected`; offset_ratio ALWAYS sent (3-decimal visible fraction — full-replace can never silently clear it), `shouldPersist` epsilon 0.01, lastSent advances only on res.ok; no virtualization (plain DOM per the B1 ~600-block budget, ReaderChunk memo'd). Entry points: citation-card "Read in context" via `readHref` (chunk 0 deep-links correctly), library-row progress + "Continue reading" (nullable-safe `formatProgress`); middleware matcher gains `/read/:path*`. Gates all PASS: /security-review (protocol-sanitization verified in installed source; CVE-2025-29927 N/A on Next 15.5.19; open-redirect guarded by `safeRedirectPath`; CSRF mitigated SameSite=Lax+JSON), web quality gate (typecheck/lint clean, 92/92 vitest, zero litter, zero Python imports, zero client-side api-host refs), contract-conformance (field-exact vs api/reader.py+library.py incl. start-is-lower-bound math and full-replace honor). Live e2e on real api+web servers: 9/9 (unauth 307 → /login?next=…, SSR 200, chunks window [0,1,2], PUT/GET position round-trip incl. omitted-ratio→NULL, whitelist live-proof, 404 cmp-byte-identical), exact-row cleanup. 12 commits. Deviations: (i) `web/AGENTS.md` pure-file list refreshed (predated `summary.ts`, gained `reader.ts`/`reader-view.ts`/`library.ts`); (ii) `next-env.d.ts` rewritten by a `pnpm build` probe, restored via git checkout per its documented intent. Sub-threshold notes: bookId `..` dot-segment collapses the upstream path (not exploitable — fixed origin, caller's own bearer, no aliased route) — encode dots if a route ever nests deeper; per-user proxy responses set no explicit Cache-Control (matches every existing proxy; no shared cache in the stack) — set `private, no-store` repo-wide if a CDN ever fronts prod; `?next=` drops the query string so a logged-out ?chunk=N deep link loses its anchor after login (UX only). Manual-QA ledger (browser-event wiring; logic is unit-covered): IO sentinel firing/re-observe, Safari prepend re-anchor, settle-timer/pagehide/teardown event wiring, anchor scrollIntoView+tint visuals, rendered inertness of hostile HTML, link click-through.)
- [x] Phase 34 — Documents schema + API CRUD (completed 2026-06-15, branch `phase-34/documents-api` — storage + API half of the sermon editor (B2 slice A), **begins Milestone M5** so the GCP-OAuth operator reminder was fired this session. **Migration 0006** (`documents`, down_revision 0005): `document_id` UUID PK, `user_id` FK→users CASCADE, `title`, `content` JSONB (ProseMirror/TipTap JSON — first JSONB in the schema), server-derived `content_text` Text, `schema_version` int (server `DEFAULT 1`, app constant `SCHEMA_VERSION=1` authoritative), `created_at`/`updated_at` (server_default now(), NO onupdate — PATCH bumps `updated_at` explicitly so it reads back for the optimistic-concurrency gate), `deleted_at` timestamptz NULL soft-delete; first descending index `ix_documents_user_updated (user_id, updated_at DESC)` via `sa.text('updated_at DESC')`. **api/documents.py:** POST create / GET list (non-deleted, **preview-only** — first `PREVIEW_CHARS=280` of content_text, no `content` key) / GET full / PATCH (partial title|content, `base_updated_at` REQUIRED → 409 on mismatch, single-author optimistic concurrency) / DELETE (soft) / POST `/{id}/restore` (clears `deleted_at`, idempotent on active). All five statement builders gate `Document.user_id == JWT user_id`; non-owned/nonexistent/soft-deleted collapse to byte-identical 404 (no existence oracle, Phase 20 posture); `extra="forbid"` makes smuggled `user_id`/`content_text`/`schema_version` a hard 422; `content_text` ALWAYS server-re-derived (never client-supplied) by `derive_content_text` (a pure ProseMirror-JSON→text walk). 2 MB cap (`MAX_CONTENT_BYTES`) measured on serialized `content` → 413. **Gates:** tenant-auditor PASS, /check-tenant-leak PASS, schema-reviewer PASS (additive table, brief FK lock, real ordered downgrade, zero model↔migration drift), make test-isolation 3/3, live migration 0006 up/down/up round-trip, full curl Verify matrix + cross-tenant ownership 404s live, api suite 256 passed. **security-review: round-1 FAIL → round-2 PASS** — round 1 caught a real DoS (recursive `derive_content_text` blew Python's 1000-frame limit on a deeply-nested ~11KB doc under the 2 MB cap → 500); fixed in-scope (`3da318d`) by rewriting the walk ITERATIVE (explicit `_Frame` stack, post-order DFS, output byte-identical — verified 0 mismatches / 4000-tree fuzz, 20000-deep doc returns correctly where the old code crashed); deep-nesting + equivalence tests added. Deviations: (i) `api/pyproject.toml` pyright include gained `documents.py`; (ii) `Document` exported from `worker/db/__init__.py` per the UploadTask precedent. Forward note (sub-threshold, recorded in api/AGENTS.md): the 2 MB cap is enforced in-handler after Starlette buffers the whole body — a pre-deserialize/global ASGI body-size limit is future cross-cutting hardening, not Phase 34 scope.)
- [x] Phase 35 — Editor shell: /sermons + TipTap (completed 2026-06-15, branch `phase-35/editor-shell` — the manuscript-editor UI half of B2 slice B atop the Phase 34 documents API. **TipTap MIT core ONLY** (`@tiptap/react`/`@tiptap/pm`/`@tiptap/starter-kit`/`@tiptap/extension-placeholder`, all `^3` — TipTap 3 is current `latest`, MIT, React-19/Next-15 compatible; **never a Pro extension**, B2); `StarterKit.configure({ link: false })` this phase (no link UI; links/citations are Phase 37). **First `next/dynamic` in web/:** `components/SermonEditor.tsx` is dynamic-imported with `ssr: false` from the route client shell (`app/sermons/[documentId]/SermonEditorShell.tsx`) so the TipTap+ProseMirror chunk loads ONLY on the editor route — `ssr:false` is doubly required (`useEditor` runs `immediatelyRender:false` per the App Router rule + ProseMirror touches the DOM). The server shell (`page.tsx`) fetches the full doc server-side via new `lib/api-server.ts:getDocument` (bearer stays server-side; `UnauthenticatedError`→`/login`, the API's uniform 404 → in-place not-found via new `DocumentNotFoundError`, no existence oracle) and passes it down, so the dynamic import defers the editor CODE not the DATA. Editor: fixed toolbar (bold/italic/H2/H3/bullet+ordered lists via `useEditorState` active-state selector), editable title `<input>`, **EXPLICIT Save only** (NO autosave — Phase 36): PATCHes `{title, content: editor.getJSON(), base_updated_at}` through the same-origin `/api/documents/[id]` proxy (whitelists exactly those three — `lib/documents.ts`); on 200 adopts the returned `updated_at` as the new in-memory `base_updated_at` (a `useRef`, so a same-tab second save is never a false self-conflict); on 409 a **non-destructive** inline error that KEEPS the user buffer (reload/merge UX is Phase 36); 413/404 get their own messages. **ZERO `dangerouslySetInnerHTML`** (repo invariant) — TipTap renders its own DOM, content round-trips as JSON; list previews stay PLAIN TEXT. Middleware matcher + shared nav already carried `/sermons` (Phase-35 proxy/gate builder). **Gates:** `pnpm typecheck` clean, `biome check` clean, `pnpm test` 137 passed (+6 new `SermonEditor.test.tsx` — TipTap mocked since ProseMirror needs DOM-measurement APIs jsdom lacks; asserts the Save proxy body incl. `base_updated_at`, the 200 base-advance, the 409 non-destructive error, 413). E2E: extended `e2e/support/fake-api.mjs` with the `/documents` endpoints (create/list-preview-only/full-GET/PATCH-with-409-on-stale-base/soft-DELETE, uniform 404, strictly-monotonic `updated_at` counter for deterministic concurrency) + `e2e/editor.spec.ts` editor smoke (create→type→Save→reload-persists; stale-tab Save→409 inline error). Decisions recorded in `web/AGENTS.md`. Deviations: (i) **TipTap 3 not 2** — the scout brief/pre-made decisions assumed v2 (~100kB, standalone Placeholder); v3 is now `latest`, still MIT + React-19-compatible, and `@tiptap/extension-placeholder@3` still ships as a standalone package re-exporting `Placeholder` from `@tiptap/extensions`, so the spec's exact four package names install unchanged; (ii) the editor module lives at `components/SermonEditor.tsx` (per the pre-made `dynamic(() => import('@/components/SermonEditor'))` decision) with the dynamic-import shell in the route folder; (iii) added `DocumentNotFoundError` to `lib/api-server.ts` so the editor shell renders not-found in place rather than redirecting. **Verify (all PASS):** `/security-review` of the cookie-forwarding surface PASS (bearer server-only, write-whitelists rebuild the body dropping every server-owned field, byte-identical 404/409/413 passthrough, documentId encodeURIComponent'd, zero `dangerouslySetInnerHTML`); web-quality PASS; contract-conformance PASS (field-exact vs api/documents.py). Cookie-jar live drive 7/7 on the real api+web — incl. smuggled `user_id`/`content_text`/`schema_version` DROPPED on both create and PATCH (success not 422, server values authoritative), content JSON round-trips byte-exact, stale base_updated_at → 409 passthrough, unauth `/sermons` → 307 `/login`, list preview-only. Playwright 6/6 headless (after fixing `editor.spec.ts` stale-409 assertion — the same bare-`getByRole('alert')` route-announcer ambiguity as Phase 25, now scoped to the conflict-message text + a `web/AGENTS.md` E2E note added to prevent a third recurrence). No DB/Milvus queries touched — API tenant gates ran in Phase 34.)
- [x] Phase 36 — Editor autosave + conflict + soft-delete UX (completed 2026-06-15, branch `phase-36/editor-autosave` — B2 slice C, pure web UX on the Phase 34/35 surface; **NO api change** (see limiter below). **Autosave** mirrors the proven Phase 33 reader-position pattern in a new pure `lib/sermon-autosave.ts`: 2000ms debounce + 15000ms max-interval (first dirty edit arms the ceiling so continuous typing still saves), a `FlightState` machine guaranteeing ONE in-flight PATCH (mid-flight edits coalesce to exactly one trailing save — never parallel writes that would race the base), after every 200 the response `updated_at` is adopted as the next `base_updated_at` (stale base → spurious 409s), a dirty check so an unchanged buffer never PATCHes. Explicit Save button REMOVED — autosave is the only mechanism; `SaveStatus` = saved/saving/unsaved/error/conflict (aria-live span + `data-save-status` hook). **pagehide keepalive flush** (also on unmount, since SPA nav never fires pagehide) only when dirty AND serialized body ≤ 65536 bytes; an oversize doc SKIPS silently (next-open save covers it) — never throws on close. **409 conflict** → status=conflict, autosave loop STOPS (a `conflicted` ref gates every scheduler entry), an amber banner offers "Reload latest" (re-GET → `setContent` + reset base + resume); the user buffer is kept until they choose — never auto-clobbered. **Delete/restore** on `/sermons`: `SermonList` became a client island (a server component can't mutate; `router.refresh()` re-runs the server list after each call), `window.confirm`-gated delete (manuscripts are irreplaceable), and an **undo toast** as the restore reachability (an `<output>`/role=status — deliberately not role=alert, which Next's route-announcer owns — chosen over a "recently deleted" view because the API list is non-deleted-only and a list-deleted endpoint would be an api change, out of scope); DELETE treats 204+404 as "gone", a failed restore keeps the toast for retry. **Limiter (Phase 19):** the rate-limit scout + a dedicated re-check confirmed PATCH /documents has NO per-user bucket (only signup/login/summary are throttled) — sustained ~1 PATCH/2s autosave is already unthrottled, so 429 is impossible by construction and NO api change was made (recorded in web/ + api AGENTS.md); a 40-rapid-PATCH live hammer confirmed zero 429s. **Gates:** web-quality PASS (tsc/biome/vitest 169 — +17 sermon-autosave pure-module tests, +12 SermonEditor, +9 SermonList), autosave-correctness review PASS (single-flight, base adoption, 409 stop+preserve, keepalive guard all traced). **Verify:** Playwright 9/9 headless; real-api contract 11/11 (base advance, stale-409, >64KB accepted by api, delete-204/get-404/restore-intact); browser-verified 9/9 — 81 keystrokes coalesce to 0-during + 1 trailing PATCH, the 15s ceiling fires once under never-settling typing, the two-tab 409 banner + Reload-latest recovery, the keepalive flush persisting an un-debounced edit across reopen, the >64KB skip. Fix-forward in-phase: removing the Save button staled `editor.spec.ts` (it clicked a gone button — the 3rd E2E-lags-UI recurrence this run); migrated both specs to wait on `data-save-status` + drive the 409 via stale-tab typing (`83cda1e`). Deviation: explicit Save removed entirely (autosave-only per B2); restore is an in-session undo toast, not a deleted-docs view (avoids an out-of-scope api endpoint).)
- [x] Phase 37 — Citation node + insert-from-search (completed 2026-06-15, branch `phase-37/citation-node` — **B2 slice D, completes the sermon editor**: cited library passages are now first-class manuscript blocks deep-linking into the reader. **Citation node** (`components/editor/CitationNode.tsx`): a block-level TipTap ATOM (`atom:true`, group block, `@tiptap/react` v3 re-exports — no new dep, MIT preserved) with attrs `{bookId, chunkIndex, bookTitle, snippet, parentSection?}`, each mapped to a `data-*` attr via parseHTML/renderHTML so it round-trips through `documents.content` JSON on getJSON/setContent (verified with a real headless Editor AND confirmed in live Postgres — the saved doc holds `{type:"citation", attrs:{…}}` with the snippet cached at insert). The node view renders a card mirroring the /search citation styling, snippet as PLAIN TEXT (zero `dangerouslySetInnerHTML`), an owned card links `/read/{bookId}?chunk={chunkIndex}` (Phase 33). **Raw-hit field mapping** (a /search hit has no `snippet`/`bookTitle`): `snippet←content_chunk`, `chunkIndex←metadata.chunk_index`, `parentSection←metadata.parent_section`, `bookId←book_id`; **bookTitle has no hit source** so it's resolved from the one-shot `/library` fetch the shell already does. **Degraded state** without any per-citation fetch: the editor shell fetches the owned-book set ONCE on open (`getLibrary`, passed RSC→shell→`LibraryMembershipProvider` context — a Set can't cross the RSC boundary so book_ids cross as a string[] and the set is rebuilt client-side); a node whose bookId isn't in the set drops the link and shows an amber "No longer in your library" badge with the cached snippet — live-verified ZERO browser `/api/*` calls on a degraded reopen (only the single library fetch). **LibraryDrawer** (`components/editor/LibraryDrawer.tsx`): in-editor search via a NEW thin `web/app/api/search/route.ts` proxy → POST /search (RAW hybrid hits, NO LLM — live-timed ~16–22s vs the ~134s /search-summary path, response carries no summary); clicking a hit `editor.chain().insertContent({type:"citation", attrs})`. **Proxy** whitelists `{query}` ONLY (drops smuggled `user_id`/`book_ids`/limit/rerank — rebuilt from scratch), cookie→bearer server-side, own-JSON 401 when unauthenticated, 502/504 on upstream wedge; adds NO unscoped query (POST /search still resolves the library from the JWT, `extra=forbid`). **Gates (full set — new tenant-facing surface): ALL PASS** — make test-isolation 3/3, /check-tenant-leak (no unscoped path), tenant-auditor (no client-header pass-through, structural whitelist, scope preserved), web-quality (tsc/biome/vitest 193 — +9 CitationNode, +9 LibraryDrawer, +6 search-proxy), contract-conformance (SearchHit field-exact vs api/search.py + worker metadata). **Verify PASS:** Playwright 10/10; proxy legs 3/3 (clean / smuggled-dropped / 401); real-browser drive — drawer search → insert → card with title+snippet → autosave+reload persists (data-layer confirmed) → click opens the reader at the chunk; degraded reopen shows cached snippet + badge with zero per-citation fetches. Process note honored: the "update e2e specs when UI changes" rule was in every builder brief — no stale-spec failures this phase. Deviation: `bookTitle` sourced from the library fetch (not on the raw hit); no new TipTap dep (v3 re-exports cover Node/ReactNodeViewRenderer).)
- [x] Phase 38 — Calendar schema + API (completed 2026-06-15, branch `phase-38/calendar-api` — the whole server side of the B3 calendar in one slice; Phases 39-42 are pure web on top. **Migration 0007** (`sermon_events`, down_revision 0006): `event_id` UUID PK, `user_id` FK→users CASCADE, **`event_date` DATE (the schema's FIRST date column — `sa.Date()`, no timezone, no server_default; day-anchored so a UTC-midnight timestamptz can't shift a day for UTC-minus users)**, `title` Text, `series` Text NULL (B3 free-text recurrence label, NOT an RRULE), **`document_id` nullable FK→documents `ON DELETE SET NULL` (the schema's FIRST SET NULL — a real documents-row delete detaches the event; the documents API soft-deletes so the link normally survives)**, `created_at`/`updated_at` (server_default now(), NO onupdate — PATCH bumps `updated_at` explicitly); index `ix_sermon_events_user_date (user_id, event_date)` plain ascending (bidirectional range scan, no DESC trick); **DELIBERATELY no unique on (user_id, event_date)** — two services one Sunday is normal. **api/calendar_routes.py** (file named `calendar_routes.py` NOT `calendar.py` — see deviation i): GET `/calendar/events?start&end` (half-open DATE range `[start, end)` — an event dated exactly `end` is EXCLUDED; `start<=end` else 422; span > **400-day** cap else 422; `event_date` order), POST `/calendar/events` (`extra=forbid` body `{event_date, title, series?, document_id?, repeat_weekly_until?}`; optional `repeat_weekly_until` materializes DISCRETE INDEPENDENT weekly rows from `event_date` through it inclusive, cap **53 rows** else 422; `until < event_date` → 422), GET/PATCH/DELETE `/calendar/events/{event_id}` (PATCH partial `extra=forbid`, ≥1 field else 422, `document_id` three-state via `model_fields_set`: absent=leave / null=detach / non-null=re-link; DELETE is a HARD delete — events are cheap/regenerable, unlike the soft-deleted documents). All queries double-scoped (`event_id` AND `user_id` from the JWT) via module-level `_xxx_stmt` builders (`_range_stmt` half-open `>= start`/`< end` pinned, `_owned_event_stmt`/`_update_stmt`/`_delete_stmt`, `_document_owned_stmt`); non-UUID/nonexistent/cross-tenant `event_id` → byte-identical 404 (no existence oracle, Phase 20 posture). **document_id is attacker-controlled body input**: a non-null `document_id` on POST/PATCH is ownership-checked against the JWT user's `documents` (`_document_owned_stmt`, NO `deleted_at` filter — active OR soft-deleted both acceptable, ownership is what matters) and a miss is the SAME 404 whether the doc is another tenant's or nonexistent (no existence/title oracle) — gate runs BEFORE any write. **Pre-made decisions recorded:** (1) migration 0007/down 0006; (2) range cap 400 days, materializer cap 53 rows (resolves the B3 "GET range cap value" open question); (3) `event_date` + GET `start`/`end` are DATE, half-open `[start,end)`; (4) document_id ownership miss → plain 404, no oracle; (5) FK SET NULL is the real-row-delete defense, documents API soft-deletes; (6) materialized rows are independent (no parent linkage), each PATCH/DELETEs on its own. **Checks (api builder):** ruff (E,F,W,I,B,BLE,TRY,ASYNC,S,ARG,ERA,UP,TID) clean, pyright strict 0 errors, full api suite **283 passed** (+30 new `tests/test_calendar_unit.py`: statement compile-pins, `_weekly_dates` arithmetic, half-open range incl. event-on-`end`-excluded, span>400→422, materializer count + 53-cap→422, each materialized row independently PATCH/DELETE, cross-tenant document_id→404 no-oracle on POST+PATCH, document_id null-detach, smuggled user_id→422, cross-tenant event_id→404); calendar routes live-mount under `PYTHONPATH=../worker`. caps recorded in `api/AGENTS.md`. **Deviations:** (i) **the router module is `api/calendar_routes.py`, not `api/calendar.py`** — a file literally named `calendar.py` is shadowed by the stdlib `calendar` under pytest's `pythonpath=["."]` (the test `from calendar import …` resolved to `/usr/lib/python3.12/calendar.py` → ImportError at collection AND pyright `reportAttributeAccessIssue`); renaming kills the shadow at both import sites and main.py imports it plain (`import calendar_routes`), no alias needed; (ii) `SermonEvent` exported from `worker/db/__init__.py` per the Document/UploadTask precedent (DB builder). **Gates — all PASS:** tenant-auditor (the document_id cross-tenant trap closed — ownership-checked, no-oracle 404), /check-tenant-leak (every statement user_id-scoped), schema-reviewer (additive table, SET-NULL + DATE correct, no unique on user+date, zero model↔migration drift), make test-isolation 3/3, live migration 0007 up→downgrade→up round-trip, full curl Verify matrix 10/10 (half-open boundary incl. event-on-`end`-excluded, span>400→422, 10-row materialize + 53-cap→422, each row independent PATCH/DELETE, cross-tenant document_id→404 no-oracle, cross-tenant event_id→404, FK SET-NULL on a direct documents-row delete). **security-review: round-1 FAIL → round-2 PASS** — round 1 caught a real materializer DoS + 500: the 53-cap was checked AFTER `_weekly_dates` built the full list (a far-future `repeat_weekly_until` builds a huge list before the 422) and `current += timedelta(weeks=1)` overflowed past `date.max` → uncaught 500. Fixed in-scope (`6202cc8`) with an O(1) date-arithmetic count-check BEFORE generation (`(until - event_date).days // 7 + 1 > 53` → 422; `until < event_date` → 422) so no large list is ever allocated and generation never iterates past a valid date (+ a belt-and-suspenders `try/except OverflowError → 422`); +4 tests incl. a monkeypatched-bomb proving generation is unreachable on the far-future path. Final api suite 287 passed. Phase 38 is NOT operator-gated — auto-merged on green CI.)
- [x] Phase 39 — Calendar year + month views, read-only (completed 2026-06-15, branch `phase-39/calendar-year-month` — pure web on the Phase 38 calendar API, ZERO new runtime deps (custom Tailwind CSS-grid; the FullCalendar fallback stays untouched). **Data layer:** `web/lib/dates.ts` — pure YYYY-MM-DD string/numeric helpers (month grids with adjacent-month fill, half-open `[start,end)` year + month ranges matching the API, leap-year-correct, `WEEK_STARTS_ON=0` Sunday constant); NEVER `new Date("YYYY-MM-DD")` (the only `new Date(` uses are numeric `Date.UTC(...)` weekday math, grep-gated to zero string parses under app/calendar + lib/dates.ts). `CalendarEvent` + `CalendarEventListResponse` added to `web/lib/types.ts` mirroring the API verbatim (snake_case, nullable `series`/`document_id`, no `user_id`). Same-origin GET proxy `web/app/api/sermon-events/route.ts` → API `/calendar/events`: cookie→Bearer, structural whitelist of ONLY `start`/`end`, `cache:no-store`; range validation left to the API (one owner). **UI:** `/calendar?view=year|month&date=YYYY-MM-DD` (linkable/deep-linkable), ONE range fetch drives both views; CalendarYear = grid-cols-3/4 of 12 MiniMonth (grid-cols-7 DayCells, ≤2 series-colored dots + popover), MonthView = larger DayCells with ≤3 chips + “+N more”; event titles render as TEXT NODES only (zero-`dangerouslySetInnerHTML` stance holds); `web/middleware.ts` matcher gains `/calendar/:path*`; Calendar nav link added in `web/app/layout.tsx`. **Gates — all PASS:** `/security-review` (proxy whitelists start/end only, no SSRF — static env upstream, no token leak to logs/responses, no new XSS); `pnpm typecheck` (tsc strict) + `pnpm lint` (biome) clean; `pnpm test` (vitest) **233/233** (new `dates.test.ts` 25 + `calendar-view.test.ts` 15 boundary/leap-year pins); the no-string-Date grep gate is zero; Playwright **E2E 15/15** — new `web/e2e/calendar.spec.ts` 5/5 (12 aligned months incl. leap-year Feb + Sunday-start month, event dots/popovers, `?view=month` deep-link, unauth `/calendar`→`/login`, year→month drill-down) + all pre-existing specs (editor/search/sermons-list/upload) green; the calendar fake-api stub was added to `web/e2e/support/fake-api.mjs` in the same change (no E2E lag). Web-only — no tenant DB gates (the calendar API gates ran in Phase 38); not operator-gated, auto-merged on green CI. Note: left `web/next-env.d.ts` at its committed form — `next dev` re-appends the `.next/types/routes.d.ts` reference + strips the comment, which would break CI's build-less `tsc --noEmit`.)
- [x] Phase 40 — Calendar week view + event CRUD UX (completed 2026-06-15, branch `phase-40/calendar-week-crud` — calendar goes read-write, still ZERO new runtime deps. **Week view:** `?view=week` joins the URL state, 7 day-columns of full event cards, same single half-open range fetch; `web/lib/dates.ts` gains week helpers (Sunday start, vitest-pinned incl. month/year rollover). **CRUD UX:** QuickCreatePopover on empty-day click (title, series, optional weekly-repeat-until → POST; the Phase 38 materializer caps the run server-side, 422 surfaced); EditEventPopover on chips/cards → PATCH (title/series/event_date) + DELETE; the CalendarView island refetches the range after every mutation. **Mutation proxies:** POST on `web/app/api/sermon-events/route.ts`, PATCH/DELETE on `…/sermon-events/[eventId]/route.ts` — structural body whitelists via `web/lib/calendar.ts` (POST: event_date/title/series/repeat_weekly_until; PATCH: event_date/title/series; `document_id` EXCLUDED until Phase 41; `eventId` from the path segment only, never the body). **Series→color:** `web/lib/series-color.ts` hashes the series string into a fixed array of LITERAL Tailwind classes (no interpolated/runtime-built class strings — those don't compile) — same color across year/month/week, vitest-pinned (stable hash + null handling). POST returns a LIST (`{events:[…]}`, one row or the whole weekly run) — the client merges all returned rows. **Gates — all PASS:** /security-review (whitelists confirmed, eventId path-only, no SSRF/token-leak/XSS, literal classes); pnpm typecheck (tsc strict) + lint (biome) clean; vitest **282/282** (dates 46, series-color 7, calendar-body 18 + week/view pins); the no-string-`Date` and no-interpolated-Tailwind grep gates are clean; Playwright **E2E 21/21** — `calendar.spec.ts` 11/11 (create-on-empty-day appears across week/month/year, weekly-repeat capped run, edit+delete round-trip across views, same-series-same-color) + all pre-existing specs green; E2E + fake-api mutation handlers added in the same change (no lag). Web-only — no tenant DB gates (calendar API gates ran in Phase 38); not operator-gated, auto-merged on green CI. Reverted the spurious `next-env.d.ts` regen before commit.)
- [x] Phase 41 — Calendar↔editor linking flows (completed 2026-06-15, branch `phase-41/calendar-editor-link` — pure UX wiring over existing endpoints, NO schema/API change, ZERO new deps. **Whitelist:** the event PATCH proxy whitelist (`web/lib/calendar.ts whitelistPatchEvent`) now forwards `document_id` by KEY PRESENCE (mirrors `series`): an explicit `null` is forwarded to detach, an absent key is omitted (leave the link) — never a truthiness check (a present null must pass for unlink); `CalendarEventPatch.document_id?: string|null` added to types.ts. The API still owns ownership/tenancy (Phase 38). **UX:** a linked event chip/card click navigates to `/sermons/{document_id}`; an unlinked click keeps the Phase 40 edit popover; create-doc-from-empty-date = POST `/api/documents` (title prefilled, NewSermonButton seed) → PATCH the event's `document_id` → navigate into the editor (two existing calls, no new endpoint); the EditEventPopover gains a link/unlink picker fed by GET `/api/documents` (own docs by construction), unlink = PATCH `document_id:null`; the Phase 38 ownership 422/404 surfaces as a VISIBLE popover error (never swallowed). User text renders as text nodes. **Gates — all PASS:** /security-review (document_id forwarded by key-presence, no field smuggling, ownership stays server-side, error surfaced not swallowed, no XSS/SSRF/token-leak); pnpm typecheck (tsc strict) + lint (biome) clean; vitest **285/285** (document_id three-state whitelist pins); Playwright **E2E 25/25** — `calendar.spec.ts` 15/15 (linked-click opens the right doc, create-from-date links + opens the editor, unlink clears, doc-delete leaves the event with `document_id` NULL [ON DELETE SET NULL contract], a cross-tenant `document_id` surfaces the visible error) + all pre-existing specs green; E2E + fake-api handlers extended in the same change. Web-only — the calendar/documents API tenant gates ran in Phases 34/38; not operator-gated. Reverted the spurious `next-env.d.ts` regen before commit.)
- [x] Phase 42 — Drag-to-reschedule + calendar E2E (completed 2026-06-15, branch `phase-42/calendar-drag` — the last B3 slice; web-only, ZERO new runtime deps (native HTML5 DnD). **Drag:** the event chip/card is `draggable` (dragstart payload = `event_id`), the day cell/column is a drop target (dragover preventDefault + drop) in ALL THREE views — month/week chips + year-view MiniMonth (popover `<li>` draggable, `<details>` day a drop target). **Optimistic reschedule:** drop runs a PURE `web/lib/calendar-dnd.ts` helper (applyMove repositions only the target event; a same-date drop is a no-op → no PATCH), then fires exactly ONE PATCH `event_date` via the existing proxy; on failure it rolls back to the EXACT prior date and shows a NON-blocking error (the grid stays on screen). **Keyboard fallback (accessible path — HTML5 DnD is mouse-only):** a move-to-date control in the EditEventPopover PATCHes `event_date`. No server/proxy change (`event_date` was already whitelisted in Phase 40). **Gates — all PASS:** pnpm typecheck (tsc strict) + lint (biome) clean; vitest **296/296** (11 new calendar-dnd pins: move-only-target, order preserved, same-date no-op, rollback restores exactly); Playwright **E2E 31/31, 0 flaky** — create + visible in year/month/week, drag-to-another-day persists after reload with exactly one PATCH, a forced-500 snaps the chip back + surfaces the error, keyboard-only reschedule via the popover fallback (native DnD driven reliably via manual DataTransfer dispatch). Web-only — no tenant DB gates and no /security-review (no new input surface); not operator-gated. **Closes Milestone M6.** Reverted the spurious `next-env.d.ts` regen before commit.)
- [x] Phase 43 — .docx round-trip core (export/import + revision snapshots) (completed 2026-06-15, branch `phase-43/docx-roundtrip` — B4's v2-minimal slice, ZERO OAuth; begins Milestone M7 (round-trip). **Migration 0008** (`sermon_doc_revisions`, down_revision 0007): `revision_id` PK, `document_id` FK→documents CASCADE, `user_id` FK→users CASCADE (DENORMALIZED tenant gate), `content` JSONB (PRIOR snapshot), `content_text`, `schema_version`, `source` ('import'), `created_at`; index (document_id, created_at DESC). **Node-leg decision (recorded):** the TipTap-JSON↔HTML legs run in a standalone React-free Node bundle `worker/convert_node/` (own pinned package.json + committed lockfile: @tiptap/html + core/pm/starter-kit @3.26.1 + **happy-dom 20.10.3** [peer bump from the 20.0.2 plan]; node_modules gitignored), invoked via subprocess (absolute `node` path, fixed argv, no shell) by `worker/convert.py`, which owns the pandoc legs (pypandoc html↔docx, `--reference-doc worker/assets/reference.docx`). The convert_node extension set MIRRORS web's `SermonEditor.buildExtensions` (StarterKit `link:false` + the citation node). **Citation fidelity:** the convert_node citation extension is a React-free MIRROR of web's CitationNode — renderHTML emits `<a href="/read/{bookId}?chunk={N}">` (data-* attrs don't survive docx; URLs do), parseHTML recovers bookId+chunkIndex FROM the URL (bookTitle degraded from anchor text, snippet/parentSection dropped — accepted ceiling); lockstep contract documented. **API (mounted on the existing documents resource — product term "sermons" == documents; naming deviation recorded):** GET `/documents/{id}/export.docx` (owned-doc 404-no-oracle gate → convert_to_docx → streamed) + POST `/documents/{id}/import` (multipart; libmagic docx sniff → 415, size cap → 413, /tmp staging ALWAYS cleaned in finally; **SNAPSHOT-FIRST** — insert the prior content/content_text revision row BEFORE the overwrite, one txn; content_text re-derived, never trusted; extra='forbid'). api imports `worker.convert` synchronously (6th sanctioned cross-package import; convert.py imports pypandoc directly, pulls no extractors/celery — api/AGENTS.md updated; pandoc+Node+the bundle flagged as Phase 29 image-bake deps, Dockerfiles untouched this phase). **Web:** Download/Import buttons in SermonEditor + same-origin export(stream)/import(multipart) proxies; imports load via `editor.setContent` as TipTap JSON (zero dangerouslySetInnerHTML). **Gates — ALL PASS:** schema-reviewer (additive, brief FK locks, single head 0008, correct DESC index, zero model↔migration drift, live up/down/up round-trip); tenant-auditor (both endpoints `_require_owned_document`; revision + overwrite `user_id`/`document_id` from the JWT/owned doc, never the body); `/check-tenant-leak` clean; **`/security-review` PASS — the attacker-docx surface fuzzed live (javascript:/data:/protocol-relative///absolute/`/read/../` traversal hrefs) → only same-origin `/read` citations are minted, no XSS reaches the editor, edge size-cap + libmagic sniff precede disk/pandoc, pandoc runs no-network, /tmp always cleaned, no shell injection (list argv)**; `make test-isolation` 3/3; **the GOLDEN docx round-trip phase gate RAN+PASSED (not skipped) — a citation-bearing sermon JSON→docx→JSON preserves structure + every `/read` citation deep-link, with the snapshot row predating the overwrite**; api **309 passed** (incl. a real pandoc+Node round-trip, snapshot-first, 404/415/413/422, /tmp-cleanup); web tsc/biome clean + vitest **299** + Playwright **E2E 33/33**. `test_convert` + the api real-round-trip skip cleanly in CI when node/pandoc/bundle are absent (live-test discipline). Reverted the spurious `next-env.d.ts` regen before commit.)
- [x] Phase 44 — OAuth connection vault (code-complete on branch `phase-44/oauth-vault`, PR #75; all keyless gates green, ⏳ merge gated on live Google connect verification against the Testing project — flipped to `[x]` to satisfy the `phases-row-flipped` CI gate on a `phase-44/*` branch, finalized at merge. **Vault:** `api/crypto_vault.py` AES-256-GCM, ciphertext-only at rest (`oauth_connections.access/refresh_token_ciphertext` BYTEA) — 32-byte key hex-decoded + length-validated on use, fresh `os.urandom(12)` nonce per `encrypt()` prepended to ciphertext+tag, `encrypt()` runs BEFORE the upsert, no plaintext fallback (InvalidTag → 500 no-oracle), missing/empty/non-hex/wrong-length key → `OAuthUnconfiguredError` → 503 naming only the env var, never blocks boot. **CSRF:** `/integrations` callback runs a strict fully-sequenced gate BEFORE any Google network call — provider allow-set → google-configured → constant-time HMAC-SHA256 state verify (`hmac.compare_digest` over `{user_id,nonce,provider,exp}`, 600s TTL) → exp → provider-match → account-binding (`state.user_id == JWT user`) → atomic Redis `GETDEL` of the single-use PKCE verifier (keyed by state nonce, EX=600); only then does the code exchange fire; every failure → shared generic 400 no-oracle. **Tenant:** all four `oauth_connections` statement builders scoped by `user_id`, always `current_user.user_id` (JWT) at every call site — never body/path/query/state; routes read no body; cross-tenant/never-connected revoke collapses to byte-identical 404. **Tokens never reach browser/logs:** list/connection responses select no ciphertext (provider+email+scopes+timestamps only), web proxies forward the bearer server-side only, deny-list in BOTH `api/observability.py` + `worker/obs.py` (lockstep) carries token/secret + explicit refresh_token/access_token/code_verifier/client_secret; web callback redirect is a FIXED same-origin path (`/settings/integrations?connected={provider}|?error={code}`, provider/error-code allow-set only, state/code never reflected — no open redirect). **Schema:** migration `0009_oauth_connections` single linear head off 0008, up→down→up clean, byte-for-byte model↔table match (FK `users.user_id` ON DELETE CASCADE, `UNIQUE(user_id,provider)`), `alembic check` zero drift. **Gates:** adversarial security gate PASS (no crit/high/med/low); api 356 passed (30 Phase-44), web 318 passed (19 Phase-44) + Playwright; app boots Google-unconfigured in dev+prod, OAuth route → clean 503. **live_connect deferred to operator:** populate `infra/.env` (`SERMON_API_GOOGLE_CLIENT_ID/SECRET` + `SERMON_API_TOKEN_ENC_KEY`) + run the connect flow before merge.)
- [ ] Phase 45 — Google Docs link/pull/unlink (spike-first)
- [ ] Phase 46 — Microsoft Graph provider (⛔ operator: Azure creds first)

---

Each phase is **one new Claude Code session**. Phases are intentionally small to keep context tight and reduce drift / errors. Run sequentially; verify each deliverable before moving on.

## Reference materials
PDFs live in `~/Downloads/`:
- `Ebook Search and Library System Architecture.pdf` — research paper
- `Future Platform_ Ebook RAG Architecture V1.pdf` — blueprint
- `Next Steps_ Platform Implementation Roadmap V1.pdf` — roadmap

Phase 0 distills these into a committed `ARCHITECTURE.md` inside the repo. Subsequent phases read that file, not the PDFs.

## Workflow per phase
1. Open new Claude Code session.
2. Copy the prompt for the current phase from `docs/PHASES.md` (or from this file pre-Phase-0).
3. Verify the deliverable.
4. Tick the phase's checkbox in `docs/PHASES.md` — completion date, branch name, any deviations or follow-up notes.
5. Commit.
6. Move to the next phase.

Global `CLAUDE.md` is loaded automatically (git identity, branch hygiene, /effort MAX). After Phase 0, a root `AGENTS.md` (with repo-level `CLAUDE.md` symlinked to it — Linux Foundation cross-tool standard so Cursor/Codex/Aider/Gemini/Copilot share the same instructions) plus per-package `AGENTS.md` files load contextually as Claude works in each directory. Each phase's branch name is pre-suggested so all sessions follow the same convention.

**Source of truth.** Once Phase 0 lands, `docs/PHASES.md` (committed in the repo) becomes canonical for both the plan and progress state — visible to contributors on clone and to any AI session. The personal `~/sermon-guide-phases.md` you bootstrapped from can be archived or deleted at that point.

---

## Phase 0 — Repo skeleton + OSS scaffolding + ARCHITECTURE.md

```
# Bootstrap sermon.guide

Set up a fresh OSS repo for sermon.guide — a multi-tenant ebook RAG platform (4,000 tenants × 10,000 ebooks each, theological library + sermon prep use case). Solo dev now; future contributors will use mixed AI tools (Claude Code, Cursor, Aider, Codex, Copilot) so all conventions go in cross-tool AGENTS.md files.

## Reference PDFs (read all three first)
- ~/Downloads/Ebook Search and Library System Architecture.pdf
- ~/Downloads/Future Platform_ Ebook RAG Architecture V1.pdf
- ~/Downloads/Next Steps_ Platform Implementation Roadmap V1.pdf

## Steps

1. Confirm the repo path with me. Default suggestion: ~/projects/sermon.guide.

2. mkdir parent if needed, git init inside, empty initial commit on main, then create branch `phase-0/repo-skeleton`.

3. Directory layout:
   - infra/      — docker-compose, k8s later
   - worker/     — Python ingestion pipeline
   - api/        — FastAPI backend
   - web/        — Next.js frontend
   - docs/       — PDFs copied here for offline reference
   - docs/adr/   — Markdown Any Decision Records (MADR format)
   - .claude/    — settings, skills, agents (committed to repo)
   - .github/    — PR template, issue templates, workflows

4. Write `ARCHITECTURE.md` at repo root:
   - Goal + scale targets.
   - Locked decisions: tenancy = shared collection w/ metadata filtering; vector DB = Milvus flat index; embeddings = BGE-Large 1024d; ingestion = Celery+Redis, autoscaled on K8s w/ KEDA (manifests in `infra/k8s/`, Phase 30); format = EbookLib+pandoc / pymupdf4llm; dedup = MinHash LSH; chunking = LlamaIndex SemanticSplitter; search = hybrid BGE+BM25 with RRF + cross-encoder rerank; pruning = BGE-M3 semantic highlighting; LLM = Gemini 1.5 Flash; frontend = Next.js+Tailwind; raw storage = R2/B2.
   - Milvus collection schema for `library_vectors` (id PK INT64, vector FloatVector 1024, tenant_id VarChar partition key, book_id VarChar, content_chunk Text, metadata JSON).
   - Postgres schema sketch (Users, GlobalBooks, UserLibrary, Highlights, Collections per the research PDF).
   - Out of scope for v0 — multi-region, mobile, Graph RAG, semantic caching, highlight import.
   - **## Open Questions** section listing decisions to make:
     - Dedup vs isolation: partition on tenant_id (vectors duplicated per user) OR partition on book_id (vectors shared, query filters book_id IN userlibrary). Decide before Phase 2.
     - Highlights: separate Milvus collection or same collection with content_type field? Decide before Phase 11.
     - LICENSE: Apache-2.0 (permissive + patent grant) OR AGPL-3.0 (network-copyleft, prevents proprietary SaaS forks). Decide before publishing publicly. MIT not recommended for SaaS-shaped projects.

5. Write `AGENTS.md` at repo root. Hard cap 300 lines, aim for ~60. Include only what AI cannot derive from code:
   - One-line project description and stack pointer.
   - Monorepo layout + dep direction (api imports worker.db; web is fully independent).
   - Conventional commits (atomic, one logical change per commit). Branch naming: `phase-N/short-slug`.
   - Per-package conventions live in `<package>/AGENTS.md` (added in later phases).
   - Pointers to `ARCHITECTURE.md` and `docs/adr/`.
   - For each line ask: would removing this cause AI to make mistakes? If no, cut.

6. Symlink so Claude Code finds the same content natively: `ln -s AGENTS.md CLAUDE.md` (run from repo root).

7. Write `.claude/settings.json` (committed to repo):
   - `permissions.allow`: `Bash(docker compose *)`, `Bash(make *)`, `Bash(uv run *)`, `Bash(pytest *)`, `Bash(pnpm *)`, `Bash(npm run *)`, `Bash(gh pr *)`, `Bash(gh api *)`, `Bash(git status)`, `Bash(git diff*)`, `Bash(git log*)`, `Bash(git show*)`.
   - `permissions.deny`: `Read(.env)`, `Read(.env.*)`, `Read(~/.ssh/**)`, `Read(~/.aws/**)`, `Read(~/.gnupg/**)`, `Bash(git push origin main*)`, `Bash(git push --force*)`, `Bash(rm -rf*)`.
   - `enableAllProjectMcpServers: false` — critical for OSS, blocks malicious MCPs from cloned dependency repos.
   - `PostToolUse` hooks (stub now, real commands wire in once tooling exists):
     - matcher `Edit|Write`, filePattern `worker/**/*.py|api/**/*.py` → `cd <pkg> && uv run ruff check --fix "$file" && uv run pyright "$file"` (Phase 2 enables for worker, Phase 10 for api).
     - matcher `Edit|Write`, filePattern `web/**/*.{ts,tsx}` → `cd web && pnpm tsc --noEmit && pnpm biome check "$file"` (Phase 15 enables).
   - `PreToolUse` hook on `Bash`: grep guard that exits 1 on `rm -rf` or `git push.*--force`.
   - Add `.claude/settings.local.json` to `.gitignore` for per-machine overrides.

8. Write `LICENSE`. **Stop and ask me which license** (Open Question above). Do not pick for me.

9. Write `CONTRIBUTING.md`:
   - Setup: install `uv` for Python, `pnpm` for Node, `pre-commit install` for hooks, `make up` for infra.
   - Pre-PR checklist: `/test-isolation` (search/auth/ingestion changes), `/check-tenant-leak` (DB/Milvus query changes), `/security-review` (built-in Claude skill — for any user-input handling), conventional commits, no variant-file litter.
   - Note: this codebase is built primarily with AI assistants. If AI keeps making the same mistake in your work, that's a docs bug — file an issue against `AGENTS.md`.

10. Write `CODE_OF_CONDUCT.md` — Contributor Covenant 2.1 verbatim.

11. Write `.github/` files:
    - `PULL_REQUEST_TEMPLATE.md`: "What this changes" / phase area checkboxes / AI-collaboration checklist (`/test-isolation`, `/check-tenant-leak`, `/security-review`, golden test added if retrieval changed, no `_v2`/`_old`/`_fixed` filenames, AGENTS.md updated if conventions changed) / test plan.
    - `ISSUE_TEMPLATE/bug.md` and `ISSUE_TEMPLATE/feature.md`.
    - `workflows/ci.yml` — lint + typecheck + tests. Gate per-package jobs (worker/api/web/retrieval-golden) with a single `filter` job that probes the filesystem for each entry point (`worker/pyproject.toml`, `api/pyproject.toml`, `web/package.json`, `worker/tests/golden/queries.jsonl`) and emits a boolean output per package; downstream jobs use `needs: filter` + `if: needs.filter.outputs.<pkg> == 'true'` so they SKIP cleanly until each phase wires its package in. **Never put `hashFiles()` in a job-level `if:`** — it is only valid in step-level `if:`; at job level it causes GitHub Actions to reject the entire workflow at load time (workflow fails in 0s on every push, CI signals silently lost). Mirror the filter-job pattern in `workflows/codeql.yml`.
    - `workflows/codeql.yml` — default GitHub-managed Python + JS config.
    - `dependabot.yml` — weekly Python (uv) + npm updates, grouped.

12. Write `.gitleaks.toml` (default ruleset) and `.pre-commit-config.yaml` with:
    - gitleaks hook
    - variant-file regex hook blocking commits matching `_(old|new|v2|backup|fixed|copy)\.(py|ts|tsx)$`

13. Write the first three ADRs in `docs/adr/` using MADR (https://adr.github.io/madr/):
    - `0001-vector-db-choice.md` — Milvus vs Weaviate/Qdrant/pgvector.
    - `0002-tenancy-model.md` — record both partition options + state the open question.
    - `0003-embedding-model-choice.md` — BGE-Large 1024d.

14. Write `docs/PHASES.md` — the in-repo source of truth for plan + progress. Copy the contents of `~/sermon-guide-phases.md` (the file the user bootstrapped from) verbatim, then prepend a `## Progress` section with all 17 phases as unchecked checkboxes:
    ```
    - [ ] Phase 0 — Repo skeleton + OSS scaffolding + ARCHITECTURE.md
    - [ ] Phase 1 — Infrastructure (docker-compose) + infra/AGENTS.md
    - [ ] Phase 2 — Milvus collection bootstrap + Python tooling
    ... (through Phase 16)
    ```
    Add a one-line note at the top of the Progress section: "After each phase commit, tick the box and append: completion date, branch name, deviations/follow-ups." This document is now canonical; future sessions read `docs/PHASES.md`, not the personal copy.

15. Write `README.md` (brief: what this is, current phase pulled from `docs/PHASES.md`, how to run, links to AGENTS.md, ARCHITECTURE.md, CONTRIBUTING.md, PHASES.md).

16. Write `.gitignore` (Python, Node, .env, model caches, Milvus volumes, .DS_Store, `.claude/settings.local.json`).

17. Copy the three PDFs into `docs/`.

18. **Tick Phase 0's checkbox in `docs/PHASES.md`** — append completion date and `phase-0/repo-skeleton` as the branch. Note "LICENSE deferred pending Open Question answer" if the user hasn't picked yet.

## Verify
- `actionlint .github/workflows/*.yml` reports clean. (Install via `go install github.com/rhysd/actionlint/cmd/actionlint@latest` or the release binary at https://github.com/rhysd/actionlint/releases.)
- Push the branch and confirm CI actually executes: `gh run list --branch phase-0/repo-skeleton --limit 5` shows the `CI` and `CodeQL` workflows completing cleanly (success, or clean skips for jobs whose package files don't exist yet) — NOT failing in 0s. **Local YAML parsing (`python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"`) is necessary but NOT sufficient** — it accepts syntax GitHub Actions rejects at workflow-load time (e.g. `hashFiles()` or `matrix` in job-level `if:`), which is exactly how a parse-time bug can ship without the validator noticing. Only an observed workflow run proves CI is alive.

19. Commit. Stop.

## Stop criteria
Repo exists. ARCHITECTURE.md, AGENTS.md (+ CLAUDE.md symlink), .claude/settings.json, LICENSE, CONTRIBUTING.md, CODE_OF_CONDUCT.md, .github/ templates and workflows, .gitleaks.toml, .pre-commit-config.yaml, three ADRs, **docs/PHASES.md with Phase 0 ticked**, all committed. Open Questions captured. Don't proceed until I've answered the LICENSE question.
```

---

## Phase 1 — Infrastructure (docker-compose) + infra/AGENTS.md

```
cd to the sermon.guide repo. Read ARCHITECTURE.md and root AGENTS.md before doing anything.

Goal: `docker compose up -d` boots all infra deps.

## Build
- Branch: phase-1/infra-compose off main.
- infra/docker-compose.yml with:
  - Milvus standalone (latest stable) + etcd + MinIO (deps)
  - Redis 7
  - Postgres 16
- Healthchecks on every service.
- Named volumes for persistence; .env for ports/passwords (commit .env.example, gitignore .env).
- Makefile at repo root: up, down, logs, ps, nuke (down -v).
- Write `infra/AGENTS.md` (~30 lines): which services live here, healthcheck conventions, env-var naming (`SERMON_*` prefix), how to add a service, why we're not using docker-compose v1 syntax.

## Verify
- `make up` → all services healthy within 60s.
- `make down && make up` → idempotent.
- Reachability: curl Milvus health, redis-cli ping, psql connect.

Commit. Stop. No Python yet.
```

---

## Phase 2 — Milvus collection bootstrap + Python tooling

```
cd to sermon.guide. Read ARCHITECTURE.md.

**Pre-flight:** confirm the dedup-vs-isolation Open Question in ARCHITECTURE.md is resolved. If not, stop and ask which approach to take — it changes the schema partition key.

Goal: Python script creating the `library_vectors` Milvus collection per spec, plus the worker package's lint/typecheck stack so AI assistants get fast feedback.

## Build
- Branch: phase-2/milvus-bootstrap off main.
- worker/pyproject.toml (uv, Python 3.12). Deps: pymilvus, ruff, pyright.
- Ruff config: line-length=100, target-version=py312, select = E, F, W, I, B, BLE, TRY, ASYNC, S, ARG, ERA, UP, TID. flake8-tidy-imports.banned-api: `datetime.datetime.utcnow` → "use datetime.now(tz=UTC)"; `pickle` → "use json or msgpack — pickle is unsafe across versions".
- Pyright config: typeCheckingMode = "strict", pythonVersion = "3.12".
- worker/scripts/bootstrap_milvus.py:
  - Connect to Milvus (host/port from env).
  - Create `library_vectors` per ARCHITECTURE.md.
  - Partition key per the resolved Open Question.
  - Flat index on vector field (per locked decision).
  - Idempotent: --force drops+recreates; default skips if exists.
- worker/Makefile: bootstrap-milvus, lint (ruff check), format (ruff format), typecheck (pyright), test.
- Activate the worker PostToolUse hook in `.claude/settings.json` so ruff + pyright actually run on edits inside `worker/`.
- Install Pyright LSP plugin so type errors land in Claude's loop in-turn rather than next run: `/plugin install pyright-lsp@claude-plugins-official`.
- Wire CI: `.github/workflows/ci.yml` worker job runs `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`, `uv run pytest`.
- Write `worker/AGENTS.md` (~40 lines): uv usage, ruff/pyright commands, banned-api list and why, async-Milvus client gotchas, where pymilvus client is initialized, link to ARCHITECTURE.md for schema, mention LSP plugin so contributors install it.

## Verify
- `uv run ruff check` clean. `uv run pyright` clean.
- Run bootstrap script. List collections + describe schema. All fields and partition key correct.
- Run again without --force → skip cleanly. Run with --force → recreate cleanly.
- Edit a worker file with `datetime.datetime.utcnow()` → PostToolUse hook fires and surfaces the banned-api error in the same turn.

Commit. Stop.
```

---

## Phase 3 — Tenant isolation smoke test (HARD GATE) + /test-isolation skill

```
cd to sermon.guide. Read ARCHITECTURE.md.

This is a hard gate. Multi-tenant data leakage is the #1 architectural risk. Don't move past this until isolation is provably solid.

## Build
- Branch: phase-3/tenant-isolation-test off main.
- Add deps: pytest, numpy.
- worker/tests/test_tenant_isolation.py:
  - Setup: insert 100 random vectors as tenant_a, 100 as tenant_b, distinguishable book_ids.
  - Test 1: filtered query as tenant_a → zero tenant_b ids in results.
  - Test 2: same in reverse.
  - Test 3: query without filter returns mixed (sanity).
  - Behavior-named class `TestTenantIsolation` with failure-mode docstring on each test.
  - Teardown: drop test data only, not the collection.
- worker/Makefile: test-isolation.
- Ship `.claude/skills/test-isolation/SKILL.md` so any contributor (Claude, Codex, Cursor, Gemini) can `/test-isolation`. Frontmatter: `name: test-isolation`, `description: Run multi-tenant isolation tests after schema or query changes`. Body: invoke the make target; halt and report on failure; never paper over — tenant isolation failures are critical bugs.

## Verify
- `make test-isolation` passes.
- Deliberately break the filter (remove the expr arg). Test must fail loudly with the docstring's failure-mode message visible. Restore.
- In a fresh Claude Code session: `/test-isolation` invokes correctly and reports.

Commit. Stop. Don't move on if anything looks off.
```

---

## Phase 4 — Format detection + extraction

```
cd to sermon.guide. Read ARCHITECTURE.md.

Goal: extract clean Markdown from EPUB and PDF inputs. Text only, no embedding.

## Build
- Branch: phase-4/format-extraction off main.
- Deps: python-magic, EbookLib, pypandoc, pymupdf4llm.
- Pandoc binary required (apt install pandoc). Document in README and worker/AGENTS.md.
- worker/extractors/__init__.py — detect(path) -> "epub"|"pdf".
- worker/extractors/epub.py — EbookLib → (X)HTML → pandoc → markdown.
- worker/extractors/pdf.py — pymupdf4llm.to_markdown.
- worker/extractors/extract.py — dispatcher: extract(path) -> str.
- CLI: `python -m worker.extractors.extract <path>` prints markdown to stdout.

## Test data
- worker/tests/samples/ — public-domain EPUB (Project Gutenberg) and a PDF. If missing, ask me to drop one in.

## Verify
- EPUB sample → readable markdown, no alt-text pollution / metadata leakage.
- PDF sample → readable markdown, line-wrapping reasonable.
- Smoke test in worker/tests/test_extractors.py (assert non-empty + sane char distribution).

Commit. Stop. No chunking, no embedding.
```

---

## Phase 5 — Semantic chunking

```
cd to sermon.guide. Read ARCHITECTURE.md.

Goal: split extracted markdown into semantic chunks for embedding.

## Build
- Branch: phase-5/semantic-chunking off main.
- Deps: llama-index, llama-index-embeddings-huggingface (SemanticSplitter needs an embedder for boundary detection).
- worker/chunking.py:
  - chunk(markdown: str) -> list[Chunk] using LlamaIndex SemanticSplitterNodeParser.
  - Chunk dataclass: text, start_idx, end_idx, parent_section (best-effort from markdown headers).
- CLI: `python -m worker.chunking <md_file>` prints chunk count + previews.

## Verify
- Run on Phase 4's EPUB output → 50–500 chunks for a typical novel; boundaries on sentence ends.
- worker/tests/test_chunking.py smoke test.

Commit. Stop.
```

---

## Phase 6 — Embedding + Milvus insert + tenant-auditor subagent

```
cd to sermon.guide. Read ARCHITECTURE.md.

Goal: end-to-end ingest CLI: file → chunks → BGE embeddings → Milvus rows partitioned by book_id (per ARCHITECTURE.md §3 + §7.1 resolution). Plus ship the `tenant-auditor` subagent and paired `/check-tenant-leak` skill so future sessions can audit tenant scoping on demand.

## Build
- Branch: phase-6/embedding-insert off main.
- Dep: sentence-transformers (or FlagEmbedding).
- worker/embedding.py:
  - Load BAAI/bge-large-en-v1.5 once at module init. CPU fine for now; document GPU swap.
  - embed(texts: list[str]) -> np.ndarray (N, 1024).
- worker/ingest.py:
  - CLI: `python -m worker.ingest <file> --user-id <id> --book-id <id>` (user_id used for the user_library row that records ownership; book_id is the vector partition).
  - Pipeline: detect → extract → chunk → embed → insert with metadata JSON (filename, chunk index).
- No dedup yet, no Celery. Single-process.
- Ship `.claude/agents/tenant-auditor.md`:
  - Frontmatter: `name: tenant-auditor`, `description: Audit code for tenant-scoping and isolation bugs`, `tools: Read, Grep, Bash(uv run pytest worker/tests/test_tenant_isolation.py *)`, `model: opus`.
  - Body: every Milvus search has `book_id IN (<set>)` in expr where the set is sourced from Postgres user_library for a JWT-authenticated user (never the request body); every SQLAlchemy query filters by user_id derived from JWT, never the request; API routes derive user_id from JWT only; highlights queries are double-scoped (user_id AND book_id). Run isolation test as final check.
- Ship `.claude/skills/check-tenant-leak/SKILL.md` (the grep-based check that CONTRIBUTING.md and the PR template reference). Frontmatter: `name: check-tenant-leak`, `description: Audit codebase for unscoped DB or vector queries`, `disable-model-invocation: true`. Body: grep `collection.search(`, `session.query(`, `.execute(`; verify each Milvus search has a `book_id IN` expression and each SQLAlchemy query has a `user_id` filter; flag any `user_id` or `book_id` set sourced from request body rather than JWT-derived (and the user_library lookup driven by it).

## Verify
- Ingest a real book under tenant_a. Row count = chunk count.
- Re-run Phase 3 isolation test with this real data present — must still pass.
- Invoke the `tenant-auditor` subagent against current code — should pass clean.

Commit. Stop.
```

---

## Phase 7 — Postgres schema + Alembic migrations + schema-reviewer subagent

```
cd to sermon.guide. Read ARCHITECTURE.md.

Goal: relational schema for users, books, libraries, highlights — per the research PDF. Plus ship the `schema-reviewer` subagent so future migrations get reviewed for backward compat and locking risk.

## Build
- Branch: phase-7/postgres-schema off main.
- Deps: sqlalchemy, alembic, asyncpg, pydantic-settings.
- worker/db/ (shared layer; api/ will import this):
  - models.py: Users, GlobalBooks (with minhash_signature blob + text_pointer), UserLibrary, Highlights, Collections.
  - session.py: async engine + session factory.
- worker/db/alembic/ — config + initial migration creating all tables.
- worker/Makefile: migrate-up, migrate-down, migrate-new MSG=...
- Ship `.claude/agents/schema-reviewer.md`:
  - Frontmatter: `name: schema-reviewer`, `description: Review Alembic migrations for backward compat and locking risk`, `tools: Read, Grep, Bash(uv run alembic *)`, `model: opus`.
  - Body checklist: NOT NULL adds without default on existing tables; index creation on hot tables (require CREATE INDEX CONCURRENTLY); enum changes (PG enums are awful); foreign-key cascades; data backfills inside DDL transactions; downgrade path correctness; tenant scoping preserved on every new table.

## Verify
- migrate-up from clean DB → all tables.
- migrate-down → all gone.
- migrate-up again → idempotent.
- Run `schema-reviewer` against initial migration — expect "first migration, no compat concerns".

Commit. Stop. No business logic in models — schema only.
```

---

## Phase 8 — MinHash LSH dedup

```
cd to sermon.guide. Read ARCHITECTURE.md and the dedup section of the research PDF.

Goal: skip embedding work for books we've already seen.

## Build
- Branch: phase-8/minhash-dedup off main.
- Dep: datasketch.
- worker/dedup.py:
  - signature(markdown: str) -> MinHash (5-shingles, lemmatized).
  - find_duplicate(sig, threshold=0.85) -> book_id | None — query GlobalBooks signatures via LSH.
  - LSH index persisted in Postgres; lazy-load and rebuild from DB on worker start.
- Update worker/ingest.py:
  - After extract, before chunking: compute signature, check dedup.
  - If duplicate: insert UserLibrary row pointing at existing GlobalBooks.book_id. Skip chunking + embedding.
  - If new: chunk + embed, then insert GlobalBooks row with signature.

## Verify
- Ingest book X under tenant_a → vectors created.
- Ingest same book under tenant_b → no new vectors, just UserLibrary pointer.
- Confirm tenant_b can search the shared content per the dedup-vs-isolation decision from Phase 0. If isolation is broken, stop and revisit the Open Question.
- Run `tenant-auditor` subagent — must still pass.

Commit. Stop.
```

---

## Phase 9 — Celery worker

```
cd to sermon.guide. Read ARCHITECTURE.md.

Goal: turn the ingest CLI into a Celery task fed by Redis.

## Build
- Branch: phase-9/celery-worker off main.
- Deps: celery, redis.
- worker/celery_app.py — Celery instance, broker = Redis, backend = Redis.
- worker/tasks/ingest.py — @app.task wrapping the Phase 6/8 pipeline.
- worker/Makefile: worker (runs celery worker), enqueue FILE=... TENANT=... (test enqueue).

## Verify
- Start worker with `make worker`.
- In another shell: `make enqueue FILE=... TENANT=tenant_a` → task picked up, completes, vectors land.
- Kill worker mid-task → restart picks it up cleanly OR marks failed (whichever the design specifies).

Commit. Stop. KEDA/k8s autoscaling is later.
```

---

## Phase 10 — FastAPI skeleton + JWT auth + upload + api/AGENTS.md

```
cd to sermon.guide. Read ARCHITECTURE.md.

Goal: HTTP layer. Sign up, log in, upload files (which queue Celery tasks).

## Build
- Branch: phase-10/fastapi-auth-upload off main.
- api/pyproject.toml — fastapi, uvicorn, python-jose[cryptography], passlib[bcrypt], python-multipart, sqlalchemy, asyncpg, celery, redis. Import worker.db models (shared package).
- Copy the same Ruff + Pyright strict config from worker/pyproject.toml. Wire api into the PostToolUse hook (`.claude/settings.json` already targets `api/**/*.py`).
- api/main.py — FastAPI app, CORS, healthz.
- api/auth.py:
  - POST /auth/signup — create User, hashed password.
  - POST /auth/login — return JWT.
  - get_current_user dependency that decodes JWT.
- api/uploads.py:
  - POST /upload — multipart file, save to local storage (R2/B2 later), enqueue Celery ingest with user_id from JWT, return task_id.
  - GET /tasks/{task_id} — Celery task status.
- api/Makefile: dev, test, lint, typecheck.
- Wire CI: add api lint/typecheck/test job alongside worker.
- Write `api/AGENTS.md` (~40 lines): `user_id` is always derived from JWT, never from request body or query params; the user's `book_id` set for any vector search is resolved server-side from `user_library` per request, never accepted from the client; auth dependency injection pattern; how to add a new route; common 401/403 mistakes; reference `tenant-auditor` subagent before merging any new query; reference `schema-reviewer` for any DB query changes.
- Reinforce `/security-review` (built-in Claude Code skill) usage in `CONTRIBUTING.md` and `.github/PULL_REQUEST_TEMPLATE.md` for any PR touching api/ or web/ that handles user input. (PR template already includes the checkbox from Phase 0; this phase is when contributors actually start running it.)

## Verify
- Sign up two users.
- Log in as user_a, upload book → task_id.
- Poll /tasks/{id} → succeeds.
- Reuse user_a's JWT after logout-style scenarios → 401 where expected.
- Run `/security-review` against the new code — fix any reported issues before commit.
- Run `tenant-auditor` against api/ — clean.

Commit. Stop. No search yet.
```

---

## Phase 11 — Vector search endpoint + golden-test infrastructure

```
cd to sermon.guide. Read ARCHITECTURE.md.

Goal: authenticated semantic search over the user's own ingested books. Plus stand up the golden-test infrastructure so retrieval regressions get caught in CI when models, chunking, or ranking change.

## Build
- Branch: phase-11/vector-search off main.
- api/search.py:
  - POST /search → {query: str, limit: int = 10}.
  - Embed query with BGE-Large.
  - Milvus filtered search per the resolved tenancy model.
  - Return list of {content_chunk, book_id, metadata, score}.
- Shared embedding loader (don't duplicate model init across processes).
- Golden-test infrastructure:
  - `worker/tests/golden/queries.jsonl` — JSONL rows: `{"query": "...", "expected_book_ids": [...], "min_score": 0.7}`. Seed with 5–10 hand-curated entries against the public-domain test corpus (Augustine, Bunyan, Bonhoeffer if available — pick books a sermon-prep user would actually search).
  - `worker/tests/test_retrieval_golden.py` — load JSONL, run search per row as a fixed test tenant, assert at least one expected book_id in top-K with score ≥ min_score. Hit/miss binary, no fuzzy partial credit. Behavior-named class `TestRetrievalAccuracy`.
  - Wire into CI as a separate job `retrieval-golden` so a regression is visible distinct from unit tests.

## Verify
- As user_a: search a phrase from user_a's book → matching chunks.
- As user_b: same phrase → nothing (or only user_b matches).
- Phase 3 isolation test still passes.
- Golden tests pass on current corpus. Deliberately break ranking (e.g., disable BGE, return random vectors) → goldens fail loudly. Restore.

Commit. Stop. No reranking, no BM25.
```

---

## Phase 12 — Hybrid search (BM25 + RRF)

```
cd to sermon.guide. Read ARCHITECTURE.md and hybrid-search section of research PDF.

Goal: combine dense + sparse retrieval via Reciprocal Rank Fusion.

## Build
- Branch: phase-12/hybrid-search off main.
- BM25: simplest path is Postgres tsvector on a chunks table (ingest writes here too — backfill if needed). Document the choice in ARCHITECTURE.md and add an ADR `0004-bm25-backend.md`.
- api/search.py:
  - Dense + sparse run in parallel.
  - RRF fusion: score = Σ 1/(k + rank_i), k=60.
  - Return fused top-K.

## Verify
- Specific name query (e.g., "Theodore Roosevelt") that vector misses → BM25 catches it.
- Thematic query → still works.
- Golden tests still pass; add 1–2 entries that specifically exercise BM25 strengths.

Commit. Stop.
```

---

## Phase 13 — Cross-encoder rerank + semantic highlighting

```
cd to sermon.guide. Read ARCHITECTURE.md.

Goal: tighten relevance and prune context before LLM call.

## Build
- Branch: phase-13/rerank-highlight off main.
- Dep: sentence-transformers cross-encoder.
- api/rerank.py — top-30 from hybrid → cross-encoder/ms-marco-MiniLM-L-6-v2 → top-10.
- api/highlight.py — sentence-level scoring with BGE-M3 against query, prune below 0.5 threshold.

## Verify
- Manual eyeballing: pruned chunk content is more on-topic.
- Token count post-prune drops 70–80% (architecture target).
- Golden tests still pass; reranking should improve hit rate, not degrade it.

Commit. Stop.
```

---

## Phase 14 — Gemini 1.5 Flash summary agent

```
cd to sermon.guide. Read ARCHITECTURE.md.

Goal: 1–2 paragraph thematic summary endpoint.

## Build
- Branch: phase-14/summary-agent off main.
- Dep: google-genai.
- api/summary.py:
  - POST /search-summary → {query, limit_chunks: int = 20}.
  - Run Phases 12–13 retrieval pipeline.
  - Prompt: query + pruned chunks with citation markers (book title + page/chunk index).
  - Gemini 1.5 Flash with grounding instructions: "1–2 paragraphs, cite [book:chunk] inline, only use provided context."
  - Return {summary, citations: [...]}.
- GOOGLE_API_KEY in env. Update .env.example. Add to `.claude/settings.json` deny list as `Read(.env*)` already covers it.

## Verify
- Real query like "what does this say about faith" → coherent grounded output with citations that map to retrieved chunks.
- Hallucination check: query nothing-in-corpus → response says so, doesn't confabulate.

Commit. Stop.
```

---

## Phase 15 — Next.js: auth + library + web/AGENTS.md

```
cd to sermon.guide. Read ARCHITECTURE.md.

Goal: minimal frontend for the auth/upload/library flow. Search UI is Phase 16.

## Build
- Branch: phase-15/web-auth-library off main.
- web/ — Next.js 15 app router, TypeScript, Tailwind, Biome.
- tsconfig.json: `strict`, `noUncheckedIndexedAccess`, `noImplicitOverride`, `exactOptionalPropertyTypes`.
- Pages: /signup, /login, /library, /upload.
- JWT in HttpOnly cookie via Next route handlers (don't expose to client JS).
- /library: fetch UserLibrary entries + ingestion task status, render table.
- /upload: drag-and-drop, POST /upload, poll /tasks/{id}, optimistic update on library.
- Activate the web PostToolUse hook in `.claude/settings.json` so `pnpm tsc --noEmit && pnpm biome check $file` runs on edits inside web/.
- Wire CI: add web job running `pnpm tsc --noEmit`, `pnpm biome check`, `pnpm vitest run`.
- Write `web/AGENTS.md` (~40 lines): server vs client component split rules; HttpOnly cookie auth flow; never store JWT in localStorage; data-fetching patterns; tailwind conventions; `pnpm` (not npm) for deps; biome run command; route handler pattern for proxying api/ calls so JWT cookie never reaches the browser JS.

## Verify
- Browser: sign up, log in, upload book, watch status flip to done, see book in library.
- `pnpm tsc --noEmit && pnpm biome check` clean.
- Run `/security-review` — fix any reported issues.

Commit. Stop.
```

---

## Phase 14b — OpenAI-compatible LLM transport (ppq.ai) + Phase 14 live verify

```
cd to sermon.guide. Read ARCHITECTURE.md §2 + §5, api/AGENTS.md, api/summary.py,
api/settings.py, and GitHub issue #24 before doing anything.

This phase closes Phase 14's open deviation — the /search-summary LLM
round-trip has never run live (issue #24) — and MUST land before Phase 16 puts
UI on top of that endpoint.

## Pre-flight
- Standard hygiene: git stash list, sync main, branch phase-14b/ppq-llm-transport.
- Note: the phases-row-flipped CI gate only matches phase-<digits>/ branches,
  so it skips on 14b — tick the Phase 14b row manually before merging.
- If the Phase 14b row/section is missing from docs/PHASES.md on main, the
  planning PR hasn't merged — merge it (docs-only) before branching.

## Researched context (2026-06-04 — re-verify anything that smells stale)
- ppq.ai (PayPerQ) = pay-as-you-go OpenAI-compatible gateway (card/crypto,
  ~2¢/query, 10¢ minimum top-up; no Google account needed).
  - base_url https://api.ppq.ai/v1, auth "Authorization: Bearer <PPQ_API_KEY>",
    standard chat-completions shape (docs: https://ppq.ai/api-docs).
  - Public model catalog, no key needed: curl -s https://api.ppq.ai/v1/models
  - Catalog as of 2026-06-04: google/gemini-2.5-flash ($0.21/$1.75 per 1M
    in/out), google/gemini-2.5-flash-lite ($0.07/$0.28), google/gemini-2.5-pro,
    gemini-3-flash-preview, and ~google/gemini-flash-latest (drifting alias —
    do not pin). All support temperature + max_tokens.
  - There are NO gemini-1.5-* models anywhere: Gemini 1.5 Flash is retired
    industry-wide, so Phase 14's GEMINI_MODEL = "gemini-1.5-flash" is dead
    regardless of transport. That retirement is the forcing function for this
    phase touching the LLM layer at all.
- Google's own OpenAI-compatible endpoint also exists:
  https://generativelanguage.googleapis.com/v1beta/openai/ with bare model ids
  (gemini-2.5-flash) and the same GOOGLE_API_KEY
  (docs: https://ai.google.dev/gemini-api/docs/openai). One transport therefore
  serves both providers.

## Decision — write docs/adr/0005-llm-transport.md (MADR)
Replace the google-genai SDK with the openai SDK as a single config-driven
OpenAI-compatible chat-completions transport. Rationale: ppq.ai is OpenAI-shaped
(google-genai cannot speak to it); Google-direct stays available via its
OpenAI-compat endpoint, so prod runs the SAME code path the PPQ live-verify
exercises; the gemini-1.5-flash retirement forces the model constant to change
anyway. Alternatives to record: two-SDK provider switch (rejected: two error
paths, and the google-genai arm would stay forever-unverified); raw httpx
(rejected: hand-rolled retries/types for no gain). Update ARCHITECTURE.md §2
LLM row: "Gemini 1.5 Flash" → "Gemini Flash (2.5 at v0) over an
OpenAI-compatible transport (ADR 0005)".

## Build
- api/settings.py:
  - llm_provider: Literal["google", "ppq"] = "google" (SERMON_API_LLM_PROVIDER).
  - Provider map (single source of truth, e.g. a small _PROVIDERS dict in
    summary.py): google → (https://generativelanguage.googleapis.com/v1beta/openai/,
    "gemini-2.5-flash", GOOGLE_API_KEY); ppq → (https://api.ppq.ai/v1,
    "google/gemini-2.5-flash", PPQ_API_KEY).
  - ppq_api_key read unprefixed via validation_alias="PPQ_API_KEY" (same
    pattern + rationale as GOOGLE_API_KEY; it is the literal name PPQ's docs
    use).
  - llm_model: str | None = None override (SERMON_API_LLM_MODEL); None → the
    provider default above.
- api/summary.py: _client() → lazy lru_cache'd openai.OpenAI(base_url=...,
  api_key=...); _generate_summary → client.chat.completions.create(
  model=..., messages=[{"role": "system", ...}, {"role": "user", ...}],
  temperature=0.2, max_tokens=768); map openai.APIError → 502, and None/empty
  choices[0].message.content → 502. The handler's 503-before-retrieval guard
  now keys on the ACTIVE provider's key, and its detail names the missing env
  var (e.g. "...set PPQ_API_KEY"). Grounding prompt, citation contract, and
  the no-context short-circuit are unchanged.
- api/pyproject.toml: drop google-genai, add openai (current major, pinned in
  style with neighbors); rewrite the Phase 14 dep comment block.
- infra/.env.example: document PPQ_API_KEY + SERMON_API_LLM_PROVIDER +
  SERMON_API_LLM_MODEL (append-only — deny rules block Read/Edit on .env*).
- Tests (api/tests/test_summary_unit.py): re-seam the 22 Phase 14 tests from
  models.generate_content → chat.completions.create, preserving every pinned
  behavior; add provider-resolution tests: default=google; ppq flip picks PPQ
  base_url/model/key; llm_model override wins; per-provider 503 detail;
  "PPQ_API_KEY set but provider=google still 503s" (no silent cross-pairing).
- api/AGENTS.md: model-surface table row + any google-genai mentions →
  openai-SDK transport (still a network call; no in-process model; no
  api↔worker pin-lockstep).
- Aside while in api/ (own commit): the .claude/settings.json api PostToolUse
  hook runs `ruff check --fix` but NOT `ruff format` — exactly how Phase 14's
  format-only CI failure shipped. Append `&& uv run --project api ruff format
  "$file"` (and the worker analog). Phase 15 hit the auto-mode classifier on
  settings.json edits — expect to ask the operator to authorize.

## Verify
- Unit: cd api && uv run pytest; uv run ruff check; uv run ruff format
  --check .; uv run pyright. All clean.
- Catalog pre-check (no key): curl -s https://api.ppq.ai/v1/models | grep -o
  '"google/gemini-2.5-flash"' | head -1 — confirm the pinned id still exists.
- LIVE (the point of the phase — needs the operator's PPQ_API_KEY in
  infra/.env + SERMON_API_LLM_PROVIDER=ppq; ~2¢ total; stack via make up, api
  via cd api && make dev):
  1. Grounded: as phase12-a (owns the 5-book corpus), POST /search-summary
     {"query": "what does this say about faith"} → 1–2 paragraphs, inline
     [book:chunk] markers that resolve, citations ⊆ prompt sources.
  2. No-confabulation: {"query": "who was Theodore Roosevelt"} → the fixed
     no-context message, citations=[], and NO LLM call (prove it: control run
     with the key unset must behave identically on this query).
  3. Record warm E2E latency (retrieval + LLM round-trip) for the
     api/AGENTS.md open-gap row.
- make test-isolation (Phase 3 hard gate) + /check-tenant-leak +
  tenant-auditor (summary.py touched; transport adds no query surface) +
  /security-review (key handling changed).

## Close out
- Tick the Phase 14b row in docs/PHASES.md (date, branch, deviations — note
  explicitly that the google-direct arm is config-verified only until a
  GOOGLE_API_KEY exists; the transport code is identical either way).
- Edit Phase 14's row: append "(live verify closed by Phase 14b)" to its
  Deviation sentence. Do not rewrite the rest.
- Close issue #24 with the verify transcript. Update the project memory note
  (phase-14-live-verify-deferred) — it gates Phase 16 on this work.
- Conventional commits: docs(adr) → feat(api) → test(api) → chore(hooks) →
  docs(phases). Stop. Phase 16 is a separate session.
```

---

## Phase 16 — Next.js: search + summary UI

```
cd to sermon.guide. Read ARCHITECTURE.md.

Goal: the actual sermon-prep workflow.

## Build
- Branch: phase-16/web-search off main.
- /search page:
  - Query input, submit.
  - Calls /search-summary.
  - Renders 1–2 para summary at top, citation cards below (book title, chunk preview).
  - Loading / empty / error states.
- Add to nav from /library.

## Verify
- Full browser flow: log in, upload theology book, wait for ingest, search "what does this say about grace" → grounded summary with working citations.
- Run `/security-review` — fix any reported issues.

Commit. Stop. v0 done.
```

---

## v1 Plan — Beyond Phase 16

Planned 2026-06-05 from a full-repo audit (phase deviations, ARCHITECTURE/ADR
consequences, AGENTS.md "Open trust gaps", CI, code, GitHub tracker) followed by a
three-draft design pass (security-first / ops-first / product-first) that was judged,
merged, and adversarially dependency-checked. v0 shipped the pipeline; v1 makes it
**trustworthy** (M1), **resilient with real content** (M2), and **deployable** (M3).

Rules — same as v0, plus one that bit during planning:

- One phase = one Claude Code session = one `phase-N/short-slug` branch off main.
  Conventional commits, atomic.
- **Every phase must flip its own `- [x] Phase N — …` row in this file** — the
  `phases-row-flipped` CI job fails any `phase-N/*` PR whose row is unchecked. This
  applies to "pure docs" phases too; the row flip is the one expected edit even when a
  phase claims "no source changes".
- Tenant gates (`make test-isolation`, `/check-tenant-leak`, `tenant-auditor`,
  `/security-review`, `schema-reviewer` on migrations) re-run per phase as listed.
  Until Phase 17 lands, remember the gates are dev-box-only — CI skip-passes them.
- Ordering: milestones are sequential; within them, Phase 26 (docs) is free-floating
  and Phase 29 may run in parallel with 27/28 (its only hard dependency is Phase 18).

### Milestone 1 — Make the safety net real & lock auth trust (Phases 17–21)

The "tenant isolation is not negotiable" gate actually executes in CI instead of
skip-passing; JWTs cannot be forged from a shipped default secret; request bodies
reject smuggled fields instead of silently dropping them; the auth/search edge is not
a free DoS; ingest metadata is clean at the root. After M1, every downstream "tenant
gates pass" claim is trustworthy.

### Milestone 2 — Resilience & a corpus a pastor can trust (Phases 22–25)

The retrieval path degrades gracefully instead of returning bare 500s; a real
seedable theological corpus replaces the 5 synthetic dev books; citations survive the
live-observed comma-merge; the search filter survives a 10K-book library; the
hand-verified web flows become regression tests.

### Milestone 3 — Observable, recoverable, deployable (Phases 26–30)

Docs stop lying; structured logs + metrics + error tracking make the §1 latency
targets observable; backups make disk loss survivable; baked offline-capable images
build in CI; readiness-gated k8s/KEDA manifests land the locked deploy direction.

---

## Phase 16b — Remote inference: kill in-process models (PRIORITY — before Phase 17)

```
cd to sermon.guide. Read ARCHITECTURE.md §2/§5, docs/adr/0005-llm-transport.md,
api/search.py, api/rerank.py, api/highlight.py, api/summary.py,
worker/embedding.py, worker/chunking.py, infra/docker-compose.prod.yml.

Goal: NO model weights load in-process anywhere — every inference leg becomes a
remote API call. Kills ~3.7GB resident RAM in the api (+ ~3GB worker spikes),
~75s/query of CPU model time, and the ~40min/book ingest wall; shrinks both
Python images by ~1.5GB; lets the AWS box downsize t3a.xlarge → t3a.large
(~$55/mo, us-east-1 verified 2026-06-05).

Decisions locked by the 2026-06-05 research pass (re-verify prices on the live
pages before pinning ids):
- Embeddings (query + ingest chunks + semantic-chunking boundaries): DeepInfra
  BAAI/bge-large-en-v1.5 — EXACT same weights as today, so every existing
  Milvus vector stays valid (no re-embed, goldens keep their calibration).
  OpenAI-embeddings-compatible (https://api.deepinfra.com/v1/openai),
  $0.01/1M tokens, zero-retention by default (matters: users' private
  libraries). ppq.ai was the preferred vendor but is chat-completions ONLY
  (catalog live-probed 2026-06-05: 331 models, zero embedders/rerankers) —
  keep base_url/model/key env-driven so a future ppq /v1/embeddings is an
  env flip, no code.
- Rerank: replace the in-process cross-encoder with DeepInfra
  BAAI/bge-reranker-v2-m3 (~$0.01/1M, sub-200ms for 30 docs, beats the
  2021 MiniLM on BEIR-class benchmarks). Same query+passages→scores shape.
- Highlight: KEEP the stage and the 0.5 threshold — the no-context →
  no-LLM-call anti-confabulation contract (Phases 14/16 live verifies)
  depends on it. Swap in-process BGE-M3 for DeepInfra BAAI/bge-m3 dense,
  one batched call per query (~300 sentences ≈ $0.000075).
- Summary LLM: unchanged — ppq/google via the ADR 0005 transport. Latency
  bonus IF the active provider exposes it: reasoning_effort "none"/minimal
  (verified on Google's OpenAI-compat layer for 2.5-era models; ppq only
  documents reasoning.effort on /v1/responses, not chat.completions) —
  probe and take the win (~60s thinking → ~5-10s non-thinking).

## Build
- Branch: phase-16b/remote-inference off main.
- ADR 0006: remote inference transport — vendor matrix, the
  vector-compatibility argument (same weights => zero migration), privacy
  posture (DeepInfra zero-retention default; ppq policy re-checked), and the
  ppq-embeddings gap + env-portability story.
- Shared transport lives in worker/ (api already imports worker modules):
  OpenAI-compatible embeddings client + thin rerank client. Env (unprefixed
  key alias per the GOOGLE_API_KEY/PPQ_API_KEY precedent):
  SERMON_EMBEDDINGS_BASE_URL / SERMON_EMBEDDINGS_MODEL / DEEPINFRA_API_KEY /
  SERMON_RERANK_MODEL. Timeouts + one retry; failure → 502 naming the
  provider (the 14b pattern). Batch <= provider max, preserve input order.
- Space-consistency guard: record the embedding model id once at bootstrap
  (Postgres meta row or Milvus collection description); embed/search refuse
  to run when env model != recorded model. Silent provider/model drift would
  mix embedding spaces and quietly destroy retrieval — make it loud.
- worker/embedding.py: embed() keeps its signature; body becomes the API
  call. worker/chunking.py: swap HuggingFaceEmbedding for the same transport
  (llama-index OpenAI-compatible embedding class), same model id.
- api/rerank.py + api/highlight.py: same public shapes, thresholds, and
  metadata keys (rrf_score, sentences_kept/total) — bodies become API calls.
- Delete: torch + sentence-transformers from BOTH pyprojects (regenerate
  uv locks); HF cache volume + prewarm one-shot + HF_HUB_OFFLINE/HF_HOME
  from infra/docker-compose.prod.yml and both Dockerfiles;
  infra/scripts/prewarm_models.py.
- ARCHITECTURE.md §2 rows (embedding/rerank/pruning) + §5 lifecycle updated;
  api/AGENTS.md model table → remote-call table (no pin-lockstep concern).

## Verify
- Unit suites green with transports mocked — every existing rerank/highlight
  behavioral pin (truncation, tiebreak, threshold inclusivity, metadata
  preservation) must survive the seam swap unchanged.
- make test-retrieval-golden 9/9 against live DeepInfra (same weights => same
  thresholds, re-ingest NOT required). Spot-check one query's vector against
  the local model's output within float tolerance.
- make test-isolation 3/3 (hard gate; no tenant surface changed).
- Live prod timing before/after: /search-summary warm E2E — target <=15s with
  reasoning off, <=70s if thinking stays. Record both in the row.
- RAM: api + worker resident <1GB combined warm (was ~7GB). Then stop the
  box → modify-instance-attribute t3a.xlarge → t3a.large → start → re-run
  deploy smoke.
- Cost reconciliation: one EPUB ingest + 10 searches against the DeepInfra +
  ppq dashboards (~$0.006/book, ~$0.001-0.004/search expected).
- Gates: /check-tenant-leak + tenant-auditor + /security-review — new
  outbound calls carry chunk text to DeepInfra: confirm no user_id/JWT/email
  ever leaves, keys flow env → Authorization header only.

## Close out
- Flip this row with: pinned model ids + prices, measured E2E latency delta,
  RAM delta, instance downsize done/deferred, and any ppq capability changes
  (embeddings endpoint? reasoning knobs on chat.completions?).
```

---

## Phase 17 — CI service containers + model cache: make the gates run for real

```
cd to sermon.guide. Read .github/workflows/ci.yml and the skipif guards in
worker/tests/test_tenant_isolation.py, test_retrieval_golden.py, test_ingest.py.

Goal: the CI gates stop skip-passing. Today ci.yml provisions no Postgres/Milvus/Redis
and no model cache, so the load-bearing suites hit their skip guards and pass
vacuously — tenant isolation has only ever been enforced on the dev box. This phase is
first because every later phase re-runs gates that are currently green-but-vacuous.

## Build
- Branch: phase-17/ci-real-gates off main.
- Bring real infra to CI. Simplest path: a job step running the existing
  infra/docker-compose.yml (`make -C infra up` already --wait's on healthchecks)
  rather than hand-rolling GH `services:` blocks — Milvus needs its etcd + MinIO
  siblings anyway. Path-filter so the infra boot only runs for worker/api changes
  (the filter job exists).
- Wire the env vars each skip-guard keys off; bootstrap the Milvus collection so
  test_tenant_isolation.py executes its body. The isolation suite is synthetic (two
  tenants, disjoint book_id sets) — no corpus, no embedding models needed. Make it a
  REQUIRED blocking check.
- Golden/ingest jobs: the local 5-book corpus is copyrighted and must NOT be
  committed. Either (a) seed a tiny public-domain CI corpus (Gutenberg/CCEL texts)
  with its own golden rows + actions/cache on HF_HOME (~3.7 GB models; consider a
  nightly job if PR latency is unacceptable), or (b) keep them local-gated but make
  skipping LOUD — a CI step that fails if those suites report SKIPPED without an
  explicit local-only marker. Choose (a) if the cache restore stays under ~3 min;
  record the choice in this phase's row.

## Verify
- Mutation proof IN CI, not just locally: a throwaway branch dropping the `filter=`
  from the Milvus search turns the CI isolation job RED (FAILED, not SKIPPED). Revert.
- CI logs show real pass counts; zero "SKIPPED (Milvus unreachable)" lines in the
  required job.

## Close out
- Flip this row; record the golden/ingest option chosen and the CI wall-clock delta.
```

---

## Phase 18 — JWT-secret startup guard + Pydantic extra='forbid' + /readyz

```
cd to sermon.guide. Read api/settings.py, api/main.py, api/auth.py.

Goal: close the two cheapest total-isolation defeats, and give orchestrators a
readiness signal every later deploy phase needs.

## Build
- Branch: phase-18/jwt-guard-readyz off main.
- JWT-secret startup guard: api/settings.py:27 defaults jwt_secret to a
  publicly-known dev string, and the module docstring CLAIMS a startup assertion
  exists — it does not (api/main.py has no lifespan hook). Add a FastAPI lifespan
  hook that refuses to boot when the secret is unset or equals the dev default,
  unless an explicit dev opt-out (e.g. SERMON_API_ENV=dev) is set. Fix the lying
  docstring. A deployment that forgets SERMON_API_JWT_SECRET must fail loudly, not
  serve forgeable JWTs.
- extra="forbid" on every inbound request model (search, summary, auth bodies): a
  smuggled user_id/book_ids becomes a hard 422 instead of a silently-dropped field
  backed by a reviewer-enforced rule (Phase 12 deviation d). Check the web/ proxies
  first — they forward {query} only, so nothing legitimate breaks.
- GET /readyz: Postgres + Milvus + Redis connectivity with per-dep status and short
  timeouts; /healthz stays cheap and dependency-free. Phases 29/30 wire container
  HEALTHCHECK and k8s readiness to this route.

## Verify
- App refuses boot with default/unset secret; boots with a real one and with the dev
  opt-out. POST /search with an extra user_id field → 422. /readyz → 200 only when
  all three deps are reachable, 503 with per-dep breakdown when one is stopped;
  /healthz unaffected.
- Gates: make test-isolation 3/3 (now real in CI) + /check-tenant-leak +
  tenant-auditor + /security-review (auth surface changed).
```

---

## Phase 19 — Edge rate limiting (signup/login/search-summary) + CORS prod-origin guard

```
cd to sermon.guide. Read api/auth.py, api/main.py:26-32 (CORS), api/settings.py.

Goal: the public edge stops being free to abuse. /auth/signup takes any
email+password with no throttle, /auth/login is open to credential stuffing, and
/search-summary burns ~134 s of CPU per call. No rate limiting exists anywhere.

## Build
- Branch: phase-19/edge-rate-limits off main.
- Redis-backed rate limiting (broker Redis is already in the stack; works across
  replicas — pick slowapi or a small middleware, record the choice in api/AGENTS.md):
  per-IP limits on /auth/signup + /auth/login; a stricter per-user limit on
  /search-summary (the most expensive route in the system). 429 + Retry-After.
- CORS prod-origin guard in the Phase 18 lifespan hook: refuse boot when
  allow_credentials=True pairs with a wildcard/unset origin outside dev. Document
  SERMON_API_CORS_ORIGINS for prod in infra/.env.example (append-only — deny rules).
- Explicitly out of scope (parked): email verification / CAPTCHA, per-tenant quotas.

## Verify
- Scripted burst: /auth/login and /search-summary return 429 past the threshold,
  enforced across two api processes sharing one Redis. Limits documented.
- CORS guard blocks boot on a credentials+wildcard misconfig; dev boot unaffected.
- Gates: make test-isolation + /check-tenant-leak + /security-review.
```

---

## Phase 20 — Upload idempotency + upload_tasks ownership + content-type posture

```
cd to sermon.guide. Read api/uploads.py INCLUDING its "Security choices" docstring,
worker/AGENTS.md "Idempotency caveat", api/AGENTS.md "Open trust gaps".

Goal: uploads survive crashes and tasks have owners — the oldest deferred trust gaps
(Phases 9–11). All three items below touch api/uploads.py + one migration; they are
one session.

## Build
- Branch: phase-20/upload-integrity off main.
- upload_tasks(task_id, user_id, …) table + Alembic migration (schema-reviewer gate):
  GET /tasks/{task_id} authorizes by JWT-derived user_id — another user's task
  returns 404 (don't leak existence) — instead of treating the 122-bit Celery UUID
  as an unguessable capability.
- Task-id-keyed idempotency token on /upload → worker, so a worker death between the
  Milvus insert and the Postgres commit converges to one consistent record on
  redelivery instead of orphan vectors (the documented Phase 9 window,
  worker/AGENTS.md "Idempotency caveat").
- Content-type posture — do NOT silently contradict the repo: api/uploads.py's
  docstring deliberately argues AGAINST API-edge format trust ("refusing here would
  just push attackers to a different content-type header"; the worker libmagic-sniffs
  via worker/extractors). Decide in-phase: (a) implement early libmagic rejection
  with a NEW rationale (don't stage attacker bytes to disk; don't burn a
  tens-of-minutes doomed ingest) AND rewrite that docstring to match; or (b) close
  the deferred-since-Phase-4 item as deliberate-wontfix recorded in api/AGENTS.md.
  Either way, code and stated design must agree afterwards.

## Verify
- Re-POST of the same upload/task token → no second ingest, no duplicate vectors.
  kill -9 the worker mid-insert (the Phase 9 drill) → redelivery reconciles to one
  record, zero orphan vectors.
- GET /tasks/{id} as another user → 404; own task unchanged.
- If (a): a script renamed .epub is rejected at the edge. If (b): the wontfix and its
  rationale are in api/AGENTS.md.
- Gates: make test-isolation + /check-tenant-leak + tenant-auditor +
  /security-review + schema-reviewer on the migration.
```

---

## Phase 21 — parent_section HTML strip at ingest + backfill + orphan-debris sweep

```
cd to sermon.guide. Read worker/chunking.py (the ATX-heading capture),
worker/extractors/epub.py, worker/scripts/backfill_chunks.py, and Phase 16
deviation iv above.

Goal: kill the parent_section HTML debris at the root. Today the web UI masks it
(displaySection() drops any label containing '<') while worker/chunking.py keeps
capturing raw pandoc heading text — tag soup like '<a href="part0002…' persists in
chunks.parent_section and Milvus metadata. The post-v0 cleanup was previously tracked
nowhere in-repo; this phase is that item.

## Build
- Branch: phase-21/parent-section-clean off main.
- Strip markup from headings at capture time in worker/chunking.py (pandoc gfm emits
  inline HTML anchors/spans — strip tags, collapse whitespace; avoid a new heavyweight
  dep if existing tooling covers it).
- Backfill: extend the worker/scripts/backfill_chunks.py pattern to rewrite
  parent_section on existing chunks rows AND re-sync the matching Milvus metadata.
- Same data pass: delete the known dev test debris — 1 orphan global_books row
  (_test_phase8_synthetic) + 3 orphan Milvus book_ids (b_mere_christianity,
  b_1_thess, 88ba2fe2…). Verify none are tenant-reachable BEFORE deleting.
- web/lib/summary.ts displaySection() stays as belt-and-suspenders; do not remove.

## Verify
- Fresh EPUB ingest → zero '<' in parent_section in both Postgres and Milvus; the
  backfill leaves zero dirty rows; orphans gone and only those rows touched.
- Gates: make test-retrieval-golden + make test-isolation; /check-tenant-leak (light
  — no query-shape change).
```

---

## Phase 22 — Graceful degradation across retrieval arms + model loads

```
cd to sermon.guide. Read api/search.py (the dense/sparse asyncio.gather),
api/rerank.py, api/highlight.py, and the Phase 12 pre-merge audit above.

Goal: a single dependency blip stops meaning a bare 500. Today one-arm-down ⇒ 500
(Milvus-down burns ~12 s of pymilvus retries first); any rerank/highlight model-load
or inference failure raises straight through (realistic trigger: cold HF cache
without network on the first reranked /search after boot).

## Build
- Branch: phase-22/graceful-degradation off main.
- The dense/sparse fan-out IS a gather: add return_exceptions=True + per-arm handling
  so one arm down degrades to the surviving arm with a partial-result signal in the
  response (e.g. degraded: ["dense"]). Bound the Milvus-down case to a fast typed
  error via client timeout config instead of the 12 s retry long-tail.
- Rerank + highlight are NOT a fan-out — they run sequentially after fusion: wrap
  each in try/except so a cross-encoder/BGE-M3 failure falls back to raw RRF top-K
  (also flagged in the response), not a 500.
- /search-summary inherits the posture via run_search; decide and document what a
  degraded-retrieval summary does (proceed-with-flag vs 503).
- Degradation must NEVER widen scope: every fallback path still filters by the
  JWT-derived user_library set.

## Verify
- Stop Milvus → fast degraded BM25-only response with the flag (not a 12 s 500);
  force a reranker load failure → RRF results + flag; each failure mode unit-tested.
- Gates: make test-isolation + /check-tenant-leak + tenant-auditor (fallback paths
  are new query paths).
```

---

## Phase 23 — Production corpus seeding plan + idempotent bulk-ingest runbook

```
cd to sermon.guide. Read worker/tests/golden/queries.jsonl, the enqueue target in
worker/Makefile, ADR 0003 (English-first corpus note).

Goal: a corpus a pastor can actually use. The dev corpus is 5 synthetic books owned
by a dev user; the spec's own "grace" query prunes to zero on it. No corpus plan
exists anywhere.

## Build
- Branch: phase-23/production-corpus off main.
- Sourcing/licensing policy (short doc in docs/): public-domain seeding
  (Gutenberg/CCEL classics — Augustine, Calvin, Spurgeon, Wesley, …); user-owned
  uploads remain the tenant path; no gray-area content.
- Seed manifest + idempotent bulk-ingest script: point at a directory of
  legally-held ebooks, enqueue through the existing Celery + dedup path using the
  Phase 20 idempotency token so re-runs are safe. CPU ingest is slow (~40 min/book)
  — the runbook documents expected wall-clock and worker parallelism.
- Extend the golden query set with rows against the seeded books so retrieval
  quality is measurable on real content — including a "grace" row that actually has
  corpus support.
- Kill the live-suite silent-skip trap (Phase 20 deviation ii): `make -C worker
  test` is plain `uv run pytest` — it never sources `infra/.env`, so on a fully
  keyed dev box the live-gated suites (ingest e2e incl. the kill-9 redelivery
  regression, embedding weight-parity, golden) skip silently and 72-passed looks
  like full coverage. Add an explicit `test-live` target that sources
  `../infra/.env` (the migrate-up/worker target pattern) and FAILS on any
  key/infra skip (the CI live-gate guard's classification, reused locally);
  leave plain `test` keyless-fast. The runbook documents which target gives
  which coverage.
- Runbook in docs/: clean infra → seeded corpus, reproducible — including the
  live-env recipe (tracked `.env.example` values + operator key + the Postgres
  port from `infra/.env`; per Phase 21 finding iii the live mapping is **5432**
  and 54322 is the stale CODE default in `worker/db/settings.py` — align or
  document in the runbook, don't hardcode either).

## Verify
- Seed run ingests the manifest; dedup converges on re-run (no duplicate vectors);
  the new golden rows pass; "grace" returns grounded results on the seeded corpus.
- `make -C worker test-live` on the keyed dev box: zero key/infra skips (only a
  documented corpus-shape skip, if any, may remain); plain `make -C worker test`
  still passes keyless.
- Gates (bulk ingest exercises dedup + library scoping): make test-isolation 3/3 +
  /check-tenant-leak AFTER the seed — seeded books must not contaminate any existing
  tenant's user_library, and dedup must not cross-link libraries.
```

---

## Phase 24 — Comma-merged citation extraction + library-size search-filter cap

```
cd to sermon.guide. Read api/summary.py (_extract_citations), web/lib/summary.ts
(segmentSummary), api/AGENTS.md "No library cap" row, ADR 0002 consequences.

Goal: two known correctness/scale items on the search path. No hard dependency on
Phase 23 — multi-citation summaries already occur on the 5-book corpus, and the
filter cap is testable synthetically.

## Build
- Branch: phase-24/citations-filter-cap off main.
- Comma-merged citations: _extract_citations does summary_text.find(marker) on the
  exact bracketed string, so "[A:70, A:51]" silently drops the merged-only member
  (the documented v0 trade-off; observed live in Phases 14b and 16). Parse merged
  brackets into their individual markers — members must still resolve against the
  prompt-source set (never fabricate a citation). Update web/lib/summary.ts
  segmentSummary to render merged brackets as linked chips; carry the contract
  change through its unit tests.
- Library-size filter cap: a 10K-book library generates a ~360 KB book_id IN (...)
  expr per arm (doubled by the BM25 arm). Implement the chunked-filter or capped
  strategy from api/AGENTS.md + ADR 0002's revisit note. Test with SYNTHETIC data:
  insert 10K dummy user_library rows for a test user — no real ingest needed.

## Verify
- A summary containing a merged bracket yields each member as a resolved citation
  chip, zero fabricated markers; single-marker behavior unchanged (unit-pinned).
- The synthetic 10K-book user's /search executes without expr rejection or blowup;
  record the latency.
- Gates: make test-retrieval-golden + make test-isolation + /check-tenant-leak +
  tenant-auditor (the filter builder IS the tenant boundary).
```

---

## Phase 25 — Web component + Playwright E2E coverage for search/citations/upload

```
cd to sermon.guide. Read web/AGENTS.md, web/test/, and the Phase 15/16 deviation
notes above (no headless browser; verifies were cookie-jar HTTP drives).

Goal: the hand-verified Phase 15/16 flows become regression tests. web/test/ covers
only pure lib helpers; SearchPanel, citation chips, and the upload flow have zero
automated coverage.

## Build
- Branch: phase-25/web-e2e off main.
- Component tests (@testing-library/react or Playwright component mode — record the
  choice in web/AGENTS.md): SearchPanel states (loading ticker, empty, error,
  grounded), citation chip resolution including the Phase 24 merged-bracket
  contract, upload form.
- Playwright E2E against a booted dev stack: login → search → grounded summary with
  resolving citations; login → upload → task status. The upload E2E asserts the
  POST-Phase-20 contract (own task → 200, another user's task → 404). Stub or
  short-circuit the ~134 s LLM round-trip for CI (e.g. a test provider row) — the
  live LLM path stays a manual/nightly concern.
- Wire into the web CI job; bind the dev server to an explicit free port (the :3000
  conflict on the dev box is real — see web/AGENTS.md once Phase 26 lands).

## Verify
- Suite passes locally + in CI; deliberately breaking the citation chip renderer or
  the search submit turns it red; E2E runs headless in CI against the seeded stack.
- Gates: none (web-only; talks to api over HTTP).
```

---

## Phase 26 — Doc-rot sweep (README status, shipped-gate phrasing, :3000 workaround)

```
cd to sermon.guide.

Goal: docs stop lying. Cheap, high-confusion fixes in one pass. Free-floating — can
land any time.

## Build
- Branch: phase-26/doc-rot off main.
- README.md: "Status: Phase 0 (repo skeleton)" → v0 complete + link the v1 plan; fix
  the "Quick start (when phases land)" annotations for phases that landed.
- CONTRIBUTING.md + root AGENTS.md: "/test-isolation (ships in Phase 3)" and
  "/check-tenant-leak (ships in Phase 6)" → present tense; they shipped.
- web/AGENTS.md: document the :3000 port conflict + `pnpm dev --port 3001`
  workaround and the "never pkill -f 'next dev' unqualified" rule (memory-only until
  now).
- Anything else `grep -rn "Phase 0\|ships in Phase" --include="*.md"` surfaces.

## Verify
- The greps come back clean. No source files changed — the ONLY non-doc edit is this
  file's own Phase 26 row flip (required by the phases-row-flipped CI gate).
```

---

## Phase 27 — Structured logging + metrics + error tracking

```
cd to sermon.guide. Read api/AGENTS.md latency rows, ARCHITECTURE.md §1 targets.

Goal: the system stops being unobservable. No service has structured logging,
metrics, or error tracking; the §1 latency targets and the ~134 s reality cannot be
seen in production.

## Build
- Branch: phase-27/observability off main.
- Structured JSON logging with a per-request correlation id across api/ and worker/
  (an upload traceable enqueue → ingest → searchable). JWTs/secrets/PII never logged.
- Metrics endpoint (Prometheus shape): p50/p95 per route on the hot paths (/search,
  /search-summary, /upload); per-stage retrieval timings (embed / dense / sparse /
  rerank / highlight / LLM); ingest stage timings; Celery queue depth. Emit the
  Phase 22 degraded-arm signals as counters.
- Error tracking (Sentry-compatible, env-driven DSN, off by default in dev) for
  api/ + worker/.

## Verify
- One /search-summary call → correlated logs with stage timings; the metrics
  endpoint exposes per-route histograms; a deliberately raised error reaches the
  tracker; a log audit shows zero JWT/secret/PII.
- Gates: /check-tenant-leak sanity (no query-shape change).
```

---

## Phase 28 — Backup + restore tooling (Postgres, Milvus, MinIO)

```
cd to sermon.guide. Read infra/Makefile, infra/docker-compose.yml.

Goal: losing the box stops meaning losing everything. No backup tooling exists for
any of the three stateful stores (infra targets are up/down/logs/ps/nuke).

## Build
- Branch: phase-28/backups off main.
- make backup / make restore targets: pg_dump/pg_restore for Postgres (users,
  library, chunks); the supported Milvus 2.6 path for collections (e.g. the
  milvus-backup tool — its data lives in the compose MinIO + etcd); MinIO bucket
  sync. Artifacts to a configurable target dir; off-box rsync documented.
- Restore drill runbook in docs/: backup → make nuke → restore → verify.

## Verify
- The drill, actually run: a previously-ingested book + its vectors + user_library
  rows round-trip backup → nuke → restore; /search finds it afterwards.
- Gates: make test-isolation 3/3 AFTER restore (tenant scoping survives recovery).
```

---

## Phase 29 — App Dockerfiles + image-build CI (models baked, HF offline)

```
cd to sermon.guide. Read worker/AGENTS.md "Offline mode" + NLTK pre-warm notes,
api/AGENTS.md model table (~3.7 GB), .github/workflows/ci.yml.

Goal: the apps become shippable artifacts. Lint/test CI + CodeQL exist; zero
application Dockerfiles do. Hard dependency: Phase 18's /readyz only. Phase 27's
logging is recommended-not-required — rebuild images when it lands; do NOT serialize
packaging behind observability.

## Build
- Branch: phase-29/app-images off main.
- Dockerfiles: api/ (uvicorn, HEALTHCHECK → /readyz); worker/ (Celery; bake
  BGE-Large + cross-encoder + BGE-M3 + NLTK WordNet at build time per
  worker/AGENTS.md, set HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1); web/ (Next
  standalone). Multi-stage, uv- and pnpm-native, pinned bases; .dockerignore keeps
  model caches/venvs out of context.
- CI image-build job on main: build all three, tag (sha + latest), push to GHCR.
  Path-filtered like the existing jobs.

## Verify
- docker run of each image works against the compose stack; the worker container
  ingests a book with HF network access disabled (offline proof); CI publishes
  tagged images; the api container HEALTHCHECK flips unhealthy when Postgres stops.
- Gates: /security-review on the image surface (secrets, base images); no
  query-path changes.
```

---

## Phase 30 — KEDA + k8s manifests gated on /readyz

```
cd to sermon.guide. Read ARCHITECTURE.md §2 (ingestion runtime row) + §6.

Goal: the locked deploy direction exists as manifests. Last v1 phase by design — it
sits on images (29), readiness (18), observability (27), and recoverability (28,
recommended before real traffic).

## Build
- Branch: phase-30/k8s-keda off main.
- infra/k8s/: provider-portable manifests (or a Helm chart — if Helm, record it in
  an ADR) for api/worker/web consuming the Phase 29 GHCR images. Postgres/Redis/
  Milvus as external services or operator-managed — document the trade-off; the
  compose stack stays for local dev. Readiness gates on /readyz, liveness on
  /healthz. Secrets via k8s Secrets — SERMON_API_JWT_SECRET is required (the
  Phase 18 guard refuses the default). Resource requests honest about CPU model
  costs.
- KEDA ScaledObject: worker scales on Redis queue depth (the §2 locked decision).
- Update the Phase 0 locked-decisions row above + ARCHITECTURE.md §6 so "K8s w/
  KEDA" stops describing unbuilt infra once this lands.

## Verify
- kind/minikube: manifests apply; pods Ready only when /readyz is green; KEDA
  scales the worker deployment up under a queue burst and back down; a rollout with
  a dead dependency is held by the readiness gate.
- Gates: /security-review (exposed services, secrets handling).
```

---

## v2 Plan — Sermon Workflow (Phases 31–46)

Planned 2026-06-10 directly from the **v2 Product Backlog** section below, which stays
the canonical design — the prompts cite its anchors instead of restating it. Numbering
was assigned ahead of the post-Phase-30 drill by operator decision; that re-audit still
happens, scoped to hardening gaps rather than these features.

Milestones: **M4 Read** (31–33) · **M5 Write** (34–37) · **M6 Schedule** (38–42) ·
**M7 Round-trip** (43–46).

Rules — v2 execution model (extends the v0/v1 rules; written for autonomous runs):

- One phase = one `phase-N/short-slug` branch off main = **one ultracode Workflow**.
  The main loop stays thin — scout, build, gate, verify, ship — holding conclusions
  only; subagents absorb all file reading and return summaries, never file dumps.
- Scout reads ONLY the files the phase prompt names + the backlog anchor it cites.
  The backlog is the design; do not re-derive or re-litigate it.
- Build: fan out builder subagents on disjoint file sets (worker/ vs api/ vs web/).
  A builder is done only when its PostToolUse hooks (ruff+pyright / tsc+biome) pass
  clean.
- Gates: audit gates run in parallel (tenant-auditor, /check-tenant-leak,
  /security-review, schema-reviewer — as the diff demands); live suites run SERIALLY
  (one shared Milvus/Postgres on this box). Tenant gates are never waived,
  downgraded, or retried-until-green.
- Gate failure: fix-forward only when the fix stays inside the phase's stated scope;
  otherwise STOP — push the branch, open the PR marked blocked, notify the operator.
  Two consecutive failed gate rounds = hard stop.
- Migration race rule: take number = (highest merged in worker/db/alembic/versions)
  + 1 at branch time; renumber + re-point down_revision if scooped; never two
  migration-bearing phases in flight at once. Migration-bearing here: 32, 34, 38,
  43, 44, 45.
- Ship: flip this file's `- [x] Phase N` row IN THE SAME BRANCH before opening the
  PR — the phases-row-flipped job fails any phase-N/* PR otherwise, and merges happen
  only via `gh pr merge` (direct push to main is denied), so an unflipped row
  deadlocks an autonomous session. Conventional commits, atomic; the PR body lists
  every gate run and its result.

Ordered queue (v1 ∥ v2 interleave — the Progress checkboxes at the top of this file
are the durable cursor; any session resumes at the first unchecked row in this order):

```
31 → 17 18 19 20 21 → 32 33 → 22 23* 24 25 → 34 35 36 37 → 28† → 38 39 40 41 42
   → 43 44* 45* 46* → 26 27 29 30
```

`*` = operator-gated stop-at-PR (17 changes CI semantics + needs the branch-protection
flip, 23 corpus rights, 44–46 OAuth credentials). `†` = Phase 28 pulled forward from
M3: backups must precede irreplaceable sermon manuscripts (B2 flags this upgrade), and
26/27 deferred so the doc sweep covers everything and Phase 27 log-redaction covers
the full OAuth token surface — this amends v1's "milestones are sequential" rule.

Merge policy: auto-merge (`gh pr merge --rebase`) when local gates + CI are green and
no fix-forward touched a cross-item contract. Until Phase 17 lands, local gates are
the merge authority (CI's load-bearing suites skip-pass there); after 17, both must be
green. Stop-at-PR for the `*` phases and any blocked gate.

Operator-only checkpoints (autonomy stops + notifies): GCP project + OAuth consent +
client creds in `.env` before 44 (Testing mode is fine for dev; production publishing
has multi-week lead — start console work around M5); Azure app registration before 46;
real keys land in `.env` by operator hand only (agent reads are denied), and
`SERMON_API_TOKEN_ENC_KEY` must be generated + backed up by the operator (loss bricks
every stored refresh token); corpus rights at 23; branch-protection flips (17) are
GitHub admin actions. Product micro-calls pre-answered in the prompts: week start
Sunday; hard read-only lock while linked; explicit user choice on unlink; block-level
citation card with cached snippet.

---

## Phase 31 — Originals persistence: stop losing uploads

```
cd to sermon.guide. Read worker/ingest.py, worker/db/settings.py, infra/docker-compose.yml
+ the "### B1 — Citation → reader deep-link" section (esp. its time-sensitive
originals sub-item) of the v2 Product Backlog in this file.

Goal: stop losing originals — time-sensitive. Uploads live only in volatile
/tmp/sermon-uploads and MinHash dedup short-circuits second owners before any
write: every book ingested to date has a permanently unrecoverable original,
and each ingest before this lands adds another. Fix per B1 is write-only.

## Build
- Branch: phase-31/originals-persistence off main.
- Originals bucket on the compose MinIO (already running): idempotent
  create-if-missing; creds/endpoint via a new BaseSettings class following the
  worker/db/settings.py pattern; new env vars append-only in infra/.env.example.
- New-book path in worker/ingest.py: upload the original bytes under
  originals/{book_id}/{sanitized-filename}; set the plumbed-but-never-filled
  global_books.text_pointer to that key. Sanitize the user-supplied filename —
  strip path separators/dotdot/control chars; never use it raw in the key.
- Dup-hit path: when the existing book's text_pointer IS NULL, upload the
  second owner's identical bytes and backfill it; already set → no-op, no
  duplicate object. This is the only recovery path pre-phase books will ever get.
- Scope fence per B1: NO read endpoint (zero new tenant read surface) and NO
  migration (text_pointer column exists since Phase 7).
- Pick boto3 vs minio-py (same S3 API as the future R2/B2 swap); record the
  choice + the write-failure posture (fail the ingest vs log-and-continue) in
  worker/AGENTS.md.

## Verify
- Fresh ingest of a new book → object lands at originals/{book_id}/... in the
  bucket AND global_books.text_pointer holds that key.
- Re-upload the same content as a second user: NULL pointer → backfilled;
  already-set pointer → unchanged, still one object.
- Hostile filename (../, slashes) → key stays under originals/{book_id}/.
- Gates: make test-isolation (ingestion touched; stays green) + /check-tenant-leak
  + tenant-auditor + /security-review (user filename becomes an object key).
```

---

## Phase 32 — Reader API: windowed chunks + reading positions

```
cd to sermon.guide. Read api/library.py, api/main.py, worker/db/models.py + the
"### B1 — Citation → reader deep-link" section of the v2 Product Backlog in this file.

Goal: the reader's data layer per B1 Data/API — tenant-gated windowed chunk
reads plus persisted reading positions. chunks already holds every book's full
text in dense chunk_index order: one migration + three endpoints, no new storage.

## Build
- Branch: phase-32/reader-api off main.
- reading_positions + migration at the next free migration number (0004 at
  authoring time = highest merged + 1; renumber + re-point down_revision if
  scooped); doubly-scoped like highlights, UNIQUE(user_id, book_id) — shape per B1.
- GET /books/{book_id}/chunks?start&limit — default 40, cap 100, chunk_index
  order; 404 unless the book is in the JWT user's user_library (no existence
  oracle). chunks has no user_id by design — membership IS the tenant gate;
  build it as a testable statement builder per api/library.py _library_stmt.
- GET/PUT /books/{book_id}/position — upsert on the UNIQUE constraint, same
  404 gate; PUT model extra="forbid"; user_id from the JWT, never the body.
- GET /library gains per-book progress. Trap, verbatim from B1: the /library
  join to reading_positions MUST be ON (user_id AND book_id) — joining on
  book_id alone leaks another tenant's reading position for a shared deduped book.
- Decide in-phase (B1 open questions): offset_ratio in or out of the first
  cut; chunk_count denormalized onto global_books or computed per request.

## Verify
- Owned book: default 40; ?limit=500 capped at 100; start past end → empty list.
- Non-owned and nonexistent book_id → identical 404s on /chunks and /position.
- PUT /position twice → one upserted row; smuggled extra body field → 422.
- Two users sharing one deduped book each see only their own position and
  progress on GET /library — the join trap, checked live.
- Gates: make test-isolation + /check-tenant-leak + tenant-auditor +
  /security-review + schema-reviewer on the migration.

## Close out
- Row records the migration number taken + offset_ratio/chunk_count decisions.
```

---

## Phase 33 — Reader UI: /read/[bookId]?chunk=N

```
cd to sermon.guide. Read web/components/SearchPanel.tsx, web/components/LibraryTable.tsx,
web/middleware.ts, web/app/api/tasks/[taskId]/route.ts (the same-origin proxy exemplar)
+ the "### B1 — Citation → reader deep-link" section of the v2 Product Backlog in this file.

Goal: the in-app reader per B1 Web — /read/[bookId]?chunk=N on the Phase 32 API;
citation cards gain "Read in context", library rows "Continue reading". Deps: Phase 32.

## Build
- Branch: phase-33/reader-ui off main.
- Same-origin proxies per the Phase 15/16 pattern — app/api/books/[bookId]/chunks
  (GET) + .../position (GET/PUT): cookie forwarded, structural whitelist on PUT body.
- app/read/[bookId]/page.tsx: bidirectional windowed scroll per B1 Web —
  IntersectionObserver sentinels at both ends; manual scrollTop compensation on
  prepend (Safari lacks overflow-anchor); ?chunk=N anchor-scroll + tint; plain
  DOM first, virtualization only on observed jank.
- Rendering: react-markdown WITHOUT rehype-raw (raw HTML stays inert — keeps the
  repo's zero-dangerouslySetInnerHTML stance); img stubbed to alt text; links
  rel=noopener; plain-text fallback acceptable if the dep is vetoed.
- Position persistence: debounced PUT on scroll-settle + pagehide flush via
  fetch keepalive.
- Entry points: "Read in context" on citation cards (SearchPanel already has
  book_id + chunk_index); "Continue reading" + progress on library rows.
- middleware.ts matcher gains "/read/:path*" (today only library/search/upload);
  nav stays card/row-driven — the two entry points above, no top-nav link.

## Verify
- Citation card → /read/[bookId]?chunk=N lands anchored on the tinted chunk;
  scroll up prepends with no viewport jump; scroll down reaches the book's end.
- Close the tab mid-read → /library shows progress and Continue reading resumes
  at the saved chunk (pagehide keepalive landed).
- Raw HTML in a chunk renders as inert text, images as alt text; grep web/ for
  dangerouslySetInnerHTML → still zero.
- Unauthenticated /read/... → middleware redirect to /login.
- Gates: pnpm typecheck + lint + test (tsc/biome/vitest) + /security-review
  (new proxies + user-navigable surface); tenant suites ran in Phase 32.
```

---

## Phase 34 — Documents schema + API

```
cd to sermon.guide. Read api/highlight.py (user-owned CRUD precedent),
worker/db/models.py, worker/db/alembic/versions/ (current numbering + style) + the
"### B2 — In-app sermon document editor" section and "Cross-item contracts" of the
v2 Product Backlog in this file.

Goal: the storage + API half of the sermon editor (B2 slice A). Canonical sermon
storage is TipTap/ProseMirror JSON per the Cross-item contracts; Phases 35–37 build
the web side on this surface. No web changes in this phase.

## Build
- Branch: phase-34/documents-api off main.
- documents table + Alembic migration at the next free migration number (0004 at
  authoring time — renumber + re-point down_revision if another phase lands first):
  columns per B2 Data/API, including content JSONB (ProseMirror JSON), server-derived
  content_text, schema_version, deleted_at soft delete, idx (user_id, updated_at DESC).
- api/documents.py: POST / GET list (non-deleted, content_text preview) / GET / PATCH /
  DELETE (soft) / restore (Phase 36's list UX consumes it). PATCH carries
  base_updated_at → 409 on mismatch (single-author optimistic concurrency per B2).
- Request models Pydantic extra="forbid" (Phase 18 posture). content_text is NEVER
  accepted from the client — the server re-derives it from content on every write.
  Reject content over ~2 MB with 413.
- Every query filters user_id from the JWT; non-owned document_id → 404 (no existence
  oracle — the Phase 20 /tasks posture).

## Verify
- curl round-trip: create → list shows preview → PATCH with stale base_updated_at →
  409, fresh → 200; smuggled user_id field → 422; >2 MB content → 413; soft-deleted
  doc vanishes from list, GET → 404, restore returns it intact.
- Ownership test in api/tests: user B GET/PATCH/DELETE user A's document → 404 each.
- Gates: make test-isolation + /check-tenant-leak + tenant-auditor + /security-review
  (new user-input surface) + schema-reviewer on the migration.
```

---

## Phase 35 — Editor shell: /sermons + TipTap

```
cd to sermon.guide. Read web/middleware.ts, web/app/library/page.tsx (server-list
precedent), web/app/api/search-summary/route.ts (proxy pattern), web/AGENTS.md + the
"### B2 — In-app sermon document editor" section of the v2 Product Backlog in this file.

Goal: pastors get a working manuscript editor (B2 slice B) — a /sermons list plus a
TipTap editor with explicit save. Autosave (36) and citations (37) stack on this
shell. Depends on Phase 34's documents API.

## Build
- Branch: phase-35/editor-shell off main.
- pnpm add @tiptap/react @tiptap/pm @tiptap/starter-kit @tiptap/extension-placeholder —
  MIT core only, NEVER a Pro extension (B2 Approach); editor code stays confined to
  the editor route by App Router code-splitting.
- /sermons: server-component list per the /library precedent — title, content_text
  preview, updated_at, "new sermon" create flow.
- /sermons/[documentId]: server shell → "use client" editor; useEditor with
  immediatelyRender: false (App Router SSR requirement per B2); StarterKit +
  Placeholder; fixed toolbar; editable title; explicit Save sending doc JSON +
  base_updated_at — no autosave yet (Phase 36).
- Same-origin proxy route handlers for documents CRUD under web/app/api/documents/
  (structural field whitelists + HttpOnly cookie pass-through — Phase 15/16 pattern);
  add /sermons to the middleware.ts matcher and the shared nav.
- TipTap is headless contenteditable — zero dangerouslySetInnerHTML (repo invariant);
  previews render content_text as plain text.

## Verify
- Cookie-jar live drive (Phase 15/16 precedent): login → create via proxy → PATCH
  content JSON → GET round-trips it; unauthenticated /sermons redirects to /login.
- Browser pass for what cookie jars can't type (B2 cross-phase note): create, type,
  Save, reload → content persists; a stale-tab Save surfaces the 409 as an error
  (full conflict UX is Phase 36). Extend the Phase 25 Playwright suite with an editor
  smoke if it reaches cheaply.
- Gates: pnpm tsc + biome + vitest + /security-review (new cookie-forwarding proxy
  surface); no DB/Milvus queries touched — API tenant gates ran in Phase 34.
```

---

## Phase 36 — Editor autosave + conflict + soft-delete UX

```
cd to sermon.guide. Read the Phase 35 editor under web/app/sermons/ (+ its client
component), the web/app/api/documents/ proxies, api/AGENTS.md (Phase 19 limiter
entry) + the "### B2 — In-app sermon document editor" section of the v2 Product
Backlog in this file.

Goal: the editor stops losing work (B2 slice C) — autosave with a tab-close flush,
visible save state, honest conflict handling, delete/restore in the list. Pure web UX
on the Phase 34/35 surface; the only api/ touch allowed is the Phase 19 limiter config.

## Build
- Branch: phase-36/editor-autosave off main.
- Autosave per B2 Web: ~2 s debounce + 15 s max-interval; one in-flight PATCH at a
  time; after every 200, adopt the response updated_at as the next base_updated_at —
  reusing a stale base manufactures spurious 409s.
- pagehide flush via fetch keepalive with the ~64 KB body ceiling guarded: oversize
  docs skip the flush (they save on next open per B2) instead of throwing.
- SaveStatus indicator: saved / saving / error / conflict.
- 409 → conflict banner: stop autosaving, offer reload-latest; never silently clobber
  either side.
- Delete (soft) + restore actions on the /sermons list against the Phase 34 endpoints.
- Phase 19 landed earlier in the queue: confirm its limiter tolerates ~1 PATCH/2 s
  sustained autosave; widen the bucket in-phase if needed and record it in
  api/AGENTS.md.

## Verify
- Continuous typing: PATCHes coalesce to the debounce cadence, the max-interval save
  fires, SaveStatus cycles, and logs show zero 429s under sustained typing.
- Two tabs on one doc: the stale tab's save → 409 banner with a working reload.
- Edit → close tab → reopen: last state persisted via the keepalive flush; a >64 KB
  doc closes without errors and saves on next open.
- Delete from list → gone (GET 404); restore returns it with content intact.
- Gates: pnpm tsc + biome + vitest; if the limiter config changed, api make lint
  typecheck + /security-review.
```

---

## Phase 37 — Citation node + insert-from-search

```
cd to sermon.guide. Read web/components/SearchPanel.tsx, api/search.py (POST /search
contract), web/app/api/search-summary/route.ts (proxy precedent), the Phase 35 editor
component + the "### B2 — In-app sermon document editor" section and "Cross-item
contracts" of the v2 Product Backlog in this file.

Goal: the signature integration (B2 slice D) — cited passages from library search
become first-class blocks in the manuscript, deep-linking into the reader. Depends on
Phase 35; the click-through live verify needs Phase 33's /read route.

## Build
- Branch: phase-37/citation-node off main.
- Block-level citation atom via ReactNodeViewRenderer, attrs {bookId, chunkIndex,
  bookTitle, snippet, parentSection?} per the Cross-item contracts; snippet is cached
  at insert so the doc stays self-contained — the node view NEVER refetches on render.
  Styled like the /search citation cards, snippet as plain text (zero-
  dangerouslySetInnerHTML invariant), linking to /read/{bookId}?chunk={chunkIndex}.
- Degraded state: book no longer in the user's library → render the cached snippet
  with a "no longer in your library" badge, no refetch, no error.
- In-editor LibraryDrawer reusing the SearchPanel plumbing against a NEW thin proxy
  web/app/api/search/route.ts → existing POST /search (raw hybrid hits, no LLM
  round-trip): structural whitelist forwards {query} only, HttpOnly cookie
  pass-through (Phase 15/16 pattern).
- Insert from a drawer hit via editor.chain().insertContent with the citation attrs;
  the node must survive save → reload → re-render through documents.content JSON.

## Verify
- Live drive: open a doc → drawer search returns raw hits (no LLM wait) → insert →
  card shows title + snippet → save + reload intact → click → /read/{bookId}?chunk=N
  opens at the cited passage (Phase 33 live).
- Remove the cited book from the library, reopen the doc: cached snippet + degraded
  badge, zero per-citation network fetches.
- Proxy: smuggled extra fields dropped by the whitelist; unauthenticated call → 401.
- Gates — the new /api/search proxy is a new tenant-facing surface, re-run ALL of:
  make test-isolation + /check-tenant-leak + tenant-auditor + /security-review +
  pnpm tsc + biome + vitest.
```

---

## Phase 38 — Calendar schema + API

```
cd to sermon.guide. Read api/highlight.py (double-scoped CRUD precedent),
api/documents.py (Phase 34 FK target), worker/db/models.py + the
"### B3 — Sermon calendar" section of the v2 Product Backlog in this file.

Goal: the calendar's whole server side in one slice — sermon_events + api/calendar.py
CRUD with the weekly materializer. Phases 39–42 are pure web on top of this API.

## Build
- Branch: phase-38/calendar-api off main.
- Migration at the next free number (0004 at authoring time; renumber + re-point
  down_revision if scooped): sermon_events per B3 Data/API. Exact traps: event_date
  is DATE, not timestamptz (day-anchored; UTC-midnight shifts a day for UTC-minus
  users); document_id NULL FK→documents ON DELETE SET NULL; idx (user_id,
  event_date); deliberately NO unique on (user_id, event_date) — two services one
  Sunday is normal.
- api/calendar.py (router in main.py): GET half-open [start, end), validated +
  capped ≤ ~400 days; POST with optional repeat_weekly_until materializing discrete
  rows, cap ~53 (B3 Recurrence — no RRULE); PATCH partial; DELETE. All double-scoped
  (event_id AND user_id), non-owned → 404; builders testable per api/library.py
  _library_stmt; request models extra='forbid' (cross-item contract).
- TRAP — document_id is attacker-controlled body input: ownership-check on
  POST/PATCH against documents WHERE user_id = JWT user (422/404 on miss), or user B
  links user A's doc and the calendar leaks its existence/title. The FK + this check
  ship IN THIS PHASE (documents landed in 34).

## Verify
- Range GET: own events only; event dated `end` excluded (half-open); span > cap →
  422. repeat_weekly_until → capped row count, each row independently PATCH/DELETE.
- Another user's document_id on POST/PATCH → 422/404; another user's event_id →
  404. Deleting a linked document leaves the event, document_id NULL.
- Gates: make test-isolation + /check-tenant-leak + tenant-auditor +
  /security-review + schema-reviewer on the migration.

## Close out
- Record the chosen range cap + materializer cap in the row (B3 open question).
```

---

## Phase 39 — Calendar year + month views, read-only

```
cd to sermon.guide. Read web/middleware.ts, web/app/layout.tsx,
web/app/api/search-summary/route.ts (the proxy exemplar), api/calendar_routes.py (Phase 38)
+ the "### B3 — Sermon calendar" section of the v2 Product Backlog in this file.

Goal: the headline year wall-planner plus the month view, read-only — all 12 months
on one screen before any CRUD UX exists. Custom Tailwind CSS-grid, ZERO new runtime
deps (B3 Approach; FullCalendar is the documented fallback, don't reach for it).

## Build
- Branch: phase-39/calendar-year-month off main.
- web/lib/dates.ts: pure YYYY-MM-DD string helpers (month grids, ranges; week starts
  Sunday — settled, keep it a named constant), vitest-pinned. NEVER
  new Date("YYYY-MM-DD") anywhere — UTC parse shifts the day (B3 Dates).
- Same-origin proxy web/app/api/sermon-events/route.ts (Phase 15/16 pattern:
  cookie→Bearer, structural whitelist of start/end). ONE range fetch drives both
  views.
- /calendar page, URL state ?view=year|month&date=YYYY-MM-DD (linkable): CalendarYear
  = grid-cols-3/4 of 12 MiniMonth (grid-cols-7 DayCells; ≤2 series-colored dots +
  popover at small sizes, truncated title at ≥~36px cells); month = larger DayCell,
  ≤3 chips + "+N more" — per B3 Web.
- middleware.ts matcher gains "/calendar/:path*"; nav link added in app/layout.tsx.
- Event titles render as text nodes only — the repo's zero-dangerouslySetInnerHTML
  stance holds.

## Verify
- /calendar?view=year: 12 aligned months (spot-check a leap-year February and a
  month starting on Sunday); event days show dots; ?view=month&date=… deep-links;
  unauthenticated /calendar redirects to /login.
- vitest pins dates.ts over month/year boundaries + leap years; grep shows no
  new Date( on date strings under web/app/calendar or web/lib/dates.ts.
- Gates: /security-review (new proxy = new input surface) + pnpm
  typecheck/lint/test.
```

---

## Phase 40 — Calendar week view + event CRUD UX

```
cd to sermon.guide. Read web/app/calendar/ (Phase 39 components), web/lib/dates.ts,
web/app/api/sermon-events/route.ts + the "### B3 — Sermon calendar" section of the
v2 Product Backlog in this file.

Goal: the calendar goes read-write — week view, quick create with weekly repeat,
edit/delete from chips, deterministic series colors. Still zero new runtime deps.

## Build
- Branch: phase-40/calendar-week-crud off main.
- Week view: 7 day columns of full event cards; ?view=week joins the URL state;
  same single range fetch; dates.ts gains week helpers (vitest-pinned, Sunday
  start).
- QuickCreatePopover on empty-day click: title, series, optional weekly-repeat-until
  → POST (the Phase 38 materializer caps rows server-side). Edit/delete popover on
  chips/cards → PATCH/DELETE.
- Proxy mutations: POST on web/app/api/sermon-events/route.ts, PATCH/DELETE on
  …/sermon-events/[eventId]/route.ts — structural whitelists (title, series,
  event_date, repeat_weekly_until; document_id waits for Phase 41), Phase 15/16
  pattern.
- Series→color: hash the series string into a fixed map of literal Tailwind classes
  (runtime-built class strings won't compile — Tailwind only sees literals); same
  color across year/month/week (B3 Web).
- Density polish per B3: dot/chip caps and "+N more" thresholds across views.

## Verify
- Create on an empty day → appears in year, month, and week; weekly repeat shows
  the capped run; edit title and delete round-trip across all views.
- Same series string = same color everywhere, stable across reloads; vitest pins
  the hash. User text renders as text nodes (no dangerouslySetInnerHTML).
- Gates: /security-review (new mutation proxies + form input) + pnpm
  typecheck/lint/test.
```

---

## Phase 41 — Calendar-editor linking

```
cd to sermon.guide. Read web/app/calendar/ (Phase 39/40), web/app/sermons/ (Phase 35
editor entry), api/documents.py + the "### B3 — Sermon calendar" section of the v2
Product Backlog in this file.

Goal: calendar ↔ manuscript linking. Pure UX — the document_id FK and its ownership
check landed in Phase 38; this phase wires flows over existing endpoints. No schema
or API changes expected.

## Build
- Branch: phase-41/calendar-editor-link off main.
- Linked event chip/card click → /sermons/{document_id}; unlinked click keeps the
  Phase 40 edit popover.
- Create-doc-from-empty-date: POST /documents (title prefilled from the event),
  then PATCH the event's document_id, then navigate into the editor — two existing
  calls, no new endpoint.
- Link/unlink in the edit popover: picker fed by the Phase 35 documents list proxy
  (own docs only by construction); unlink = PATCH document_id: null. The event
  PATCH proxy whitelist gains document_id — explicit null must pass it.
- Surface the Phase 38 ownership 422/404 as a visible error state; never swallow it.

## Verify
- Click linked event → editor opens the right doc; create-from-date → doc exists
  and the event shows its linked state; unlink clears it.
- Delete the doc in /sermons → the event survives with document_id NULL
  (ON DELETE SET NULL, cross-item contract).
- curl PATCH with another user's document_id still → 422/404 (Phase 38 regression).
- Gates: /security-review (proxy whitelist gains document_id) + pnpm
  typecheck/lint/test.
```

---

## Phase 42 — Drag-to-reschedule + calendar E2E

```
cd to sermon.guide. Read web/app/calendar/ (chips + DayCells), the Phase 25
Playwright harness under web/ (config + one existing spec), web/AGENTS.md + the
"### B3 — Sermon calendar" section of the v2 Product Backlog in this file.

Goal: reschedule by dragging — the last B3 slice — plus the calendar's regression
suite riding the Phase 25 harness.

## Build
- Branch: phase-42/calendar-drag off main.
- Native HTML5 DnD, zero new runtime deps: EventChip draggable (payload =
  event_id), DayCell a drop target, working in year, month, and week views.
- Optimistic PATCH event_date on drop: move the chip immediately; on failure roll
  back and show a visible error.
- Keyboard-accessible fallback per B3 Web: a move-to-date control in the Phase 40
  edit popover — HTML5 DnD is mouse-only, this is the only accessible path.
- Playwright specs on the Phase 25 harness, wired into its CI job: login → create
  event → visible in all three views; drag to another day → persists after reload.

## Verify
- Drag a chip to another day in each view: exactly one PATCH fires, the chip moves,
  reload persists it. Stop the api (or force a 500) and drag → chip snaps back +
  error visible.
- Keyboard-only reschedule succeeds via the popover fallback.
- Playwright suite green locally and in CI; deliberately breaking the drop handler
  turns it red.
- Gates: pnpm typecheck/lint/test + the Playwright suite (web-only; no server code
  touched).
```

---

## Phase 43 — .docx round-trip core (export/import + revision snapshots)

```
cd to sermon.guide. Read api/uploads.py ("Security choices" docstring + /tmp
staging pattern), api/documents.py, api/AGENTS.md + the "### B4 — External
editor round-trip" section of the v2 Product Backlog in this file.

Goal: B4's v2-MINIMAL slice — .docx download/import, zero OAuth. Hard deps:
Phases 34 (documents) + 37 (citation nodes) merged — the round-trip exists to
carry sermon structure + citation /read hyperlinks.

## Build
- Branch: phase-43/docx-roundtrip off main.
- worker/convert.py pandoc seam per B4 + the Cross-item contracts: export =
  content JSON → @tiptap/html generateHTML (server-side Node, no browser DOM)
  → pandoc html→docx with a --reference-doc template under worker/assets;
  import = docx → pandoc → HTML → generateJSON. Pandoc legs live in
  worker/convert.py; decide the Node-leg placement in-phase and record it.
- pandoc becomes an api system dep (apt in api/Dockerfile, or an api/AGENTS.md
  note for Phase 29 to bake — the queue runs 43 first) and the api/AGENTS.md
  allowed-import surface gains worker/convert.py (Phases 11/12/16b precedent).
- sermon_doc_revisions + Alembic migration at the next free number (0004 at
  authoring time; renumber + re-point down_revision if scooped). Every import
  snapshots prior app content FIRST — last-writer-wins never destroys anything.
- GET /sermons/{id}/export.docx + POST /sermons/{id}/import: multipart,
  size-capped, /tmp staging per api/uploads.py; non-owned id → 404; request
  models extra='forbid'.
- Web: Download/Import editor UI via same-origin proxy route handlers (Phase
  15/16 pattern); imports render as TipTap JSON — zero dangerouslySetInnerHTML.

## Verify
- Golden round-trip test — THE phase gate: a citation-bearing sermon exported
  then re-imported keeps structure + every citation /read hyperlink, with the
  snapshot revision row predating the overwrite.
- Oversized / non-docx multipart → 4xx; staged /tmp files cleaned up either way.
- Gates: schema-reviewer (migration) + /check-tenant-leak + tenant-auditor +
  /security-review (upload surface) + make test-isolation + api make
  lint/typecheck/test + pnpm tsc/biome.
```

---

## Phase 44 — OAuth connection vault

```
cd to sermon.guide. Read api/auth.py, api/settings.py, infra/.env.example +
the "### B4 — External editor round-trip" section of the v2 Product Backlog in
this file.

OPERATOR CHECKPOINT FIRST: STOP and notify unless the operator confirms a GCP
project + OAuth consent screen + client id/secret in .env (agents cannot read
or write .env — ask, never peek; Testing mode is fine for dev: 7-day refresh
expiry) and has generated AND backed up SERMON_API_TOKEN_ENC_KEY (loss bricks
every stored token). Goal: the provider-agnostic vault Phases 45/46 sit on.

## Build
- Branch: phase-44/oauth-vault off main.
- oauth_connections per B4 (UNIQUE(user_id, provider), refresh_token_ciphertext
  BYTEA) + Alembic migration at the next free number (0004 at authoring time;
  renumber + re-point down_revision if scooped). App-layer AESGCM via the
  cryptography package — promote it to an explicit api dep — keyed by
  SERMON_API_TOKEN_ENC_KEY, documented append-only in infra/.env.example.
- api/integrations.py: GET /integrations, authorize, callback, DELETE revoke.
  HMAC-bind state to user_id + nonce + ~10-min expiry, plus PKCE S256; validate
  BOTH at the callback BEFORE the code exchange — the account-binding CSRF
  defense (an attacker otherwise binds their account to a victim session and
  exfiltrates pulled sermons). Thin httpx, no SDKs (ADR 0005/0006 precedent).
- Web: /api/integrations/{provider}/callback route handler is the PUBLIC
  redirect URI (top-level redirect onto the web origin — SameSite=Lax survives);
  /settings/integrations page + nav entry + middleware.ts matcher coverage.
- Tokens never reach the browser and never hit logs (Phase 27 will redact).

## Verify
- Live against the Testing-mode Google project: connect → row + provider_email
  on /settings/integrations; tampered/expired/wrong-user state rejected BEFORE
  any token POST; DELETE revokes and removes the row; DB holds ciphertext
  only; grep api logs — zero token material.
- Gates: schema-reviewer (migration) + /check-tenant-leak + tenant-auditor +
  /security-review (new auth surface) + make test-isolation + api make
  lint/typecheck/test + pnpm tsc/biome.
```

---

## Phase 45 — Google Docs link/pull/unlink (spike-first)

```
cd to sermon.guide. Read worker/convert.py, api/integrations.py,
api/documents.py + the "### B4 — External editor round-trip" section of the v2
Product Backlog in this file.

Goal: check-out/check-in to Google Docs over Phase 43's seam + Phase 44's vault
(hard deps). NOT merge — while linked, the external copy is source of truth.

## Build
- Branch: phase-45/google-docs-link off main.
- FIRST, B4's mandated empirical spike (its one UNCLEAR fact-check): export a
  citation-bearing docx, Drive files.create upload-with-conversion to a native
  Doc, export back, assert /read hyperlinks survived. If not, STOP — write the
  bail-to-fallback decision (docx-in-Drive, no conversion) into the PR for
  operator sign-off.
- editor_links per B4 (provider_file_id, web_url, state linked|error|unlinked,
  last_remote_version — a cursor, compared never parsed; partial UNIQUE: one
  linked editor per document) + Alembic migration at the next free number
  (0004 at authoring time; renumber + re-point down_revision if scooped).
- api/editor_links.py: POST link (export+upload+row), GET status
  (remote_changed via Drive files.version), POST pull (snapshot to
  sermon_doc_revisions FIRST; prefer files.export text/markdown → pandoc, else
  the docx leg), POST unlink (explicit choice, settled: pull-final vs
  keep-app). document_id / provider file ids are untrusted: non-owned → 404.
- Hard read-only editor lock while linked (settled), "Editing externally"
  banner with Open / Pull changes / Unlink; same-origin proxies for all routes.

## Verify
- Live: link → native Doc at web_url; remote edit → remote_changed=true; pull
  → snapshot row precedes the content update + citation /read links survive;
  unlink offers both choices; second link while linked → 409.
- Gates: schema-reviewer (migration), /check-tenant-leak + tenant-auditor,
  /security-review, make test-isolation, api lint/typecheck/test, pnpm tsc/biome.

## Close out
- Record the spike outcome and which pull leg (markdown vs docx) ships primary.
```

---

## Phase 46 — Microsoft Graph provider

```
cd to sermon.guide. Read api/integrations.py, api/editor_links.py,
worker/convert.py + the "### B4 — External editor round-trip" section of the
v2 Product Backlog in this file.

OPERATOR CHECKPOINT FIRST: STOP and notify unless an Azure app registration +
client id/secret exist in .env (agents cannot read .env — ask, never peek).
Goal: Microsoft Graph as provider #2 over the SAME Phase 45 editor_links
surface — no new tables, no migration; proves vault + link schema are
provider-agnostic.

## Build
- Branch: phase-46/msgraph-link off main.
- Graph leg per B4: the sermon travels as docx in OneDrive (no native
  conversion) — simple PUT under the size cap, createUploadSession above it;
  staleness via eTag (compared, never parsed — same cursor rule as Drive);
  pull = download-back → pandoc through worker/convert.py.
- Vault refresh path must handle MSA rotation-on-redemption: every refresh
  redemption returns a NEW refresh token (90-day sliding window) that must be
  re-encrypted and persisted immediately — keeping the old one bricks the
  connection at the next refresh.
- Wire provider='microsoft' through authorize/callback/revoke,
  /settings/integrations, and the editor banner; thin httpx, no Graph SDK.

## Verify
- Live against an MSA account: connect, link a sermon, edit in Word Online →
  GET status flags remote_changed via eTag, pull snapshots then updates,
  unlink offers both choices; citation /read hyperlinks survive the docx legs.
- Rotation proof: force two consecutive refreshes — the second succeeds and
  the stored ciphertext changed between them (rotated token persisted).
- An oversized doc exercises (or a unit test pins) the createUploadSession
  branch.
- Gates: /check-tenant-leak + tenant-auditor + /security-review + make
  test-isolation + api make lint/typecheck/test + pnpm tsc/biome (no
  migration → no schema-reviewer).
```

---

## Parked — trigger-gated, blocked, or v2+

Deliberately NOT scheduled. Each has a written unblock trigger; when one fires,
plan it as the next phase number.

| Item | Why parked | Unblock trigger |
|---|---|---|
| GPU swap (embedding/rerank/highlight → cuda) | No GPU hardware exists; this is the real latency lever (~30 s rerank, ~134 s summary E2E) | GPU runtime provisioned → swap the device pins per ADR 0003 + api/AGENTS.md |
| google LLM arm live verify | Config-verified only; byte-identical code path to the live-verified ppq arm (ADR 0005) | GOOGLE_API_KEY provisioned → run the Phase 14b verify triad |
| R2/B2 object storage | /tmp staging + Phase 20 idempotency + Phase 28 backups cover single-box risk; couples to the deploy provider | Multi-node k8s (post-Phase 30) makes shared object storage mandatory; B1 (v2 backlog) starts persisting originals to the compose MinIO over the same S3 API, so this swap becomes endpoint+credentials when it fires |
| Email verification / CAPTCHA on signup | Phase 19 throttling covers abuse pre-launch | Real public signups |
| Highlight/note import (Kindle, Logos) | Blocked on ARCHITECTURE.md §7.2 (separate collection vs content_type) — deliberately deferred | Real highlight queries exist → decide §7.2 (ADR), then schedule |
| Hierarchical / parent-document retrieval | Quality enhancement; no correctness pressure | Phase 27 query logs show flat retrieval failing |
| Semantic query caching | Premature without a real query distribution | Repeated-query data from Phase 27 metrics justifies it |
| Per-tenant rate limits / quotas | Needs a usage/billing model; Phase 19 covers the abuse edge | Tenancy/billing model decided |
| Graph RAG | Research-grade; official v2 backlog | Deliberate product decision |
| Multilingual BM25 (language column + per-row regconfig) | English-only corpus; ADR 0004 names the path | A non-English corpus lands |
| ParadeDB pg_search (true BM25) | ts_rank_cd passes all goldens | A golden regression attributable to ranking |
| Postmortems dir (agent_docs/postmortems/) | Empty-dir busywork | First real postmortem (create the dir with it) |
| Additional MCP servers (GitHub MCP, Context7) | Opt-in dev tooling; enableAllProjectMcpServers stays false | Explicit per-tool need |
| B2-E sermon niceties (series/date/passage metadata, scripture-reference detection, preacher mode, word count, print stylesheet) | Polish; the core write loop (Phases 34–37) ships without it | Editor in weekly real use → schedule as one phase |
| B4 ext-E background freshness (Celery beat poll + Drive watch channels / Graph subscriptions + renewal job) | No Celery beat service exists; both providers require publicly-trusted HTTPS on a verified domain — deploy is IP-only `tls internal` | Real domain + Let's Encrypt flip per docs/DEPLOY_AWS.md → schedule together with a beat service |

When Phases 17–30 are done, re-audit the repo — same drill as 2026-06-05 — scoped to
hardening gaps and deviations: the sermon-workflow features are already planned as
Phases 31–46 (see the **v2 Plan** section above) with the **v2 Product Backlog** below
as their canonical design. Each consuming phase re-verifies the backlog's dated
external facts as it runs.

---

## v2 Product Backlog — sermon workflow (captured 2026-06-10)

User-requested product direction, captured and pre-designed so the post-Phase-30 v2
drill starts from requirements, not memory. The ask: (1) click a citation → the book
opens at that passage and you keep reading; (2) write sermons in the app; (3) schedule
them on an in-app calendar — year view ("each month with each day in a box, a huge
spreadsheet"), plus month and week views; (4) export a sermon to the user's preferred
editor and have saves there land back in this app ("maybe we can link with api?").
Together they close the product loop: search → cite → write → schedule → preach.

Provenance: four parallel repo-grounded design passes + a 43-claim live web fact-check
(2026-06-10: 41 confirmed, 1 refuted, 1 unclear — load-bearing ones marked "verified"
inline). B-ids remain stable references and are now numbered (2026-06-10): B1 →
Phases 31–33, B2 → 34–37, B3 → 38–42, B4 → 43–46 — see the **v2 Plan — Sermon
Workflow** section above. Each consuming phase MUST re-verify the dated facts it
leans on (licenses, API capabilities, and library status drift).

### Cross-item contracts (settle once — every B-item leans on them)

- **Canonical sermon format = TipTap/ProseMirror JSON** in `documents.content` JSONB
  (B2 decides, B4 consumes). Export leg: doc JSON → `@tiptap/html generateHTML`
  (runs server-side in Node, no browser DOM — verified) → pandoc HTML↔docx
  (hyperlinks survive both directions — verified). Markdown-canonical was rejected:
  a citation node's structured attrs cannot round-trip through string syntax without
  corrupting on edit.
- **Table `documents`, UI route `/sermons`** — naming leaves room for non-sermon docs
  (study notes) later. Calendar links via `sermon_events.document_id` FK
  ON DELETE SET NULL.
- **Reader route `/read/[bookId]?chunk=N`** (B1). The B2 citation node carries
  `{bookId, chunkIndex, bookTitle, snippet, parentSection?}` and links there. In
  docx/Google exports the citation serializes as a hyperlink to that URL — URLs
  survive docx and Google Docs, `data-*` attributes do not (verified).
- **Tenant rule unchanged and non-negotiable.** `documents`, `reading_positions`,
  `sermon_events`, `sermon_doc_revisions`, `oauth_connections`, `editor_links` are
  all user data: every query filters `user_id` from the JWT; path/body ids are never
  capabilities (non-owned → 404, no existence oracle); web stays HttpOnly-cookie +
  same-origin proxies with structural field whitelists (Phase 15/16 pattern). All
  three tenant gates + `/security-review` per phase, `schema-reviewer` per migration.
- Migrations stay hand-written, numbered 0004+ — B-items race each other for numbers;
  first to land takes the next slot.
- New Pydantic request models adopt `extra='forbid'` from day one (Phase 18 posture),
  whether or not Phase 18 has landed.

### B1 — Citation → reader deep-link ("read in context")

**What:** clicking a citation card on `/search` opens the book in an in-app reader at
the cited passage; scroll both directions through the whole book; reading position
persists so `/library` offers "continue reading".

**Approach — tiered.** Chunk-stitched reader now: Postgres `chunks` already holds the
full extracted book text in dense order (`chunk_index` 0..N−1, unique per book —
worker/ingest.py, worker/db/models.py), so the reader is one tenant-gated windowed
endpoint plus a web page, zero new storage. The full-fidelity original-file reader
(epub.js BSD-2 / pdfjs-dist Apache-2 — both verified viable) stays a later tier behind
the parked R2/B2 item; no chunk→CFI/page mapping exists anyway, so the deep-link
itself needs the chunk reader regardless.

**⚠ Time-sensitive sub-item — persist originals NOW (candidate to pull into v1, e.g.
alongside Phase 20 upload work):** originals live only in volatile `/tmp/sermon-uploads`
(api/settings.py) and MinHash dedup short-circuits second owners before any write
(worker/ingest.py), so every book ingested before this lands has a **permanently
unrecoverable original**. Fix is write-only and cheap: new bucket on the compose MinIO
(already running in infra/docker-compose.yml), populate the plumbed-but-never-filled
`global_books.text_pointer` with `originals/{book_id}/{sanitized-filename}` on new
books, and backfill on dup-hit when NULL — the only recovery mechanism that will ever
exist. No read endpoint until the fidelity tier ships → zero new tenant read surface.
Same S3 API as future R2/B2 (boto3/minio-py — verified), so that swap stays
endpoint+credentials.

**Data/API:** `reading_positions(position_id, user_id FK, book_id FK, chunk_index,
offset_ratio NULL, updated_at, UNIQUE(user_id, book_id))` — doubly-scoped like
`highlights`. `GET /books/{book_id}/chunks?start&limit` (default 40, cap 100; 404
unless the book is in the JWT user's `user_library`); `GET`/`PUT
/books/{book_id}/position` (upsert on the unique constraint); `GET /library` gains
per-book `reading` progress. Web proxies per the Phase 15/16 same-origin pattern.

**Web:** `app/read/[bookId]/page.tsx` — windowed bidirectional infinite scroll
(IntersectionObserver sentinels both ends; manual scrollTop compensation on prepend —
Safari lacks `overflow-anchor`, verified), `?chunk=N` anchor-scroll + tint. Markdown
via react-markdown **without** rehype-raw (raw HTML stays inert — verified; preserves
the zero-`dangerouslySetInnerHTML` invariant), `img` stubbed to alt-text (EPUB-internal
refs never resolve), links `rel=noopener`; plain-text fallback if the dep is vetoed.
Entry points: "Read in context" on citation cards (SearchPanel already has
book_id+chunk_index), "Continue reading" + progress on library rows. Debounced
position PUT on scroll-settle + pagehide flush via `fetch keepalive` (verified).
Virtualization library only on observed jank — ~600 text blocks with windowed fetch is
within plain-DOM budget.

**Tenant traps specific to this repo:** `chunks` is shared-by-design (no user_id) —
the gate is `user_library` membership per request, built as a testable statement
builder (api/library.py `_library_stmt` precedent). The `/library` join to
`reading_positions` MUST be `ON (user_id AND book_id)` — joining on book_id alone
leaks another tenant's reading position for a shared deduped book.

**Open questions:** is `offset_ratio` worth it in the first cut; denormalize
`chunk_count` onto `global_books` or compute per request; `parent_section` TOC/nav in
scope (cleaner after Phase 21 strips the HTML debris)?

### B2 — In-app sermon document editor

**What:** pastors write sermon manuscripts/outlines inside the app, with first-class
cited passages inserted from library search.

**Approach.** TipTap v3 — MIT core + free extensions only, never the paid Pro tier
(verified: MIT, actively maintained, official React 19 + Next 15 App Router support
with `immediatelyRender: false`; Pro = DOCX/comments/collab-cloud, none needed —
pandoc covers docx). Headless, styled with the existing Tailwind; ~100 kB min+gzip
confined to the editor route by App Router code-splitting (verified). Canonical
storage is ProseMirror JSON (cross-item contract) + server-derived `content_text` for
previews/FTS. Single-author v2: optimistic concurrency (`PATCH` carries
`base_updated_at`, 409 on mismatch), soft delete via `deleted_at`, no versions table —
collab-later is additive via Yjs/y-prosemirror/Hocuspocus (all MIT — verified) without
changing `document_id` or the citation-node schema.

**Citation node — the signature integration:** block-level "passage card" atom via
`ReactNodeViewRenderer`, attrs `{bookId, chunkIndex, bookTitle, snippet (cached at
insert), parentSection?}`; styled like the existing /search citation cards; serializes
to the `/read` deep-link hyperlink on export. Snippet is cached so the doc stays
self-contained — if the book later leaves `user_library`, render the cached text with
a "no longer in your library" state instead of refetching. Insert flow: in-editor
LibraryDrawer reusing the SearchPanel plumbing against a new thin `/api/search` proxy
to the existing `POST /search` (raw hybrid hits, no LLM round-trip).

**Data/API:** `documents(document_id, user_id FK, title, content JSONB, content_text,
schema_version, deleted_at, created_at, updated_at; idx (user_id, updated_at DESC);
optional tsv GIN mirroring chunks)`. `api/documents.py`: POST / GET list (non-deleted,
preview) / GET / PATCH (409 stale) / DELETE (soft) / optional restore; content size
cap (~2 MB); server re-derives `content_text` on every write; non-owned → 404.

**Web:** `/sermons` list (server component, /library precedent) + `/sermons/[documentId]`
(server shell → `"use client"` SermonEditor). Deps: @tiptap/react, @tiptap/pm,
@tiptap/starter-kit, @tiptap/extension-placeholder (+@tiptap/html later, server-side).
Autosave: debounced ~2 s + 15 s max-interval + pagehide `fetch keepalive` flush (~64 KB
body ceiling — size-guard, large docs save on next open); SaveStatus indicator;
409 → conflict banner. Middleware matcher + nav entries.

**Cross-phase notes:** Phase 19 rate limiting needs a generous bucket for the chatty
autosave PATCH. Phase 28 backups become materially more important — sermons are
irreplaceable primary user data, unlike re-ingestable books. Phase 25's Playwright
harness is the editor's E2E vehicle (contenteditable cannot be cookie-jar-verified).

**Suggested slices:** (A) schema + API CRUD + ownership tests; (B) editor shell +
explicit save; (C) autosave/conflict/soft-delete UX; (D) citation node +
insert-from-search + `/api/search` proxy (re-run tenant gates); (E, optional) sermon
metadata + niceties (series/date/passage fields, scripture-reference detection mark,
preacher mode, word count/speaking time, print stylesheet).

**Open questions:** block-level passage card only, or also an inline footnote-style
marker (affects docx mapping); tsv column now or at a my-sermons-search phase;
periodic version snapshots as anti-footgun despite keep-it-simple (lean no — soft
delete + Phase 28); scripture detection display-only vs normalized column.

### B3 — Sermon calendar (year wall-planner + month + week)

**What:** schedule sermon documents on dates. Required: a YEAR view — all 12 months at
once, each month a grid of day boxes ("huge spreadsheet" wall-planner feel) — plus
standard month and week views.

**Approach.** Custom Tailwind CSS-grid components, zero new runtime deps. The feature
is exclusively day-anchored, single-day, all-day events at sermon density (1–3/day):
the hard problems calendar libraries solve (time-slot layout, multi-day span packing,
overlap resolution, RRULE expansion) are absent, and the year view needs custom
density work regardless. Fact-check honesty: FullCalendar's `multiMonthYear` IS in the
free MIT standard package with React 19 support (verified) — licensing is NOT why it
lost; fit is (it would be web/'s largest dep to cover the easy 20%, and its
Tailwind-preflight conflicts are real — verified). It remains the documented fallback
if custom month/week proves heavier than expected. Year = `grid-cols-3/4` of 12
MiniMonth components (each `grid-cols-7` of DayCell boxes: ≤2 series-colored dots +
popover at small sizes, truncated title at ≥~36 px cells); month = same DayCell larger
(≤3 chips + "+N more"); week = 7 day columns of full cards. One
`GET /sermon-events?start&end` range fetch drives all views.

**Recurrence:** discrete materialized rows + optional creation-time
`repeat_weekly_until` server-side materializer (cap ~53 rows) + free-text `series`
label — NOT RRULE: every sermon instance diverges immediately (unique title/doc per
week), so RRULE's override/EXDATE machinery is pure cost; discrete rows keep range
queries a single indexed WHERE and make reschedule/cancel a row edit.

**Dates:** `event_date` is Postgres DATE, not timestamptz — preaching is day-anchored
and UTC-midnight timestamps shift a day for UTC-minus users. Dates stay `YYYY-MM-DD`
strings end-to-end in web/ (never `new Date('YYYY-MM-DD')`); pure string helpers in
`web/lib/dates.ts`, vitest-pinned (Phase 15/16 pure-helper culture).

**Data/API:** `sermon_events(event_id, user_id FK, event_date DATE, title, series
NULL, document_id NULL FK→documents ON DELETE SET NULL, created_at, updated_at; idx
(user_id, event_date); deliberately NO unique on (user_id, event_date) — two services
one Sunday is normal)`. `api/calendar.py`: GET range (validated, capped ≤ ~400 days —
year view is one call), POST (+materializer), PATCH partial (drag-to-reschedule is
just `PATCH event_date`), DELETE; all double-scoped `(event_id AND user_id)`.

**Tenant trap — `document_id` is attacker-controlled body input:** on POST/PATCH,
ownership-check it against `documents WHERE user_id = JWT user` (422/404 on miss),
otherwise user B links user A's sermon doc and the calendar leaks its existence/title.

**Web:** `/calendar?view=year|month|week&date=YYYY-MM-DD` (URL state, linkable);
QuickCreatePopover on empty day (title, weekly-repeat, create-doc); click event →
`/sermons/{document_id}` when linked, else inline create; deterministic series→color
hash; drag-to-reschedule via native HTML5 DnD, optimistic PATCH + rollback,
keyboard-accessible fallback in the edit popover.

**Suggested slices:** (1) schema + API CRUD (+materializer, statement-builder tests;
include `document_id` only if `documents` already exists); (2) read-only year + month
views + dates.ts helpers + proxies; (3) week view + CRUD UX + density polish; (4)
editor linking (ownership check + open/create flows; add-column migration if (1)
shipped without it); (5, nice-to-have) drag-to-reschedule + Playwright once Phase 25
exists.

**Open questions:** week start Sunday vs Monday (ship as a lib/dates.ts constant;
per-user setting later?); optional `service_time`/label column for multi-service
Sundays, or never any time in v2; scripture-passage column now or later; series as
free-text + hashed color vs a series table; ICS export / printable wall chart in
scope?; ~~GET range cap value~~ RESOLVED in Phase 38 — GET range cap = **400
days** (half-open span `end - start` > 400 → 422; a year view is one call) and
the weekly-materializer cap = **53 rows** (`repeat_weekly_until` producing > 53
discrete weekly occurrences → 422).

### B4 — External editor round-trip (export + sync-back)

**What:** export a sermon to the user's preferred editor (Google Docs and/or
Microsoft Word/OneDrive, plus a dumb .docx download/upload fallback), continue editing
there, and have saves land back in this app ("maybe we can link with api?" — yes:
Drive API / Microsoft Graph, but staged).

**Sync model — check-out/check-in, NOT continuous two-way merge.** Merge is overkill
and corrupting: the conversion legs are lossy in both directions (silent merge =
silent formatting corruption), sermon prep is single-author weekly cadence, and
webhooks are hard-blocked today anyway — both providers require publicly-trusted HTTPS
on a verified domain (verified) while the deploy is IP-only with `tls internal`
(infra/caddy/Caddyfile). While linked: the external copy is source of truth, the
in-app editor is read-only with an "Editing externally in {provider}" banner (Open /
Pull changes / Unlink), and **every pull/import snapshots the prior app content to
`sermon_doc_revisions` first** — last-writer-wins never destroys anything. Change
detection in v2 is pull-on-open + a manual "Pull changes" button comparing the stored
remote-version cursor (Drive `files.version` / Graph `eTag`, compared not parsed).

**v2-MINIMAL slice ships first and standalone — .docx download/import, zero OAuth:**
new `worker/convert.py` pandoc seam (pandoc is already a worker system dep; add the
apt package to api/Dockerfile and extend api/AGENTS.md's allowed-import list — the
same deliberate extension Phases 11/12/16b made). Export: content JSONB →
`generateHTML` → pandoc HTML→docx with a `--reference-doc` template in worker/assets;
import: docx → pandoc → HTML → `generateJSON`, snapshot first. Citations travel as
`/read` hyperlinks (verified surviving pandoc and Google Docs). Note: the original
design pass assumed markdown-canonical (`content_md`) — superseded by the cross-item
contract (B2's ProseMirror JSON + the verified generateHTML→pandoc leg); fidelity is
equal or better and the citation attrs stay lossless in-app.

**OAuth slices (deferred, per provider, shared schema):**
`oauth_connections(connection_id, user_id FK, provider, provider_account_id,
provider_email, refresh_token_ciphertext BYTEA, scopes, access_token_expires_at,
revoked_at, UNIQUE(user_id, provider))` — refresh tokens app-layer AESGCM-encrypted
via the `cryptography` package (already in api/uv.lock), key from a new
`SERMON_API_TOKEN_ENC_KEY` env var — and `editor_links(link_id, user_id FK, sermon/document FK,
provider, provider_file_id, provider_web_url, state linked|error|unlinked,
last_remote_version, exported_at, last_pulled_at; partial UNIQUE(document) WHERE
state='linked' — one external editor at a time, simultaneous Google+Word is a merge
problem by construction)`. OAuth state is the account-binding CSRF surface: HMAC-bind
state to user_id + nonce + ~10-min expiry + PKCE S256, validate at callback BEFORE
code exchange — else an attacker binds their account to a victim session and
exfiltrates pulled sermons. Thin httpx clients, not SDKs (ADR 0005/0006 precedent —
the surface is 2 token POSTs + ~5 REST calls per provider; verified no JS SDK needed,
no new client libs).

**Google first** — `drive.file` scope is non-sensitive: no CASA assessment, light
verification (verified). Export: Drive `files.create` upload-with-conversion to a
native Doc (verified; **hyperlink fidelity through that conversion is the one UNCLEAR
fact-check — run an empirical spike before committing**). Pull: `files.export
text/markdown` exists since ~mid-2024 with a 10 MB limit (verified) → through pandoc;
else export docx → pandoc. Operator lead time: create the GCP project + consent screen
early — Testing-mode refresh tokens expire in 7 days (fine for dev); production
publishing removes that for drive.file-only apps (verified) but has multi-week lead
time. **Microsoft second** — Graph docx in OneDrive (simple PUT under the size cap,
else uploadSession), eTag staleness, download-back → pandoc; MSA refresh tokens are
90-day sliding with rotation on each redemption (verified); Azure app registration is
a second operator task.

**Parked sub-item (written trigger):** background freshness — Celery beat service
(none exists today: no beat_schedule, no beat container) polling linked docs,
optionally Drive watch channels + Graph subscriptions with public validation routes
and beat-driven channel renewal. Trigger: real domain + the Let's Encrypt flip per
docs/DEPLOY_AWS.md "Adding a domain later". Webhook routes are UNAUTHENTICATED public
surface when they come: resolve the link by per-channel secret (Drive channel token /
Graph clientState), treat payloads as re-fetch hints, never as data.

**Tenant/secrets notes:** four new user-data tables, all JWT-scoped; refresh tokens
AESGCM-encrypted at rest, decrypted in-process per provider call, never returned to
the browser, never logged (Phase 27 logging must redact); provider file ids/URLs are
untrusted input; pulls write only to the link's own user_id-scoped document.

**Suggested slices:** (ext-A) docx round-trip core — convert.py seam + export/import
endpoints + `sermon_doc_revisions` migration + Download/Import UI; gate = golden
round-trip test proving structure + citation hyperlinks survive; (ext-B) OAuth
connection vault + /settings/integrations UI, live-verified against a Testing-mode
Google project; (ext-C) Google Docs link/pull/unlink + check-out lock; (ext-D)
Microsoft Graph as provider #2 over the same surface; (ext-E) PARKED background
freshness per the trigger above.

**Open questions:** lock severity while linked (hard read-only recommended) vs
warn-and-allow; unlink default (pull-final-copy vs keep-app-version — make the user
choose); account-rebind policy after revoke/reconnect (recommend auto-flip to
state='error'); per-user cap on active links (~25) now or with per-tenant quotas;
record pull provenance on revision rows (recommend yes — one column).

### Sequencing sketch (adopted 2026-06-10 as the v2 Plan ordered queue)

Historical record — the live queue is in the **v2 Plan — Sermon Workflow** section
above. B1's originals-persistence sub-item is the only time-sensitive piece — every ingest
before it lands loses the original forever; consider pulling it into v1 near Phase 20.
Then: B1 reader (independent) ∥ B2 A→D; B3 slices 1–3 are standalone once the
`documents` FK contract is settled (defer the column if needed); B3-4 linking and
B4 ext-A after B2; B4 ext-B/C/D as appetite allows; B4 ext-E stays parked on the
domain trigger. Dependencies on v1: Phase 18 posture (adopted early regardless),
Phase 19 autosave bucket, Phase 25 harness for editor/calendar E2E, Phase 28 backups
upgraded in importance by irreplaceable sermon data.
