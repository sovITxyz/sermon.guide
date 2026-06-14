"""Remote inference transport — OpenAI-compatible embeddings + thin rerank client.

Phase 16b (ADR 0006). NO model weights load in-process anywhere in this
codebase: every inference leg — chunk/query embeddings (BGE-Large),
rerank (Qwen3-Reranker), highlight scoring (BGE-M3 dense) — is a remote
API call through this module. The summary LLM has its own transport in
``api/summary.py`` (ADR 0005); this module mirrors that design for the
non-LLM legs.

## Provider

DeepInfra serves the exact-weights ``BAAI/bge-large-en-v1.5`` (and
``BAAI/bge-m3``) over an OpenAI-compatible embeddings endpoint, which is
what makes Phase 16b a zero-migration swap: identical weights mean every
existing Milvus vector stays valid and every calibrated threshold
(golden ``min_score`` floors, the highlight 0.5 cutoff) keeps its
meaning. The reranker rides DeepInfra's *native* inference endpoint —
``POST {rerank_base_url}/{model}`` with ``{"queries": [...],
"documents": [...]}`` → ``{"scores": [...]}`` — because no
OpenAI-compatible rerank shape exists.

Everything is env-driven (``SERMON_EMBEDDINGS_BASE_URL`` /
``SERMON_EMBEDDINGS_MODEL`` / ``SERMON_RERANK_BASE_URL`` /
``SERMON_RERANK_MODEL`` / unprefixed ``DEEPINFRA_API_KEY``, following the
``GOOGLE_API_KEY``/``PPQ_API_KEY`` alias precedent) so a future provider
move — e.g. ppq.ai shipping a ``/v1/embeddings`` that serves BGE models;
as of 2026-06-05 it serves only OpenAI embedders — is an env flip, not a
code change (ADR 0006).

## Failure modes

- Key unset → ``MissingInferenceKeyError`` naming the env var. The api
  maps it to a 503 (service unconfigured), mirroring the ADR 0005
  503-before-retrieval guard.
- Upstream failure after the built-in retry → ``RemoteInferenceError``
  naming the provider + leg. The api maps it to a 502 (the Phase 14b
  pattern); the Celery worker lets it propagate and fail the task loudly.
- Both clients run with explicit timeouts and exactly one retry.
  Embeddings retry via the openai SDK (``max_retries=1``); rerank
  retries once by hand, but never on a 4xx — a bad request or bad key
  will not heal on retry.

## Batching & ordering

DeepInfra caps embeddings requests at 1024 inputs; we batch well below
that (``_MAX_BATCH_ITEMS``) to keep request payloads modest on
book-sized ingests, and we re-assemble strictly in input order (the
OpenAI shape tags each row with its input ``index``; we sort by it
rather than trust response ordering). Rerank scores come back aligned
with ``documents`` and are length-checked before use.

## Tenant surface

None. Requests carry only the query/chunk text the caller was already
authorized to see (the tenant filter runs upstream in retrieval — see
``api/AGENTS.md``); no ``user_id``, JWT, or email ever leaves the
process, and the key flows env → ``Authorization`` header only.
DeepInfra's default policy is zero retention of request content
(ADR 0006 privacy notes).
"""

from __future__ import annotations

import time
from functools import lru_cache
from pathlib import Path
from typing import cast

import httpx
import numpy as np
import openai
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from tokenizers import Tokenizer

# One knob per leg, all overridable from the environment. Defaults are the
# ADR 0006 pins; ids are pinned, not alias-tracking, for the same reason
# ADR 0005 pins the LLM id — a silent model swap under calibrated
# thresholds is the failure mode the guard below exists to catch.
_DEFAULT_EMBEDDINGS_BASE_URL = "https://api.deepinfra.com/v1/openai"
_DEFAULT_EMBEDDINGS_MODEL = "BAAI/bge-large-en-v1.5"
_DEFAULT_RERANK_BASE_URL = "https://api.deepinfra.com/v1/inference"
_DEFAULT_RERANK_MODEL = "Qwen/Qwen3-Reranker-8B"

# DeepInfra accepts up to 1024 inputs per embeddings request; we stay well
# below so a book-sized ingest (chunks are ~1-4KB each) never builds a
# multi-megabyte request body.
_MAX_BATCH_ITEMS = 256

# --- BGE-Large 512-token window (ADR 0006 §truncation) -----------------------
# DeepInfra's bge-large endpoint REJECTS inputs over its 512-token context with
# a 400 (the in-process sentence-transformers model silently truncated them to
# max_seq_length=512). We replicate that truncation client-side with the model's
# OWN WordPiece tokenizer — a ~700KB text-splitting ruleset, NOT a neural model
# (microseconds, a few MB RAM; no torch, no GPU), bundled under worker/assets so
# nothing downloads at runtime. Truncating to 510 CONTENT tokens leaves room for
# DeepInfra's [CLS]+[SEP] (→ ≤512) and matches the old model's window exactly,
# so an over-long chunk's vector is byte-identical to what the in-process path
# produced. Verified live: my-510-token inputs are accepted (DeepInfra counts
# repetitive text more leniently than the raw tokenizer, so this is strictly
# safe). The _RETRY_TOKEN_LIMITS ladder is defensive belt-and-suspenders for any
# pathological input where DeepInfra's count might still exceed the window.
_BGE_LARGE_MODEL = _DEFAULT_EMBEDDINGS_MODEL
_TOKENIZER_PATH = Path(__file__).parent / "assets" / "bge-large-en-v1.5-tokenizer.json"
_MAX_CONTENT_TOKENS = 510
_RETRY_TOKEN_LIMITS = (510, 480, 440)

# Per-attempt read timeout. DeepInfra's bge-large endpoint was measured at
# 4–35 s for a 128-input batch during a degraded window (serverless cold
# starts / scaling), so this must comfortably clear the slow tail.
_TIMEOUT_SECONDS = 60.0

# Retries. The Phase 16b spec said "one retry"; live verification showed
# DeepInfra embeddings intermittently time out / ConnectError under the
# sustained call volume of a book-sized ingest, and one retry was not enough
# to ride a degraded window without failing the whole task. The openai SDK
# retries connection errors / 408 / 409 / 429 / 5xx with exponential backoff +
# jitter, so we lean on it (deviation from the spec, recorded in the phase
# row). The rerank httpx client backs off by hand to the same budget.
_MAX_RETRIES = 5
_RERANK_BACKOFF_BASE_SECONDS = 0.5


class InferenceSettings(BaseSettings):
    """Remote-inference env knobs (``SERMON_*`` + unprefixed key alias)."""

    model_config = SettingsConfigDict(env_prefix="SERMON_", extra="ignore")

    embeddings_base_url: str = _DEFAULT_EMBEDDINGS_BASE_URL
    embeddings_model: str = _DEFAULT_EMBEDDINGS_MODEL
    rerank_base_url: str = _DEFAULT_RERANK_BASE_URL
    rerank_model: str = _DEFAULT_RERANK_MODEL

    # Unprefixed via explicit alias — DEEPINFRA_API_KEY is the literal name
    # DeepInfra's docs use, same pattern + rationale as GOOGLE_API_KEY /
    # PPQ_API_KEY in api/settings.py. ``None`` until configured; callers
    # raise MissingInferenceKeyError naming the var rather than letting an
    # unconfigured key surface as an opaque SDK error.
    deepinfra_api_key: str | None = Field(default=None, validation_alias="DEEPINFRA_API_KEY")


settings = InferenceSettings()


class RemoteInferenceError(RuntimeError):
    """A remote inference call failed after its retry.

    The message names the provider and the leg (embeddings / rerank) but
    never carries key material or request content. ``api/main.py`` maps
    this to a 502; the Celery worker lets it fail the task loudly.
    """


class MissingInferenceKeyError(RemoteInferenceError):
    """The inference API key env var is unset.

    Subclass of ``RemoteInferenceError`` so worker callers can catch one
    type; ``api/main.py`` registers the more specific 503 mapping first.
    """


def _require_key() -> str:
    key = settings.deepinfra_api_key
    if not key:
        msg = "Remote inference is not configured; set DEEPINFRA_API_KEY."
        raise MissingInferenceKeyError(msg)
    return key


@lru_cache(maxsize=1)
def _embeddings_client() -> openai.OpenAI:
    """Construct the embeddings client once per process.

    Lazy + cached so import / lint / tests never need a key or network —
    mirrors ``api/summary.py:_client``. ``lru_cache`` does not cache a
    raised ``MissingInferenceKeyError``, so setting the key later still
    works.
    """
    return openai.OpenAI(
        base_url=settings.embeddings_base_url,
        api_key=_require_key(),
        timeout=_TIMEOUT_SECONDS,
        max_retries=_MAX_RETRIES,
    )


@lru_cache(maxsize=1)
def _bge_tokenizer() -> Tokenizer:
    """Load the bundled BGE WordPiece tokenizer once per process.

    Pure text-splitting — no weights, no inference. Lazy + cached so import
    / lint / tests never read the file until the first embed.
    """
    return Tokenizer.from_file(str(_TOKENIZER_PATH))


def token_count(text: str) -> int:
    """Count *text*'s BGE content tokens (special tokens excluded), keyless.

    Pure local tokenization through the bundled WordPiece ruleset — no
    weights, no network, no key. Callers that size text against the
    embedder's window (e.g. ``chunking`` sub-splitting an oversized chunk so
    the stored text matches what the embedder actually encodes) reuse this so
    the count can never drift from ``_truncate_to_tokens``' own tokenizer.
    """
    return len(_bge_tokenizer().encode(text, add_special_tokens=False).ids)


def truncation_token_limit() -> int:
    """The BGE content-token window inputs are truncated to before embedding.

    Exposes ``_MAX_CONTENT_TOKENS`` (ADR 0006 §truncation) as a public read so
    callers can pre-size text to fit the embedder's window — keeping the stored
    chunk text aligned with the tokens the embedder will actually encode —
    without importing the private constant.
    """
    return _MAX_CONTENT_TOKENS


def _truncate_to_tokens(text: str, limit: int) -> str:
    """Trim *text* to at most *limit* BGE content tokens (special tokens excluded).

    Returns *text* unchanged when it already fits. Decode→re-encode is
    drift-free on this WordPiece tokenizer (verified), so the result tokenizes
    back to ``<= limit`` content tokens.
    """
    ids = _bge_tokenizer().encode(text, add_special_tokens=False).ids
    if len(ids) <= limit:
        return text
    return _bge_tokenizer().decode(ids[:limit])


def _is_token_limit_error(exc: openai.BadRequestError) -> bool:
    """True when a 400 is the >512-token rejection (vs any other bad request)."""
    text = str(getattr(exc, "message", "") or exc).lower()
    return "input tokens" in text or "context length" in text


def _embeddings_error(model: str, exc: openai.APIError) -> str:
    """A diagnosable failure message — names the leg, model, and the cause.

    The generic 'call failed' message masked whether a failed ingest was a
    timeout, a connection drop, or a 5xx (the Phase 16b live verify hit a
    DeepInfra degradation and the operator could not tell from the message).
    Includes the exception type and any HTTP status; never the key or body.
    """
    status = getattr(exc, "status_code", None)
    detail = f"{type(exc).__name__}" + (f" (HTTP {status})" if status else "")
    return f"DeepInfra embeddings call failed for model {model}: {detail}."


def _embed_one_batch(
    client: openai.OpenAI,
    batch: list[str],
    model: str,
) -> list[list[float]]:
    """Embed a single batch, truncating bge-large inputs to the 512-token window.

    For the bge-large model, each input is pre-truncated to ``_MAX_CONTENT_TOKENS``
    (the faithful path that matches the old in-process behavior). The
    ``_RETRY_TOKEN_LIMITS`` ladder re-truncates harder and retries ONLY when
    DeepInfra still rejects on length — a defensive net for inputs it might
    count differently; the normal path never reaches the second rung. Other
    models (bge-m3 highlight, with short sentences well under its 8192 window)
    pass through untruncated.
    """
    limits: tuple[int, ...] = _RETRY_TOKEN_LIMITS if model == _BGE_LARGE_MODEL else (0,)
    last_exc: openai.BadRequestError | None = None
    for limit in limits:
        prepared = [_truncate_to_tokens(t, limit) for t in batch] if limit else batch
        try:
            response = client.embeddings.create(
                model=model,
                input=prepared,
                encoding_format="float",
            )
        except openai.BadRequestError as exc:
            if _is_token_limit_error(exc) and limit and limit != limits[-1]:
                last_exc = exc
                continue  # over the window even after truncation — trim harder
            raise RemoteInferenceError(_embeddings_error(model, exc)) from exc
        except openai.APIError as exc:
            # Connection / timeout / 429 / 5xx already exhausted the SDK's
            # ``max_retries`` exponential backoff before surfacing here.
            raise RemoteInferenceError(_embeddings_error(model, exc)) from exc
        if len(response.data) != len(prepared):
            msg = (
                f"DeepInfra embeddings returned {len(response.data)} rows "
                f"for a {len(prepared)}-input batch (model {model})."
            )
            raise RemoteInferenceError(msg)
        # The OpenAI shape tags each row with its input index; sort by it
        # instead of trusting response ordering.
        return [item.embedding for item in sorted(response.data, key=lambda d: d.index)]

    msg = f"DeepInfra embeddings rejected a batch as over-length for model {model}."
    raise RemoteInferenceError(msg) from last_exc


def embed_texts(texts: list[str], *, model: str) -> np.ndarray:
    """Embed *texts* remotely → ``(len(texts), dim)`` float32, L2-normalized.

    Batches at ``_MAX_BATCH_ITEMS`` per request and reassembles strictly
    in input order. bge-large inputs over the model's 512-token window are
    truncated client-side (ADR 0006 §truncation) — replicating the old
    in-process model's silent ≤512 truncation, since DeepInfra rejects
    over-length inputs instead. Output rows are L2-normalized client-side
    regardless of what the provider returns — normalizing an already-normalized
    vector is a no-op, and Milvus ``COSINE`` ≡ inner-product only holds on
    unit vectors (ARCHITECTURE.md §3).

    Empty input returns a ``(0, 0)`` array without touching the network;
    callers short-circuit before this point on their own empty paths.
    """
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)

    client = _embeddings_client()
    rows: list[list[float]] = []
    for start in range(0, len(texts), _MAX_BATCH_ITEMS):
        rows.extend(_embed_one_batch(client, texts[start : start + _MAX_BATCH_ITEMS], model))

    arr = np.asarray(rows, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    return arr / np.maximum(norms, np.float32(1e-12))


def rerank_scores(*, query: str, documents: list[str]) -> list[float]:
    """Score (query, document) relevance for each document, preserving order.

    Calls DeepInfra's native reranker endpoint (not OpenAI-shaped):
    ``POST {rerank_base_url}/{rerank_model}`` with equal-length
    ``queries``/``documents`` arrays → ``{"scores": [...]}`` aligned with
    ``documents``. Higher = more relevant. Retries transport errors / 5xx up
    to ``_MAX_RETRIES`` times with exponential backoff (matching the openai
    SDK's posture on the embeddings leg, since DeepInfra degrades the same
    way); a 4xx raises immediately (bad request or bad key will not heal on
    retry).
    """
    if not documents:
        return []
    key = _require_key()
    url = f"{settings.rerank_base_url.rstrip('/')}/{settings.rerank_model}"
    payload = {"queries": [query] * len(documents), "documents": documents}
    headers = {"Authorization": f"Bearer {key}"}

    last_error: Exception | None = None
    for attempt in range(1 + _MAX_RETRIES):
        if attempt:
            # Exponential backoff before a retry: 0.5s, 1s, 2s, 4s, ...
            time.sleep(_RERANK_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        try:
            response = httpx.post(url, json=payload, headers=headers, timeout=_TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:
            last_error = exc
            continue
        if response.status_code >= 500:  # noqa: PLR2004 — HTTP class boundary
            last_error = httpx.HTTPStatusError(
                f"server error {response.status_code}",
                request=response.request,
                response=response,
            )
            continue
        if response.status_code >= 400:  # noqa: PLR2004 — HTTP class boundary
            msg = (
                f"DeepInfra rerank call failed for model {settings.rerank_model} "
                f"(HTTP {response.status_code})."
            )
            raise RemoteInferenceError(msg)
        return _parse_rerank_scores(response, expected=len(documents))

    detail = type(last_error).__name__ if last_error else "unknown"
    msg = f"DeepInfra rerank call failed for model {settings.rerank_model}: {detail}."
    raise RemoteInferenceError(msg) from last_error


def _parse_rerank_scores(response: httpx.Response, *, expected: int) -> list[float]:
    """Validate the rerank response body shape and return its scores."""
    try:
        body: object = response.json()
    except ValueError as exc:
        msg = f"DeepInfra rerank returned a non-JSON body (model {settings.rerank_model})."
        raise RemoteInferenceError(msg) from exc
    malformed = (
        f"DeepInfra rerank returned a malformed scores array for model "
        f"{settings.rerank_model} (expected {expected} floats)."
    )
    raw = cast("dict[object, object]", body).get("scores") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        raise RemoteInferenceError(malformed)
    items = cast("list[object]", raw)
    if len(items) != expected:
        raise RemoteInferenceError(malformed)
    out: list[float] = []
    for item in items:
        # bool is an int subclass; a boolean "score" is malformed, not 0/1.
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise RemoteInferenceError(malformed)
        out.append(float(item))
    return out
