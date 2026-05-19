"""Golden retrieval regression tests (Phase 11).

Each row in ``worker/tests/golden/queries.jsonl`` is one parametrized
test. For each row, the test:

1. Embeds ``row["query"]`` with BGE-Large.
2. Runs a filtered Milvus COSINE search over the golden test user's
   library (the union of all corpus ``book_id``s — ranking quality, not
   tenant isolation, is what this test gates).
3. Asserts at least one ``row["expected_filenames"]`` book appears in
   top-10 with similarity ≥ ``row["min_score"]``.

Hit/miss binary — any one expected book in top-K passes. No fuzzy
partial credit. The point is to detect *catastrophic* regression when an
upstream component (embedding model, chunker, index, ranking) changes;
sub-rank shifts are signal but not gating.

## Skip-clean policy

Each gate fires its own ``pytest.skip`` so a missing-sample run is
distinguishable from a missing-Milvus run in CI logs:

- ``queries.jsonl`` absent → parametrization yields zero tests; pytest
  reports the file with zero collected, the job is green-but-empty.
- Any referenced sample missing from ``worker/tests/samples/`` → skip
  with the missing filename(s) listed.
- Milvus or Postgres unreachable → skip with the host:port.
- BGE-Large not in the HF cache → skip (we never download 1.3 GB inside
  pytest).
- NLTK WordNet absent → skip (the dedup gate inside ingest needs it).
- Collection ``library_vectors`` missing → skip (``make bootstrap-milvus``).

## Idempotency

The fixture ingests under a deterministic ``user_id`` (uuid5 of a fixed
namespace + label). Phase 8 dedup short-circuits every re-ingest after
the first run, so subsequent sessions resolve the filename → book_id
map in seconds rather than the ~minutes the cold first ingest takes per
book. Cleanup is intentionally absent — the corpus stays in Milvus +
Postgres so the next run is fast; the entire footprint sits under one
user_id and a manual ``user_library``/``global_books`` delete clears it
when needed.

## Local enforcement vs CI

CI's ``retrieval-golden`` job is gated on ``queries.jsonl`` existing
(Phase 0 wired it). Without Milvus + corpus in CI, every row skips
cleanly — the local ``make test-retrieval-golden`` is the real
enforcement point until a future phase brings Milvus + a curated corpus
volume online in CI. This mirrors Phase 3's ``test-isolation`` deferral
note.
"""

# pymilvus 2.6 stubs are loose; same relaxations as the rest of worker/.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnnecessaryComparison=false

from __future__ import annotations

import json
import os
import socket
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from db import User, get_sync_session_factory
from db.settings import settings as db_settings
from embedding import EMBED_DIM, MODEL_NAME, embed
from scripts.bootstrap_milvus import COLLECTION_NAME

SAMPLES_DIR = Path(__file__).resolve().parent / "samples"
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
QUERIES_PATH = GOLDEN_DIR / "queries.jsonl"

# Fixed namespace + label so the golden user_id is stable across runs.
# UUIDv5 is deterministic, so re-runs hit the same user_library rows and
# the Phase 8 dedup gate makes the re-ingest a no-op.
_GOLDEN_NAMESPACE = uuid.UUID("9b3a4e9c-5d4f-4f17-9c2c-1a8c6b6b2d11")
GOLDEN_USER_ID = uuid.uuid5(_GOLDEN_NAMESPACE, "retrieval-golden-test-user")

TOP_K = 10


def _load_golden_rows() -> list[dict[str, Any]]:
    """Return every JSONL row in queries.jsonl; empty list if file absent."""
    if not QUERIES_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    with QUERIES_PATH.open() as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            rows.append(cast("dict[str, Any]", json.loads(line)))
    return rows


def _referenced_filenames() -> list[str]:
    """Union of every ``expected_filenames`` across all golden rows."""
    seen: dict[str, None] = {}  # preserve insertion order across rows
    for row in _load_golden_rows():
        for fn in row.get("expected_filenames", []):
            seen.setdefault(fn, None)
    return list(seen.keys())


def _milvus_host_port() -> tuple[str, int]:
    host = os.environ.get("SERMON_MILVUS_HOST", "localhost")
    port = int(os.environ.get("SERMON_MILVUS_PORT", "19530"))
    return host, port


def _milvus_reachable() -> bool:
    host, port = _milvus_host_port()
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _postgres_reachable() -> bool:
    try:
        with socket.create_connection((db_settings.host, db_settings.port), timeout=1.0):
            return True
    except OSError:
        return False


def _model_available() -> bool:
    if os.environ.get("HF_HUB_OFFLINE") == "1":
        return False
    cache = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    slug = MODEL_NAME.replace("/", "--")
    return (cache / "hub" / f"models--{slug}").is_dir()


def _wordnet_available() -> bool:
    try:
        import nltk

        nltk.data.find("corpora/wordnet")
    except (ImportError, LookupError):
        return False
    return True


def _ensure_golden_user() -> None:
    """Insert the deterministic golden user row if missing; idempotent."""
    sf = get_sync_session_factory()
    with sf() as session, session.begin():
        existing = session.get(User, GOLDEN_USER_ID)
        if existing is None:
            session.add(
                User(
                    user_id=GOLDEN_USER_ID,
                    email=f"{GOLDEN_USER_ID}@golden.test",
                    password_hash="bcrypt$retrieval-golden-test-user",  # noqa: S106 — test seed only
                ),
            )


@pytest.fixture(scope="session")
def golden_corpus() -> Iterator[dict[str, uuid.UUID]]:
    """Ingest every referenced sample once per session; return filename → book_id.

    First-time ingest is slow (BGE-Large CPU ~minutes per book); subsequent
    sessions are cheap because Phase 8 dedup short-circuits the chunk →
    embed → insert path on identical content. The fixture is session-scoped
    so every parametrized test reuses the same corpus.
    """
    filenames = _referenced_filenames()
    if not filenames:
        pytest.skip("worker/tests/golden/queries.jsonl is empty — nothing to verify.")

    paths = {fn: SAMPLES_DIR / fn for fn in filenames}
    missing = sorted(fn for fn, p in paths.items() if not p.exists())
    if missing:
        pytest.skip(
            f"Golden corpus sample(s) missing under {SAMPLES_DIR}: {missing}. "
            "Add the files locally to run; CI runs without copyrighted samples.",
        )

    if not _milvus_reachable():
        host, port = _milvus_host_port()
        pytest.skip(f"Milvus unreachable at {host}:{port}; run `make up`.")
    if not _postgres_reachable():
        pytest.skip(
            f"Postgres unreachable at {db_settings.host}:{db_settings.port}; run `make up`.",
        )
    if not _model_available():
        pytest.skip("BGE-Large not in HF cache — prewarm or set HF_HOME.")
    if not _wordnet_available():
        pytest.skip("NLTK WordNet corpus missing; run `nltk.download('wordnet')`.")

    from pymilvus import MilvusClient

    host, port = _milvus_host_port()
    client = MilvusClient(uri=f"http://{host}:{port}")
    if not client.has_collection(collection_name=COLLECTION_NAME):
        pytest.skip(f"Collection '{COLLECTION_NAME}' missing — run `make bootstrap-milvus`.")

    _ensure_golden_user()

    # Local import — ingest depends on Phase 8 dedup which requires NLTK
    # WordNet; if we imported at module level the worker test collection
    # would pay that cost even when this file is skipped.
    from ingest import ingest

    book_ids: dict[str, uuid.UUID] = {}
    for fn in filenames:
        result = ingest(path=paths[fn], user_id=GOLDEN_USER_ID)
        book_ids[fn] = result.book_id

    yield book_ids


def _filter_expr(book_ids: list[uuid.UUID]) -> str:
    """Mirror of ``api/search.py:_build_filter_expr`` — kept inline to avoid
    cross-package imports in worker tests (api/ depends on worker, not the
    other way)."""
    quoted = ", ".join(f'"{b!s}"' for b in book_ids)
    return f"book_id in [{quoted}]"


class TestRetrievalAccuracy:
    """Retrieval must surface the right book in top-K for each curated query.

    A failure here means an upstream change (model, chunking, indexing,
    ranking) regressed retrieval quality on the same corpus. Inspect the
    failure's printed top-K + scores against the prior commit's run to
    decide whether the change is acceptable; do not silence the test
    without understanding what shifted.
    """

    @pytest.mark.parametrize(
        "row",
        _load_golden_rows(),
        ids=lambda r: r["query"][:60],
    )
    def test_query_surfaces_expected_book(
        self,
        row: dict[str, Any],
        golden_corpus: dict[str, uuid.UUID],
    ) -> None:
        """If this fails, the query no longer retrieves any expected book at
        the required score floor.

        Diagnosis order:
        1. Is the score floor still calibrated? If model upgrades shifted the
           absolute scale, recalibrate ``min_score`` per row.
        2. Did chunk boundaries change (Phase 5)? Section-spanning queries
           degrade when chunks shrink past the relevant span.
        3. Did the partition key or filter pushdown regress (Phase 3
           covers this)? Re-run ``make test-isolation`` to triangulate.
        """
        from pymilvus import MilvusClient

        host, port = _milvus_host_port()
        client = MilvusClient(uri=f"http://{host}:{port}")

        expected_filenames: list[str] = row["expected_filenames"]
        expected_book_ids = {golden_corpus[fn] for fn in expected_filenames}

        # Library = every corpus book (the golden user owns all of them).
        # We're testing ranking quality, not isolation — Phase 3 owns the
        # isolation gate. The filter is still load-bearing per CLAUDE.md;
        # an unfiltered search would mix in non-corpus vectors and produce
        # noisy scores.
        library = list(golden_corpus.values())
        query_vec = embed([row["query"]])[0].tolist()
        results = client.search(
            collection_name=COLLECTION_NAME,
            data=[query_vec],
            filter=_filter_expr(library),
            limit=TOP_K,
            output_fields=["book_id"],
        )

        hits = [(uuid.UUID(hit["entity"]["book_id"]), float(hit["distance"])) for hit in results[0]]
        passing = [
            (bid, score)
            for bid, score in hits
            if bid in expected_book_ids and score >= row["min_score"]
        ]

        assert passing, (
            f"RETRIEVAL REGRESSION on golden query.\n"
            f"  query: {row['query']!r}\n"
            f"  expected (any of): {expected_filenames}\n"
            f"  expected book_ids: {sorted(expected_book_ids)}\n"
            f"  min_score: {row['min_score']}\n"
            f"  top-{len(hits)} (book_id, score): {hits}\n"
            f"  note: {row.get('note', '')}\n"
            f"Compare against the prior commit's golden run on the same corpus."
        )

    def test_embedder_query_dim_matches_index(self) -> None:
        """Sanity gate — query embeddings must be 1024-D or every Milvus
        search above is a type error masquerading as a retrieval miss.
        """
        if not _model_available():
            pytest.skip("BGE-Large not in HF cache — prewarm or set HF_HOME.")
        out = embed(["dimension check"])
        assert out.shape == (1, EMBED_DIM), out.shape
