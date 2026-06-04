# ADR 0005 — LLM transport: openai SDK over OpenAI-compatible endpoints

- **Status:** Accepted
- **Date:** 2026-06-04
- **Deciders:** Cameron (sovITxyz)
- **Consulted:** ppq.ai API docs (<https://ppq.ai/api-docs>); ppq.ai public model catalog (<https://api.ppq.ai/v1/models>); Google "OpenAI compatibility" Gemini docs (<https://ai.google.dev/gemini-api/docs/openai>); GitHub issue #24
- **Informed:** Future contributors

## Context and Problem Statement

Phase 14 shipped `POST /search-summary` on the `google-genai` SDK pinned to
`gemini-1.5-flash` (ARCHITECTURE.md §2 "LLM" row, §5 lifecycle final step).
Its live verify was deferred — no `GOOGLE_API_KEY` existed in the dev env —
so the LLM round-trip has never run end-to-end ([issue #24]). Phase 16 puts
UI on top of that endpoint, so the deferred verify must close first.

Two facts force the LLM layer open again before that verify can run:

1. **Gemini 1.5 Flash is retired industry-wide.** As of 2026-06 there are no
   `gemini-1.5-*` models on Google's endpoints or any gateway catalog. The
   Phase 14 `GEMINI_MODEL = "gemini-1.5-flash"` constant is dead regardless
   of transport — the first live call would 404.
2. **The operator funds the verify through ppq.ai (PayPerQ)**, a
   pay-as-you-go gateway (~2¢/query, 10¢ minimum top-up, no Google account
   needed). ppq.ai speaks the **OpenAI chat-completions shape**
   (`https://api.ppq.ai/v1`, `Authorization: Bearer <PPQ_API_KEY>`); the
   `google-genai` SDK cannot speak to it.

So Phase 14b must pick: what SDK/wire-shape does `api/summary.py` use to
reach a Gemini-class model, given that the verify runs against ppq.ai and
production may run against Google directly?

## Decision Drivers

- **Verify-what-you-ship.** The live verify is the point of Phase 14b
  (issue #24). A transport arm that the verify never exercises stays
  forever-unverified — exactly the gap this phase exists to close.
- **One error path.** Phase 14's 502/503 mapping (`api/AGENTS.md` fail-loud
  posture) should not fork into per-SDK exception taxonomies.
- **No hand-rolled HTTP.** Retries, connection pooling, typed responses,
  and streaming (a likely Phase 16+ want) are SDK work, not application
  work.
- **Provider portability.** Gateway economics change fast (the 1.5 → 2.5
  retirement is the proof). Swapping providers should be config, not code.
- **Citation contract stability.** The grounding prompt, `[book:chunk]`
  marker scheme, and no-context short-circuit are pinned by 22 Phase 14
  unit tests and must survive the transport swap unchanged.

## Considered Options

- **openai SDK as a single config-driven OpenAI-compatible transport** —
  one client, `base_url` + model + key resolved per provider from settings.
- **Two-SDK provider switch** — keep `google-genai` for Google-direct, add
  `openai` for ppq.ai, branch in `_generate_summary`.
- **Raw `httpx` POST** — hand-roll the chat-completions call against either
  endpoint.

## Decision Outcome

**Chosen: the openai SDK as a single config-driven OpenAI-compatible
chat-completions transport.** `google-genai` is dropped from `api/`
dependencies.

Google publishes an OpenAI-compatible endpoint —
`https://generativelanguage.googleapis.com/v1beta/openai/` — that accepts
the same `GOOGLE_API_KEY` with bare model ids (`gemini-2.5-flash`), so
Google-direct remains fully available *through the exact code path the
ppq.ai live-verify exercises*. One transport serves both providers:

| Provider (`SERMON_API_LLM_PROVIDER`) | base_url                                                  | Default model            | Key env var      |
| ------------------------------------ | --------------------------------------------------------- | ------------------------ | ---------------- |
| `google` (default)                   | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.5-flash`       | `GOOGLE_API_KEY` |
| `ppq`                                | `https://api.ppq.ai/v1`                                   | `google/gemini-2.5-flash` | `PPQ_API_KEY`    |

The map is a single `_PROVIDERS` dict in `api/summary.py` (one source of
truth); `SERMON_API_LLM_MODEL` overrides the per-provider default model id.
`gemini-2.5-flash` is pinned deliberately — drifting aliases on the ppq.ai
catalog (e.g. `gemini-flash-latest`) are not used, because a silent model
swap underneath a pinned citation contract is exactly the failure mode the
live verify exists to catch.

### Rationale

- **The verify covers production.** Because both providers sit behind the
  identical `openai.OpenAI(base_url=…)` + `chat.completions.create(…)`
  call, the ppq.ai live verify exercises the same request/response/error
  handling Google-direct will use. The only unverified delta per provider
  is configuration (URL, model id, key) — not code.
- **One exception taxonomy.** `openai.APIError` → 502 is the entire
  upstream-failure mapping, mirroring Phase 14's `errors.APIError` → 502.
  No second SDK, no second set of error classes, timeouts, or retry knobs.
- **The model constant had to change anyway.** The 1.5 retirement forces a
  touch on the LLM layer regardless; folding the transport swap into the
  same phase costs one re-seam of the existing unit tests rather than two
  separate churns.
- **Boring, typed, maintained.** The openai SDK is the de-facto wire
  standard every gateway (ppq.ai included) targets; it ships `py.typed`,
  built-in retries, and connection pooling — `httpx` would re-implement
  all three by hand.

### Consequences

- `api/pyproject.toml`: `google-genai` out, `openai` (current major) in.
  Still a pure network call — no in-process model, no
  pin-lockstep-with-worker concern (`api/AGENTS.md` model-surface note).
- `api/settings.py` grows `llm_provider` (`google`|`ppq`, default
  `google`), `ppq_api_key` (unprefixed `PPQ_API_KEY`, same
  `validation_alias` pattern + rationale as `GOOGLE_API_KEY`), and
  `llm_model` (optional override; `None` → provider default).
- The `/search-summary` 503-before-retrieval guard keys on the **active**
  provider's key and names the missing env var in its detail.
- The 22 Phase 14 unit tests re-seam from `models.generate_content` to
  `chat.completions.create`; every pinned behavior (citation contract,
  hallucination guard, fail-loud mapping) is preserved, and
  provider-resolution tests are added.
- **The `google` arm is config-verified only** until a `GOOGLE_API_KEY`
  exists in the dev env — the transport code is identical either way, but
  the Phase 14b live verify runs over `ppq` (recorded as a Phase 14b
  deviation in docs/PHASES.md).
- Model ids differ per provider (bare `gemini-2.5-flash` vs prefixed
  `google/gemini-2.5-flash`) — the `_PROVIDERS` map owns that mapping; an
  `SERMON_API_LLM_MODEL` override must use the active provider's spelling.
- ARCHITECTURE.md §2 "LLM" row becomes "Gemini Flash (2.5 at v0) over an
  OpenAI-compatible transport (ADR 0005)".

## Pros and Cons of the Options

### openai SDK, single config-driven transport

- ✅ One code path; the ppq.ai live verify covers the Google-direct arm's
  code (config differs, code doesn't).
- ✅ One error taxonomy (`openai.APIError`) → the existing 502 mapping.
- ✅ De-facto wire standard: every future gateway candidate (OpenRouter
  et al.) is a `base_url` + model-id change, not a code change.
- ✅ `py.typed`, retries, pooling, streaming for free.
- ❌ Gemini-specific request features outside the OpenAI surface (e.g.
  native `system_instruction` semantics, safety-setting knobs) are only
  reachable as far as Google's compat shim exposes them. The summary
  agent uses none of them — system message + temperature + max_tokens all
  map 1:1.
- ❌ Google's compat endpoint is labelled `v1beta`. Mitigated: Google
  documents it as the supported OpenAI-compatibility surface, and the
  default provider can be flipped to `ppq` by env var if it ever breaks.

### Two-SDK provider switch (google-genai + openai)

- ✅ Google-direct keeps its first-party SDK and full feature surface.
- ❌ Two error paths, two retry/timeout configs, two mocking seams in the
  unit tests — double the glue for zero behavioral difference at our
  feature usage (system + user message, temperature, max_tokens).
- ❌ The `google-genai` arm would stay forever-unverified: the operator
  funds ppq.ai, not Google, so the live verify never touches that SDK —
  re-creating the exact "shipped but never ran" gap this phase closes.
- Rejected on both counts.

### Raw httpx

- ✅ Zero new dependency (httpx is already a dev dep via the test stack).
- ❌ Hand-rolled retries, connection pooling, response typing, and error
  taxonomy — all solved problems in the SDK, all bug surface here.
- ❌ No streaming story for Phase 16+ without writing SSE parsing.
- Rejected: work for no gain.

## More Information

- [Issue #24] — the deferred Phase 14 live verify this ADR unblocks.
- docs/PHASES.md §Phase 14b — build + verify plan executing this decision.
- ppq.ai catalog sanity check (no key needed):
  `curl -s https://api.ppq.ai/v1/models` — confirm `google/gemini-2.5-flash`
  before bumping the pinned id.
- Revisit if (a) Google's OpenAI-compat endpoint graduates/breaks `v1beta`
  in a way the shim doesn't absorb, (b) the summary agent needs
  Gemini-native features the compat surface can't reach (thinking budgets,
  safety-setting overrides), or (c) a provider with better economics
  appears — (c) is a `_PROVIDERS` entry, not an ADR amendment.

[Issue #24]: https://github.com/sovITxyz/sermon.guide/issues/24
