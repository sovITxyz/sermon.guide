"""SQLAlchemy ORM models — Postgres schema for sermon.guide.

The tables here are the executable form of ``ARCHITECTURE.md`` §4. Keep
this module schema-only (no queries, no business logic); ``api/`` and worker
ingest do all the reading and writing.

Tenant invariants live at the API layer, not in the schema: every query
against ``user_library``, ``highlights``, or ``collections`` must filter by
``user_id`` derived from the request's JWT (see the root ``CLAUDE.md``).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for the sermon.guide schema."""


class Meta(Base):
    """Deployment-scoped key/value facts — currently the embedding-space pin.

    Phase 16b (ADR 0006). ``key='embedding_model_id'`` records which
    embedding model produced every vector in Milvus; the migration that
    creates this table seeds it with the v0 locked model
    (``BAAI/bge-large-en-v1.5``). ``worker/embedding.py`` refuses to embed
    when ``SERMON_EMBEDDINGS_MODEL`` disagrees with the recorded value —
    silent provider/model drift would mix embedding spaces and quietly
    destroy retrieval. Changing embedders is a deliberate migration
    (re-embed the corpus, recalibrate thresholds, update this row), never
    an env flip.
    """

    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class GlobalBook(Base):
    __tablename__ = "global_books"

    book_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    isbn: Mapped[str | None] = mapped_column(String(20), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 5-shingle MinHash signature serialized to bytes; queried via the LSH
    # index rebuilt in-process on worker start (see Phase 8).
    minhash_signature: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Object-storage key for the raw upload (R2/B2 in Phase 14+, local path
    # before that). Postgres only stores the pointer; the bytes live elsewhere.
    text_pointer: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class Chunk(Base):
    """One semantic chunk per ingested book — the BM25 side of hybrid retrieval.

    Phase 12 / ADR 0004. Mirrors what ``worker/ingest.py`` writes into
    Milvus, so RRF fusion can identify the same retrievable unit on both
    arms via ``(book_id, chunk_index)``. ``content`` carries the same
    bytes as the Milvus ``content_chunk`` field; the generated ``tsv``
    column is what the GIN index serves.

    Tenant scoping lives at the query layer — every sparse search
    filters ``book_id = ANY(<user's library>)`` the same way the dense
    arm filters Milvus (ARCHITECTURE.md §7.1, CLAUDE.md).
    """

    __tablename__ = "chunks"

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    book_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # CASCADE: chunks have no value once their global_books row is
        # gone. user_library still holds the dedup invariant via its own
        # RESTRICT FK (you can't drop a book that any user owns), so a
        # CASCADE here only fires when the book is truly being deleted.
        ForeignKey("global_books.book_id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    parent_section: Mapped[str | None] = mapped_column(Text, nullable=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    # PostgreSQL GENERATED ALWAYS AS … STORED — kept in sync with
    # ``content`` by the DB; the application never writes it. Declared
    # ``Mapped[str]`` for the type-checker; the column type is TSVECTOR.
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', content)", persisted=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        # (book_id, chunk_index) is the chunk's identity — the same key
        # the dense arm produces from Milvus metadata. Idempotent ingest
        # + backfill upserts ON CONFLICT against this constraint.
        UniqueConstraint("book_id", "chunk_index", name="uq_chunks_book_chunk"),
        # B-tree on book_id so the ``book_id = ANY(...)`` tenant filter
        # combines with the GIN-on-tsv via Postgres's bitmap heap scan.
        Index("ix_chunks_book_id", "book_id"),
        # GIN over the generated tsvector. ``postgresql_using="gin"`` is
        # the only thing that makes the tsvector match operator (``@@``)
        # fast at corpus scale.
        Index("ix_chunks_tsv_gin", "tsv", postgresql_using="gin"),
    )


class UserLibraryEntry(Base):
    __tablename__ = "user_library"

    entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    book_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("global_books.book_id", ondelete="RESTRICT"),
        nullable=False,
    )
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "book_id", name="uq_user_library_user_book"),
        # Hot path: resolve a user's full book_id set for every Milvus search.
        Index("ix_user_library_user_id", "user_id"),
    )


class UploadTask(Base):
    """Ownership + idempotency record for one enqueued ingest task (Phase 20).

    One row per ``POST /upload``. Two jobs:

    - **Ownership.** ``GET /tasks/{task_id}`` resolves the row scoped to the
      JWT-derived ``user_id`` — a non-owned or nonexistent task is a uniform
      404, replacing the Phase 10 "122-bit task_id is the capability" model.
      The api inserts (and commits) the row *before* ``send_task`` so a crash
      between the two can never produce a running task its owner cannot see.
    - **Idempotency claim.** ``book_id`` records the in-flight book a worker
      attempt minted on the new-book path, written *before* the first
      non-transactional write (MinIO original, Milvus vectors). On Celery
      redelivery after a mid-window crash, ``worker/ingest.py`` reads the
      claim, scrubs the partial vectors, and re-runs under the SAME book_id —
      converging to one consistent record instead of orphaning the crashed
      attempt's vectors (the documented Phase 9 window).

    ``task_id`` is the Celery task UUID, minted by the api and passed to
    ``send_task(task_id=...)`` — never a column default. ``book_id``
    deliberately has NO foreign key to ``global_books``: the claim exists
    precisely to name a book whose ``global_books`` row may never land.
    """

    __tablename__ = "upload_tasks"

    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    # In-flight claim — see class docstring. NULL until a worker attempt
    # reaches the new-book path; dup-hits never claim (that path is already
    # idempotent end to end).
    book_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        # Hot path: "my uploads" listings + the per-poll ownership check.
        Index("ix_upload_tasks_user_id", "user_id"),
    )


class Highlight(Base):
    __tablename__ = "highlights"

    highlight_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    book_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("global_books.book_id", ondelete="CASCADE"),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Milvus id of the chunk this highlight anchors to, when known. INT64
    # to match the ``library_vectors`` PK (see ARCHITECTURE.md §3).
    vector_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        # Doubly-scoped queries (user_id AND book_id) per the tenant invariant
        # in CLAUDE.md.
        Index("ix_highlights_user_book", "user_id", "book_id"),
    )


class Collection(Base):
    __tablename__ = "collections"

    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (Index("ix_collections_user_id", "user_id"),)
