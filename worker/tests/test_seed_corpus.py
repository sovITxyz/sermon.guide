"""Pure-unit tests for the Phase 23 seed-corpus script.

Covers the keyless, no-I/O-beyond-tmp_path seams of
``scripts.seed_corpus``: manifest parsing/validation (the rights record
is strict by design), the deterministic Phase 20 idempotency-token
derivation, the corpus-seed user identity, and the seed planner. The
enqueue/wait half — broker messages, ``upload_tasks`` upserts, Celery
results — is exercised by the operator seed run against the live stack,
matching the maintenance-scripts split (pure helpers unit-tested; store
mutations validated live; see ``docs/SEED_CORPUS.md``).

The committed manifest itself is also a fixture here: the policy test
pins it to docs/CORPUS_POLICY.md (public-domain only, auditable source
fields, the "grace" book present).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts.enqueue_ingest import LABEL_EMAIL_DOMAIN, LABEL_NAMESPACE
from scripts.seed_corpus import (
    DEFAULT_MANIFEST,
    SEED_TASK_NAMESPACE,
    SEED_TENANT_LABEL,
    SeedBook,
    derive_task_id,
    file_sha256,
    load_manifest,
    plan_seed,
    seed_user_id,
)

if TYPE_CHECKING:
    from uuid import UUID

_USER = uuid.UUID("d296b559-28f8-54d6-9577-a5539913335c")
_SHA_A = "a" * 64
_SHA_B = "b" * 64


def _entry(**overrides: str) -> dict[str, str]:
    base = {
        "title": "All of Grace",
        "author": "Charles H. Spurgeon",
        "source": "ccel",
        "source_id": "ccel/spurgeon/grace",
        "source_url": "https://www.ccel.org/ccel/spurgeon/grace",
        "download_url": "https://www.ccel.org/ccel/s/spurgeon/grace/cache/grace.epub",
        "license": "public-domain",
        "filename": "all-of-grace-spurgeon.epub",
    }
    base.update(overrides)
    return base


def _write_manifest(tmp_path: Path, entries: list[dict[str, str]]) -> Path:
    path = tmp_path / "manifest.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# load_manifest — strict rights-record validation
# ---------------------------------------------------------------------------


def test_load_manifest_happy_path(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        [_entry(), _entry(title="Confessions", filename="confessions-augustine.epub")],
    )
    books = load_manifest(path)
    assert len(books) == 2
    assert isinstance(books[0], SeedBook)
    assert books[0].title == "All of Grace"
    assert books[0].license == "public-domain"
    assert books[1].filename == "confessions-augustine.epub"


def test_load_manifest_tolerates_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "manifest.jsonl"
    path.write_text("\n" + json.dumps(_entry()) + "\n\n", encoding="utf-8")
    assert len(load_manifest(path)) == 1


def test_load_manifest_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="seed manifest not found"):
        load_manifest(tmp_path / "nope.jsonl")


def test_load_manifest_empty_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "manifest.jsonl"
    path.write_text("\n\n", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest is empty"):
        load_manifest(path)


def test_load_manifest_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "manifest.jsonl"
    path.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 1: invalid JSON"):
        load_manifest(path)


def test_load_manifest_rejects_non_object_line(tmp_path: Path) -> None:
    path = tmp_path / "manifest.jsonl"
    path.write_text(json.dumps(["not", "an", "object"]) + "\n", encoding="utf-8")
    with pytest.raises(TypeError, match="expected a JSON object"):
        load_manifest(path)


def test_load_manifest_rejects_unknown_field(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, [_entry(notes="surprise")])
    with pytest.raises(ValueError, match=r"unknown field\(s\) \['notes'\]"):
        load_manifest(path)


@pytest.mark.parametrize(
    "field",
    ["title", "author", "source", "source_id", "source_url", "download_url", "license", "filename"],
)
def test_load_manifest_rejects_missing_field(tmp_path: Path, field: str) -> None:
    entry = _entry()
    del entry[field]
    path = _write_manifest(tmp_path, [entry])
    with pytest.raises(ValueError, match=f"field {field!r} must be a non-empty string"):
        load_manifest(path)


def test_load_manifest_rejects_empty_field(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, [_entry(author="   ")])
    with pytest.raises(ValueError, match="field 'author' must be a non-empty string"):
        load_manifest(path)


@pytest.mark.parametrize(
    "bad_license",
    [
        "CC-BY-4.0",
        "all-rights-reserved",
        "Public Domain",  # case/spelling-exact on purpose — the field is machine-checked
        "public-domain-ish",
    ],
)
def test_load_manifest_rejects_non_public_domain_license(
    tmp_path: Path,
    bad_license: str,
) -> None:
    """Policy enforcement in code: the seeder refuses gray-area entries."""
    path = _write_manifest(tmp_path, [_entry(license=bad_license)])
    with pytest.raises(ValueError, match="public-domain only"):
        load_manifest(path)


def test_load_manifest_rejects_unknown_source(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, [_entry(source="random-blog")])
    with pytest.raises(ValueError, match="source 'random-blog' is not in"):
        load_manifest(path)


@pytest.mark.parametrize(
    "bad_filename",
    [
        "../escape.epub",  # traversal — joined onto --files-dir
        "/etc/passwd",
        "dir/inside.epub",
        ".hidden.epub",
        "UPPER.EPUB",  # lowercase-kebab convention is load-bearing for golden rows
        "spaces in name.epub",
        "book.txt",  # worker accepts EPUB/PDF only (libmagic-dispatched)
        "book.epub.exe",
    ],
)
def test_load_manifest_rejects_unsafe_filenames(tmp_path: Path, bad_filename: str) -> None:
    path = _write_manifest(tmp_path, [_entry(filename=bad_filename)])
    with pytest.raises(ValueError, match="fails the allowlist"):
        load_manifest(path)


def test_load_manifest_accepts_pdf_filenames(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, [_entry(filename="some-sermons.pdf")])
    assert load_manifest(path)[0].filename == "some-sermons.pdf"


def test_load_manifest_rejects_duplicate_filenames(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, [_entry(), _entry(title="Other")])
    with pytest.raises(ValueError, match="duplicate filename"):
        load_manifest(path)


# ---------------------------------------------------------------------------
# The committed manifest is itself policy-audited (keyless, every CI run)
# ---------------------------------------------------------------------------


def test_committed_manifest_is_policy_clean() -> None:
    """worker/seeds/manifest.jsonl must always satisfy docs/CORPUS_POLICY.md."""
    books = load_manifest(DEFAULT_MANIFEST)
    assert len(books) >= 8
    assert all(b.license == "public-domain" for b in books)
    assert all(b.source in {"gutenberg", "ccel"} for b in books)
    # The starter authors the Phase 23 plan names, plus the "grace" anchor
    # that the golden 'grace' row needs corpus support from.
    authors = " | ".join(b.author for b in books)
    for required in ("Augustine", "Calvin", "Spurgeon", "Wesley"):
        assert required in authors
    assert any(b.filename == "all-of-grace-spurgeon.epub" for b in books)


# ---------------------------------------------------------------------------
# derive_task_id — the deterministic Phase 20 idempotency token
# ---------------------------------------------------------------------------


def test_derive_task_id_is_deterministic() -> None:
    first = derive_task_id(sha256_hex=_SHA_A, user_id=_USER)
    second = derive_task_id(sha256_hex=_SHA_A, user_id=_USER)
    assert first == second
    assert first == uuid.uuid5(SEED_TASK_NAMESPACE, f"{_SHA_A}:{_USER}")


def test_derive_task_id_varies_with_content() -> None:
    """A replaced file must mint a fresh token — never inherit a stale claim."""
    assert derive_task_id(sha256_hex=_SHA_A, user_id=_USER) != derive_task_id(
        sha256_hex=_SHA_B,
        user_id=_USER,
    )


def test_derive_task_id_varies_with_user() -> None:
    other = uuid.uuid4()
    assert derive_task_id(sha256_hex=_SHA_A, user_id=_USER) != derive_task_id(
        sha256_hex=_SHA_A,
        user_id=other,
    )


@pytest.mark.parametrize(
    "bad_sha",
    [
        "",
        "abc123",
        "a" * 63,
        "a" * 65,
        "A" * 64,  # hashlib hexdigest is lowercase; uppercase means a foreign value
        "g" * 64,  # non-hex
    ],
)
def test_derive_task_id_rejects_bad_sha(bad_sha: str) -> None:
    with pytest.raises(ValueError, match="64 lowercase hex chars"):
        derive_task_id(sha256_hex=bad_sha, user_id=_USER)


def test_seed_task_namespace_is_pinned() -> None:
    """Changing the namespace orphans prior runs' upload_tasks claims."""
    assert SEED_TASK_NAMESPACE == uuid.UUID("00000000-0000-0000-0000-000000000002")


# ---------------------------------------------------------------------------
# seed_user_id — the documented corpus-seed identity
# ---------------------------------------------------------------------------


def test_seed_user_id_matches_enqueue_label_derivation() -> None:
    """`make enqueue TENANT=corpus-seed` must resolve to the same user."""
    expected = uuid.uuid5(LABEL_NAMESPACE, f"{SEED_TENANT_LABEL}.{LABEL_EMAIL_DOMAIN}")
    assert seed_user_id() == expected


def test_seed_user_id_is_pinned() -> None:
    """The literal identity documented in the script/runbook/AGENTS.md."""
    assert SEED_TENANT_LABEL == "corpus-seed"
    assert seed_user_id() == uuid.UUID("d296b559-28f8-54d6-9577-a5539913335c")


# ---------------------------------------------------------------------------
# file_sha256 + plan_seed
# ---------------------------------------------------------------------------


def test_file_sha256_matches_hashlib(tmp_path: Path) -> None:
    path = tmp_path / "book.epub"
    path.write_bytes(b"epub bytes " * 1000)
    assert file_sha256(path) == hashlib.sha256(b"epub bytes " * 1000).hexdigest()


def _book(filename: str, title: str = "T") -> SeedBook:
    return SeedBook(
        title=title,
        author="A",
        source="gutenberg",
        source_id="1",
        source_url="https://www.gutenberg.org/ebooks/1",
        download_url="https://www.gutenberg.org/ebooks/1.epub3.images",
        license="public-domain",
        filename=filename,
    )


def test_plan_seed_classifies_present_missing_and_duplicates(tmp_path: Path) -> None:
    (tmp_path / "one.epub").write_bytes(b"alpha")
    (tmp_path / "clone.epub").write_bytes(b"alpha")  # byte-identical to one.epub
    (tmp_path / "two.epub").write_bytes(b"beta")
    books = [_book("one.epub"), _book("missing.epub"), _book("clone.epub"), _book("two.epub")]

    plan = plan_seed(books, files_dir=tmp_path, user_id=_USER)

    assert [item.book.filename for item in plan] == [b.filename for b in books]  # order kept
    one, missing, clone, two = plan

    sha_alpha = hashlib.sha256(b"alpha").hexdigest()
    assert one.present and one.duplicate_of is None
    assert one.sha256_hex == sha_alpha
    assert one.task_id == derive_task_id(sha256_hex=sha_alpha, user_id=_USER)

    assert not missing.present
    assert missing.sha256_hex is None and missing.task_id is None
    assert missing.path == tmp_path / "missing.epub"

    # Identical bytes: the second entry is parked, never enqueued — one
    # Celery task id must never carry two broker messages in one run.
    assert clone.present and clone.duplicate_of == "one.epub"
    assert clone.task_id is None

    assert two.present and two.task_id is not None
    assert two.task_id != one.task_id


def test_plan_seed_task_ids_differ_per_user(tmp_path: Path) -> None:
    (tmp_path / "one.epub").write_bytes(b"alpha")
    books = [_book("one.epub")]
    other: UUID = uuid.uuid4()
    plan_a = plan_seed(books, files_dir=tmp_path, user_id=_USER)
    plan_b = plan_seed(books, files_dir=tmp_path, user_id=other)
    assert plan_a[0].task_id != plan_b[0].task_id
