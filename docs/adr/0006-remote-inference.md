# ADR 0006 — Remote inference transport: kill in-process models

- **Status:** Accepted
- **Date:** 2026-06-05
- **Deciders:** Cameron (sovITxyz)
- **Consulted:** DeepInfra model catalog + per-model API metadata
  (<https://api.deepinfra.com/models/list>,
  `https://api.deepinfra.com/models/<id>`); DeepInfra data-privacy docs
  (<https://docs.deepinfra.com/account/data-privacy>); ppq.ai API docs +
  live catalog (<https://ppq.ai/api-docs>, <https://api.ppq.ai/v1/models>);
  Google "OpenAI compatibility" Gemini docs
  (<https://ai.google.dev/gemini-api/docs/openai>); ADR 0003 (embedding
  model choice), ADR 0005 (LLM transport)
- **Informed:** Future contributors

## Context and Problem Statement

Through Phase 16, three inference models loaded **in-process**:
`BAAI/bge-large-en-v1.5` (query + chunk + chunking-boundary embeddings,
worker and api), `cross-encoder/ms-marco-MiniLM-L-6-v2` (rerank, api), and
`BAAI/bge-m3` (sentence-level highlight pruning, api). The LLM was already
remote (ADR 0005). The in-process trio cost, on the v0 single box:

- ~3.7 GB resident RAM in the api process (+ ~3 GB worker spikes per
  ingest) — the reason the box is a t3a.xlarge (16 GB, ~$110/mo compute).
- ~71–76 s of CPU model wall-time per warm `/search-summary` (Phase 14b/16
  live numbers) before the LLM is even called.
- ~40 min/book CPU ingest (semantic chunking + chunk embedding).
- ~1.5 GB of torch + sentence-transformers in both Python images, a 3.7 GB
  hf-cache volume, a `prewarm` one-shot, and `HF_HUB_OFFLINE` plumbing.

Phase 16b's goal: **no model weights load in-process anywhere** — every
inference leg becomes a remote API call — without invalidating any stored
vector or recalibrating any threshold.

## Decision Drivers

- **Vector compatibility is non-negotiable.** Every vector in Milvus lives
  in bge-large-en-v1.5's embedding space; the golden suite's `min_score`
  floors and the highlight 0.5 threshold are calibrated to bge-large /
  bge-m3 cosine scales. A provider serving the **exact same open weights**
  makes the swap zero-migration; anything else forces a corpus re-embed +
  recalibration project.
- **Privacy.** Users' private library text rides every request. The
  provider must default to zero retention of request content.
- **One transport pattern.** ADR 0005 established the shape: openai SDK
  against OpenAI-compatible endpoints, env-driven provider map, lazy cached
  clients, one error taxonomy mapped to 502/503. Reuse it.
- **Provider portability.** Gateway economics change fast (ADR 0005's
  1.5→2.5 retirement; this phase's own dead reranker pin — see below).
  base_url / model / key must be env, not code.
- **Behavioral-pin stability.** Every Phase 13 rerank/highlight unit pin
  (truncation, tiebreak, threshold inclusivity, metadata preservation) and
  the Phase 14/16 anti-confabulation contract must survive the seam swap
  unchanged.

## Considered Options

- **DeepInfra** — serves exact-weights BGE models over an OpenAI-compatible
  embeddings endpoint + open rerankers over a native endpoint;
  $0.01/1M-token class pricing; zero-retention default.
- **ppq.ai** — the operator-preferred vendor (already funds the LLM leg).
- **Self-hosted GPU inference** (the pre-16b architecture-locked "GPU swap"
  path).
- **Status quo** (CPU in-process models).

## Decision Outcome

**Chosen: DeepInfra for all three non-LLM legs, behind a shared env-driven
transport in `worker/inference.py`; the LLM stays on the ADR 0005
transport.** Verified against the live catalog/pricing pages on 2026-06-05:

| Leg | Model (pinned id) | Endpoint shape | Price | Env |
| --- | ----------------- | -------------- | ----- | --- |
| Embeddings (query + chunks + chunking boundaries) | `BAAI/bge-large-en-v1.5` — **exact v0 weights** | OpenAI-compatible: `https://api.deepinfra.com/v1/openai` (batch ≤1024 inputs) | $0.010/1M tokens | `SERMON_EMBEDDINGS_BASE_URL` / `SERMON_EMBEDDINGS_MODEL` |
| Rerank (top-30 → top-N) | `Qwen/Qwen3-Reranker-8B` | DeepInfra-native: `POST {base}/{model}` with `{"queries": […], "documents": […]}` → `{"scores": […]}` | $0.050/1M tokens (~$0.0005 per 30-doc query) | `SERMON_RERANK_BASE_URL` / `SERMON_RERANK_MODEL` |
| Highlight scoring | `BAAI/bge-m3` (dense) — **exact v0 weights** | same OpenAI-compatible embeddings endpoint, ONE batched call per query | $0.010/1M tokens (~$0.0001/query) | model id is a `highlight.py` constant |
| Summary LLM | unchanged (ADR 0005) | chat.completions | — | + `SERMON_API_LLM_REASONING_EFFORT` knob (see below) |

Key: unprefixed `DEEPINFRA_API_KEY` (the literal name DeepInfra's docs use;
same `validation_alias` pattern + rationale as `GOOGLE_API_KEY` /
`PPQ_API_KEY`).

### The dead pin, and what replaced it

The 2026-06-05 planning pass locked `BAAI/bge-reranker-v2-m3` as the
reranker. The pre-pin re-verify found **DeepInfra no longer serves it** —
the model id 404s and is absent from the catalog. The reranker is the one
stateless leg (no stored vectors, no calibrated threshold; the goldens pin
dense/sparse arm scores, never rerank scores), so the replacement is a
quality decision, not a migration: DeepInfra's current rerankers are
`Qwen/Qwen3-Reranker-{0.6B,4B,8B}` ($0.010/$0.025/$0.050 per 1M) and an
nvidia vision-language reranker. **The operator chose the 8B for maximum
accuracy** (quality explicitly prioritized over the marginal latency/price
difference; any Qwen3 tier is already a large jump over the 2021 MiniLM
cross-encoder it replaces). Dropping to 4B/0.6B is an env flip
(`SERMON_RERANK_MODEL`), not code.

### Embedding-space guard

Same-weights-elsewhere only stays true while nobody flips
`SERMON_EMBEDDINGS_MODEL` casually. Migration 0003 adds a Postgres
`meta(key, value)` table seeded with
`('embedding_model_id', 'BAAI/bge-large-en-v1.5')`; `worker/embedding.py`
compares the env model against that row before the first embed of a process
and **refuses to run on a mismatch**. Silent provider/model drift would mix
embedding spaces and quietly destroy retrieval — the guard makes it loud.
Changing embedders is a deliberate migration (re-embed, recalibrate, update
the row), never an env flip.

### Weight-parity proof

`worker/tests/golden/local_model_refvecs.npz` holds vectors produced by the
in-process sentence-transformers loaders immediately before their removal;
`test_embedding.py` pins (live, keyed) that DeepInfra's vectors match within
float tolerance (cosine ≥ 0.999) for both bge-large and bge-m3. If that
test fails, the provider drifted off the exact weights and every stored
vector + threshold is suspect — the test must not be loosened.

### Input truncation (the 512-token window)

bge-large has a hard **512-token** context (it has always had this; it is a
property of the model, not of DeepInfra — 512 is standard for BERT-family
embedders, which is *why* the pipeline chunks books into passages first). The
in-process `sentence-transformers` path **silently truncated** longer inputs
to `max_seq_length=512`; DeepInfra's endpoint instead **rejects** them with a
400 (`truncate` params on both its OpenAI-compatible and native endpoints were
probed — neither truncates, 2026-06-08). The live golden ingest surfaced this
on a real over-long semantic chunk.

To preserve behaviour, `worker/inference.py` replicates the silent truncation
client-side with the model's **own WordPiece tokenizer** — a ~700 KB
text-splitting ruleset bundled at `worker/assets/bge-large-en-v1.5-tokenizer.json`
plus the pure-Rust `tokenizers` library. **This is not a model**: tokenizing is
microseconds and a few MB of RAM, no weights, no GPU, no torch — it does not
reintroduce in-process inference (the resource cost the whole ADR exists to
remove was the 1.3 GB neural net, never the tokenizer in front of it). Inputs
are trimmed to **510 content tokens** (leaving room for DeepInfra's
`[CLS]+[SEP]` → ≤512), which matches the old model's window exactly, so an
over-long chunk's vector is byte-identical to the in-process result. Verified
live: 510-token inputs are accepted (DeepInfra counts repetitive text more
leniently than the raw tokenizer, so the bound is strictly safe), with a
defensive harder-trim retry for any pathological input. Only the bge-large
embeddings leg truncates; bge-m3 highlight sentences sit far under its 8192
window. The chunker reaches the same truncation by routing its boundary
embeddings through a thin `BaseEmbedding` adapter over `embed_texts` (which
also dropped the `llama-index-embeddings-openai-like` dependency).

### Privacy posture

DeepInfra's documented default for standard inference: request content
exists only in memory during processing; output is returned then deleted;
request content is generally not logged (metadata like request ids is).
That matches the "users' private libraries" bar. The transport additionally
guarantees no `user_id`/JWT/email ever leaves the process — requests carry
only already-tenant-filtered query/chunk text, key in the `Authorization`
header only. ppq.ai's LLM leg is unchanged from ADR 0005.

### The ppq gap & env portability

ppq.ai (live-probed 2026-06-05) now exposes `/v1/embeddings` — but serving
**only OpenAI `text-embedding-3-*` models**, not BGE; and it has no rerank
endpoint. So ppq cannot carry the exact-weights requirement today. The
embeddings transport is deliberately OpenAI-compatible + fully env-driven
so the day ppq (or anyone) serves BGE embeddings, the move is
`SERMON_EMBEDDINGS_BASE_URL` + key, no code. The rerank leg is
provider-shaped (DeepInfra-native JSON), so a rerank vendor move is a small
code change by design — no OpenAI-compatible rerank shape exists to target.

### Latency bonus: `reasoning_effort`

Google's OpenAI-compat layer accepts `reasoning_effort` —
`"none"|"minimal"|"low"|"medium"|"high"` — on chat.completions, and
`"none"` disables Gemini 2.5 Flash's default thinking (the ~60 s LLM leg
observed in Phase 14b/16). ppq documents reasoning control only on
`/v1/responses`, with `reasoning_effort` in `supported_parameters` for just
a few niche chat models. Phase 16b therefore ships the knob **opt-in and
provider-agnostic**: `SERMON_API_LLM_REASONING_EFFORT` rides
`extra_body` verbatim when set, is omitted entirely when unset, and whether
a gateway forwards it is probed live per deployment, not assumed.

### Consequences

- `torch` + `sentence-transformers` (+ `llama-index-embeddings-huggingface`
  and the CPU-wheel `[tool.uv.sources]` override) leave both pyprojects;
  `openai`/`httpx`/`numpy` (+ `llama-index-embeddings-openai-like`) enter
  worker/, `httpx`/`psycopg` enter api/. Both images shrink ~1.5 GB; the
  hf-cache volume, `prewarm` one-shot, `infra/scripts/prewarm_models.py`,
  and the `HF_HUB_OFFLINE` plumbing are deleted.
- `embed()` / `rerank()` / `highlight()` keep their public shapes,
  thresholds, and metadata keys; unit suites re-seam from model-loader
  mocks to transport mocks with every behavioral pin preserved.
- New failure taxonomy mapped once in `api/main.py`: unset key → 503
  naming `DEEPINFRA_API_KEY`; upstream failure after the single retry →
  502 naming the provider + leg (the ADR 0005/14b pattern).
- Rerank `score` semantics change scale (MiniLM logits ≈ [-15, +15] →
  Qwen3 relevance ≈ [0, 1]). Only ordering is load-bearing downstream
  (nothing thresholds the rerank score); documented in `api/search.py`.
- The api venv gains a sync Postgres driver (`psycopg`) because the
  embedding-space guard reads the meta row from `worker/embedding.py`'s
  sync path (executed via `asyncio.to_thread`).
- The box downsizes t3a.xlarge → t3a.large (~$55/mo compute, us-east-1
  verified 2026-06-05) once the RAM claim is verified live.
- New steady-state spend: ~$0.006/book ingest, ~$0.0006/search-summary of
  DeepInfra on top of the ADR 0005 LLM spend. Set a provider spend cap.

## Pros and Cons of the Options

### DeepInfra (chosen)

- ✅ Exact open weights for both embedders ⇒ zero vector migration, zero
  recalibration — the property no closed-model vendor can offer.
- ✅ OpenAI-compatible embeddings endpoint ⇒ reuses the ADR 0005 SDK,
  patterns, and error taxonomy.
- ✅ Zero-retention default; $0.01/1M-class pricing makes per-query cost
  noise next to the LLM leg.
- ❌ The reranker endpoint is provider-native, not OpenAI-shaped — a thin
  `httpx` client and a vendor-coupled wire shape (accepted; no standard
  exists).
- ❌ A second vendor + key joins the stack (ppq cannot serve BGE).
- ❌ Catalog churn is real — this phase's own reranker pin died between
  planning and implementation. Mitigated by env-driven ids + the
  space guard + the parity test.

### ppq.ai for everything

- ✅ One vendor, one key, operator-preferred billing.
- ❌ No BGE embeddings (OpenAI embedders only — wrong space, full re-embed
  + golden recalibration) and no rerank endpoint at all. Rejected today;
  revisit is an env flip for embeddings if their catalog grows.

### Self-hosted GPU

- ✅ No third party sees library text; latency potentially best-in-class.
- ❌ Cheapest always-on GPU instance (~$380+/mo g4dn.xlarge) inverts the
  economics of a ~$55/mo platform, and v0 has no ops bandwidth for CUDA
  images, driver pins, and capacity management. Rejected for v0; the k8s
  phase may revisit.

### Status quo (CPU in-process)

- ❌ The measured costs this ADR opens with — RAM forcing a 2×-priced box,
  ~75 s/query CPU, ~40 min/book ingest — for zero quality benefit over
  identical remote weights. Rejected.

## More Information

- docs/PHASES.md §Phase 16b — build + verify plan executing this decision;
  the row records measured latency/RAM/cost deltas.
- ADR 0003 — why bge-large-en-v1.5 is the locked embedding space.
- ADR 0005 — the transport pattern this ADR generalizes.
- Revisit if (a) DeepInfra drops/depreciates either BGE model (the parity
  test catches weight drift; the catalog re-check belongs in any
  provider-touching phase), (b) ppq ships BGE embeddings or a rerank
  endpoint (env flip / small client respectively), or (c) traffic justifies
  the self-hosted GPU math.
