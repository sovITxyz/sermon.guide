"""Golden retrieval regression tests (Phase 11 + Phase 12 hybrid).

Each row in ``worker/tests/golden/queries.jsonl`` is one parametrized
test. For each row, the test:

1. Embeds ``row["query"]`` with BGE-Large.
2. Runs the Phase 12 hybrid pipeline — dense Milvus COSINE + sparse
   Postgres BM25, fused via RRF (k=60) — over the golden test user's
   library (the union of all corpus ``book_id``s — ranking quality, not
   tenant isolation, is what this test gates).
3. Asserts at least one ``row["expected_filenames"]`` book appears in
   top-10 with fused score ≥ ``row["min_score"]``.

Hit/miss binary — any one expected book in top-K passes. No fuzzy
partial credit. The point is to detect *catastrophic* regression when an
upstream component (embedding model, chunker, index, ranking, fusion)
changes; sub-rank shifts are signal but not gating.

``min_score`` semantics in Phase 12: interpreted as a **per-arm**
score floor against whichever arm contributed to a fused hit. A row
passes if any expected book lands in top-K of the fused list AND at
least one arm scored that hit ≥ ``min_score``. That preserves the
Phase 11 dense-strength rows' ``0.45`` COSINE floor (the dense arm
still surfaces them well above 0.45 on its own) while letting Phase 12
BM25-strength rows use ``min_score: 0.0`` — the sparse ``ts_rank_cd``
scale isn't worth pinning across corpus changes, and "any positive
arm contribution into top-K" is the right gate for the BM25 arm.

## Skip-clean policy

Each gate fires its own ``pytest.skip`` so a missing-sample run is
distinguishable from a missing-Milvus run in CI logs:

- ``queries.jsonl`` absent → parametrization yields zero tests; pytest
  reports the file with zero collected, the job is green-but-empty.
- ALL referenced samples missing from ``worker/tests/samples/`` → the
  whole session skips with the filenames listed (the CI posture —
  sample files are never committed).
- SOME samples missing (Phase 23) → only the rows whose every expected
  file is absent skip, per-row, each listing its absent filename(s);
  rows with at least one expected file on disk run against the present
  subset. Dev-corpus rows and seeded-corpus rows therefore activate
  independently — a box with only the dev samples runs the original
  rows and reports each seeded row as a visible corpus-shape skip.

Every missing-sample skip reason contains the substring
``corpus sample(s) missing`` — load-bearing API shared with the CI
live-gate guard (``.github/workflows/ci.yml``, retrieval-golden-live)
and ``scripts/test_live.sh``, which tolerate exactly that pattern and
fail on any other skip. Change all three in lockstep or none.
- Milvus or Postgres unreachable → skip with the host:port.
- ``DEEPINFRA_API_KEY`` unset → skip (Phase 16b: query + ingest
  embeddings are remote calls; the suite IS the live-DeepInfra gate).
- NLTK WordNet absent → skip (the dedup gate inside ingest needs it).
- Collection ``library_vectors`` missing → skip (``make bootstrap-milvus``).
- Any corpus book missing ``chunks`` rows → skip with a pointer to
  ``scripts.backfill_chunks`` (Phase 12 introduced the table; pre-existing
  books need a backfill before hybrid search returns sparse hits).

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

Phase 17 gives CI two flavors of this suite. The keyless
``retrieval-golden`` job runs it with no infra and no key — every row
skips, and a loud-skip guard turns that into a ``::warning`` so the
green is never silent. The keyed ``retrieval-golden-live`` job
(activates automatically once the ``DEEPINFRA_API_KEY`` repo secret
exists) boots the compose stack, migrates, bootstraps Milvus, and runs
this suite live — but the query rows still skip there as corpus-shape
skips (Phase 23 ships ``seeds/manifest.jsonl`` + the docs/SEED_CORPUS.md
download runbook instead of committing sample files), so the local
``make test-live`` against a downloaded corpus is the ranking-quality
enforcement point.
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

from db import Chunk, User, get_sync_session_factory
from db.settings import settings as db_settings
from embedding import EMBED_DIM, embed
from retrieval import hybrid_search_sync
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


def _remote_embeddings_available() -> bool:
    """Phase 16b: query + ingest embeddings are remote DeepInfra calls."""
    return bool(os.environ.get("DEEPINFRA_API_KEY"))


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

    First-time ingest embeds remotely (DeepInfra, ADR 0006 — seconds per
    book, ~$0.006/book); subsequent sessions are cheap because Phase 8
    dedup short-circuits the chunk → embed → insert path on identical
    content. Ephemeral CI infra never gets that discount — every CI run
    is a cold ingest. The fixture is session-scoped so every
    parametrized test reuses the same corpus.
    """
    filenames = _referenced_filenames()
    if not filenames:
        pytest.skip("worker/tests/golden/queries.jsonl is empty — nothing to verify.")

    paths = {fn: SAMPLES_DIR / fn for fn in filenames}
    missing = sorted(fn for fn, p in paths.items() if not p.exists())
    if len(missing) == len(filenames):
        # Nothing to ingest at all (the CI posture — sample files are never
        # committed). Skip the whole session BEFORE probing infra so the
        # reason names the corpus gap, not whatever else is also down.
        pytest.skip(
            f"Golden corpus sample(s) missing under {SAMPLES_DIR}: {missing}. "
            "Add the dev samples and/or download the seeded corpus per "
            "docs/SEED_CORPUS.md; CI runs without sample files.",
        )

    if not _milvus_reachable():
        host, port = _milvus_host_port()
        pytest.skip(f"Milvus unreachable at {host}:{port}; run `make up`.")
    if not _postgres_reachable():
        pytest.skip(
            f"Postgres unreachable at {db_settings.host}:{db_settings.port}; run `make up`.",
        )
    if not _remote_embeddings_available():
        pytest.skip("DEEPINFRA_API_KEY unset — remote embeddings unavailable.")
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

    # Ingest only what's on disk; rows whose every expected file is absent
    # skip per-row in the test body (corpus-shape skip), so a partially
    # downloaded corpus still runs every row it can.
    absent = set(missing)
    book_ids: dict[str, uuid.UUID] = {}
    for fn in filenames:
        if fn in absent:
            continue
        result = ingest(path=paths[fn], user_id=GOLDEN_USER_ID)
        book_ids[fn] = result.book_id

    yield book_ids


def _chunks_present_for(book_ids: list[uuid.UUID]) -> bool:
    """Return True iff every corpus book has at least one ``chunks`` row.

    Phase 12 hybrid search needs the BM25 arm, which reads from
    ``chunks``. A book that landed in Milvus before the table existed
    (or before backfill ran) would silently make the sparse arm a no-op
    for that book. Skip with a clear message rather than failing the
    rest of the suite.
    """
    sf = get_sync_session_factory()
    with sf() as session:
        from sqlalchemy import func, select

        stmt = (
            select(Chunk.book_id, func.count(Chunk.chunk_id))
            .where(Chunk.book_id.in_(book_ids))
            .group_by(Chunk.book_id)
        )
        present = {row[0] for row in session.execute(stmt).all() if row[1] > 0}
    return present.issuperset(set(book_ids))


class TestRetrievalAccuracy:
    """Retrieval must surface the right book in top-K for each curated query.

    A failure here means an upstream change (model, chunking, indexing,
    ranking, fusion) regressed retrieval quality on the same corpus.
    Inspect the failure's printed top-K + scores against the prior
    commit's run to decide whether the change is acceptable; do not
    silence the test without understanding what shifted.
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
        1. Did fusion break? Re-run ``test_search_unit.py::test_rrf_fuse_*``
           in api/ — those pin the math without infra.
        2. Is the per-arm score floor still calibrated? If model
           upgrades shifted the COSINE scale, recalibrate ``min_score``
           on the dense-strength rows. If a Postgres text-search config
           change shifted ts_rank_cd, recalibrate the BM25-strength rows.
        3. Did chunk boundaries change (Phase 5)? Section-spanning queries
           degrade when chunks shrink past the relevant span.
        4. Did the partition key or filter pushdown regress (Phase 3
           covers this)? Re-run ``make test-isolation`` to triangulate.
        """
        from pymilvus import MilvusClient

        host, port = _milvus_host_port()
        client = MilvusClient(uri=f"http://{host}:{port}")

        expected_filenames: list[str] = row["expected_filenames"]
        absent = sorted(fn for fn in expected_filenames if fn not in golden_corpus)
        expected_book_ids = {golden_corpus[fn] for fn in expected_filenames if fn in golden_corpus}
        if not expected_book_ids:
            # Corpus-shape skip: this row's book(s) aren't on disk; the rest
            # of the suite still runs. The substring "corpus sample(s)
            # missing" is load-bearing — it's the ONLY skip reason the CI
            # live-gate guard and scripts/test_live.sh tolerate; any other
            # skip fails `make test-live`. Change all three in lockstep.
            pytest.skip(
                f"Golden corpus sample(s) missing under {SAMPLES_DIR}: {absent}. "
                "Row is corpus-shape gated — download the file(s) "
                "(docs/SEED_CORPUS.md for seeded books) to activate it.",
            )

        # Library = every corpus book (the golden user owns all of them).
        # We're testing ranking quality, not isolation — Phase 3 owns the
        # isolation gate. The filter is still load-bearing per CLAUDE.md;
        # an unfiltered search would mix in non-corpus vectors and produce
        # noisy scores.
        library = list(golden_corpus.values())

        if not _chunks_present_for(library):
            pytest.skip(
                "Some corpus books lack `chunks` rows — run "
                "`uv run python -m scripts.backfill_chunks` to populate, "
                "then re-run the goldens.",
            )

        query_vec = embed([row["query"]])[0].tolist()
        sf = get_sync_session_factory()
        with sf() as session:
            fused = hybrid_search_sync(
                client=client,
                session=session,
                query=row["query"],
                query_vec=query_vec,
                book_ids=library,
                limit=TOP_K,
            )

        floor = float(row["min_score"])
        passing = [
            hit
            for hit in fused
            if hit.book_id in expected_book_ids
            and (
                (hit.dense_score is not None and hit.dense_score >= floor)
                or (hit.sparse_score is not None and hit.sparse_score >= floor)
            )
        ]

        hits_repr = [(hit.book_id, hit.score, hit.dense_score, hit.sparse_score) for hit in fused]
        assert passing, (
            f"RETRIEVAL REGRESSION on golden query.\n"
            f"  query: {row['query']!r}\n"
            f"  expected (any of): {expected_filenames}\n"
            f"  absent from samples dir (not in play this run): {absent}\n"
            f"  expected book_ids: {sorted(expected_book_ids)}\n"
            f"  per-arm min_score floor: {floor}\n"
            f"  top-{len(fused)} (book_id, rrf, dense, sparse): {hits_repr}\n"
            f"  note: {row.get('note', '')}\n"
            f"Compare against the prior commit's golden run on the same corpus."
        )

    def test_embedder_query_dim_matches_index(self) -> None:
        """Sanity gate — query embeddings must be 1024-D or every Milvus
        search above is a type error masquerading as a retrieval miss.
        """
        if not _remote_embeddings_available():
            pytest.skip("DEEPINFRA_API_KEY unset — remote embeddings unavailable.")
        if not _postgres_reachable():
            pytest.skip(
                f"Postgres unreachable at {db_settings.host}:{db_settings.port}; "
                "the embedding-space guard needs the meta row.",
            )
        out = embed(["dimension check"])
        assert out.shape == (1, EMBED_DIM), out.shape
