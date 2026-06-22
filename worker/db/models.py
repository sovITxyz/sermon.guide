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
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
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


class ReadingPosition(Base):
    """Last-read location per (user, book) — the reader's resume point (Phase 32).

    One row per user per book, upserted by ``PUT /books/{book_id}/position``
    ON CONFLICT against ``uq_reading_positions_user_book``. ``chunk_index``
    is the last-read chunk; ``offset_ratio`` optionally refines it to a
    0.0–1.0 scroll position within that chunk (validated at the API layer,
    per the schema-only rule above).

    Doubly-scoped like ``highlights``: every query MUST filter by both
    ``user_id`` (JWT-derived) and ``book_id``. The ``/library`` progress
    join in particular must be ON (user_id AND book_id) — book_id alone
    would leak another tenant's position for a shared deduped book.
    """

    __tablename__ = "reading_positions"

    position_id: Mapped[uuid.UUID] = mapped_column(
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
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    offset_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        # One position per (user, book) — PUT upserts ON CONFLICT here. The
        # backing unique index also serves every doubly-scoped lookup (and,
        # via its user_id prefix, the per-user /library progress join), so
        # no separate index is needed.
        UniqueConstraint("user_id", "book_id", name="uq_reading_positions_user_book"),
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


class Document(Base):
    """A user's sermon document — canonical TipTap/ProseMirror JSON (Phase 34).

    The storage half of the B2 sermon editor (slice A). ``content`` holds the
    canonical ProseMirror/TipTap node tree as JSONB; ``content_text`` is the
    server-derived plain-text projection used for list previews and future
    FTS — it is NEVER accepted from the client, the API re-derives it from
    ``content`` on every write by walking the node tree. ``schema_version``
    is server-managed (a module constant in ``api/documents.py``), not
    client-supplied.

    User-owned like ``highlights``: every query MUST filter by ``user_id``
    (JWT-derived); a non-owned ``document_id`` is a uniform 404 with no
    existence oracle (the Phase 20 ``/tasks`` posture). Deletion is soft —
    ``deleted_at`` flips from NULL (active) to a timestamp; ``POST
    /documents/{document_id}/restore`` clears it.

    ``updated_at`` carries the repo's ``server_default=func.now()`` for the
    insert, but has NO ``onupdate`` (the schema-wide convention): PATCH bumps
    it EXPLICITLY via ``func.now()`` so the value is read back for the
    single-author optimistic-concurrency ``base_updated_at`` 409 gate. An
    ``onupdate`` here would silently change the column outside that gate.
    """

    __tablename__ = "documents"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # Canonical sermon body — ProseMirror/TipTap JSON node tree (Cross-item
    # contract). JSONB so the document survives round-trips without the
    # string-syntax corruption a markdown-canonical form would suffer.
    content: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    # Server-derived plain text from ``content`` (text-node concatenation,
    # block nodes joined with newlines). Re-derived on every write; the
    # client never supplies it. Backs list previews and future FTS.
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Server-managed ProseMirror schema version (a constant in the API),
    # never client-supplied.
    schema_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
    )
    # Soft-delete sentinel: NULL = active, a timestamp = deleted. Restore
    # clears it back to NULL.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        # Hot path: a user's sermon list ordered newest-first. The DESC on
        # ``updated_at`` matches the list query's ORDER BY so the planner
        # walks the index in order; ``user_id`` prefix scopes it per tenant.
        Index(
            "ix_documents_user_updated",
            "user_id",
            text("updated_at DESC"),
        ),
    )


class SermonDocRevision(Base):
    """A prior-content snapshot of a sermon document — docx-import undo (Phase 43).

    The snapshot-first half of the B2 docx round-trip. ``POST
    /documents/{document_id}/import`` accepts an attacker-controlled .docx,
    converts it (pandoc + the ``worker.convert`` Node leg) and OVERWRITES
    ``documents.content``. Before that overwrite, in ONE transaction, the API
    inserts a row here holding the CURRENT (pre-overwrite) ``content`` /
    ``content_text`` so an import is never destructive — the prior state is
    always recoverable.

    ``content`` is the PRIOR ProseMirror/TipTap JSON node tree (JSONB);
    ``content_text`` is the prior server-derived plain-text projection
    (re-derived from ``content``, NEVER accepted from the client — same rule
    as ``Document``). ``schema_version`` mirrors ``documents.schema_version``
    (server-managed). ``source`` records what triggered the snapshot
    (DEFAULT ``'import'``).

    ``user_id`` is DENORMALIZED — duplicated from the owning ``documents``
    row — so the tenant gate filters revisions by the JWT-derived ``user_id``
    WITHOUT a join back to ``documents`` (which may itself be soft-deleted).
    Like every user-owned table it MUST be queried scoped to ``user_id``
    derived from the request's JWT (CLAUDE.md), never from request input.

    Both FKs are ON DELETE CASCADE: a revision is a snapshot OF a document
    (gone with the document's real row delete — the documents API
    soft-deletes, so this fires only on a true delete) and is meaningless
    once its user is gone.

    ``created_at`` has the schema-wide ``server_default=func.now()``; there
    is no ``updated_at`` — a revision is an immutable snapshot.
    """

    __tablename__ = "sermon_doc_revisions"

    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.document_id", ondelete="CASCADE"),
        nullable=False,
    )
    # DENORMALIZED owner — duplicated from the documents row so the tenant
    # gate filters here without a join back to documents (which may be
    # soft-deleted). See class docstring.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    # PRIOR (pre-overwrite) ProseMirror/TipTap JSON node tree — the snapshot.
    content: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    # Prior server-derived plain text; re-derived from ``content``, never
    # client-supplied (same rule as Document).
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Mirrors documents.schema_version (server-managed).
    schema_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
    )
    # What triggered the snapshot — DEFAULT 'import'.
    source: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'import'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        # Revision-history hot path: newest snapshot first per document. The
        # DESC on ``created_at`` matches the history query's ORDER BY so the
        # planner walks the index in order; ``document_id`` prefix scopes it.
        Index(
            "ix_sermon_doc_revisions_document_created",
            "document_id",
            text("created_at DESC"),
        ),
    )


class OAuthConnection(Base):
    """An encrypted OAuth refresh-token vault row — one per (user, provider).

    The storage half of the B4 OAuth vault (Phase 44). The
    ``/integrations/{provider}/callback`` route exchanges a Google
    authorization code for tokens, then UPSERTs one row here keyed by
    ``(user_id, provider)``. The refresh token (and the optional short-lived
    access token) are stored ONLY as AES-256-GCM ciphertext — the api-side
    ``crypto_vault`` module encrypts before write and decrypts on use; the
    database never holds plaintext token material. The ONLY token-derived
    value ever returned to the browser is ``provider_account_email``.

    ``refresh_token_ciphertext`` / ``access_token_ciphertext`` are ``BYTEA``
    (mapped via ``LargeBinary``, the same type ``GlobalBook.minhash_signature``
    uses) holding the AESGCM layout ``nonce(12 bytes) || ciphertext+tag`` — a
    per-encryption random 96-bit nonce PREPENDED to the library's
    ciphertext+tag. ``access_token_ciphertext`` is NULLABLE (the vault strictly
    needs only the refresh token; storing the access token avoids a refresh
    round-trip on the first Phase 45 call). ``token_expiry`` is the stored
    access token's expiry (NULLABLE). ``provider`` is generic text ('google'
    now; 'microsoft' in Phase 46). ``scopes`` is the space-delimited granted
    scope string Google returns.

    User-owned like every other table here: every query MUST filter by
    ``user_id`` derived from the JWT (CLAUDE.md), never from request input.
    The FK -> ``users.user_id`` is ON DELETE CASCADE — a connection is
    meaningless once its user is gone.

    ``UniqueConstraint(user_id, provider)`` backs the callback's
    ``ON CONFLICT(user_id, provider) DO UPDATE`` (reconnect overwrites the row
    in place) AND serves the per-user list scan.

    ``updated_at`` carries ``server_default=func.now()`` for the insert but
    has NO ``onupdate`` (the schema-wide convention): the upsert bumps it
    EXPLICITLY via ``func.now()`` (the ``Document`` / ``SermonEvent``
    precedent).
    """

    __tablename__ = "oauth_connections"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    # Generic provider key — 'google' now, 'microsoft' in Phase 46.
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    # The ONLY token-derived value ever returned to the browser.
    provider_account_email: Mapped[str] = mapped_column(Text, nullable=False)
    # AESGCM ciphertext: 12-byte nonce PREPENDED to ciphertext+tag. Never
    # plaintext. ``LargeBinary`` maps to BYTEA on Postgres.
    refresh_token_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Optional short-lived access token (same nonce-prepended layout).
    access_token_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # Expiry of the stored access token (NULLABLE).
    token_expiry: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # Space-delimited granted scope string returned by Google.
    scopes: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        # Backs ON CONFLICT(user_id, provider) DO UPDATE (reconnect overwrites
        # in place) AND serves the per-user list scan.
        UniqueConstraint(
            "user_id",
            "provider",
            name="uq_oauth_connections_user_provider",
        ),
    )


class SermonEvent(Base):
    """A dated entry on a user's preaching calendar (Phase 38 — B3 slice).

    The server half of the B3 calendar: ``api/calendar.py`` does range-GET /
    POST (with an optional weekly materializer) / partial-PATCH / DELETE, all
    DOUBLE-scoped (``event_id`` AND ``user_id``). User-owned like
    ``highlights`` / ``documents``: every query MUST filter by ``user_id``
    (JWT-derived); a non-owned ``event_id`` is a uniform 404 with no existence
    oracle (the Phase 20 ``/tasks`` posture).

    ``event_date`` is a Postgres DATE, NOT a timestamptz — preaching is
    day-anchored, and a UTC-midnight timestamptz silently shifts a day for
    UTC-minus users. Dates stay ``YYYY-MM-DD`` end-to-end. This is the
    schema's first DATE column.

    ``document_id`` is a NULLABLE FK to ``documents`` with ``ON DELETE SET
    NULL`` (the schema's first SET NULL) — deleting the linked document
    detaches the event instead of cascading it away. The documents API
    soft-deletes (the row and link survive); the SET NULL is the defensive
    behaviour for a real row delete. Because ``document_id`` arrives as
    attacker-controlled body input, ``api/calendar.py`` ownership-checks it
    against the JWT user's documents before write — the FK alone does not
    scope tenancy.

    ``series`` is an optional free-text recurrence label (e.g. "Advent"); the
    weekly materializer writes INDEPENDENT rows (no parent linkage), so each
    materialized occurrence PATCHes / DELETEs on its own.

    There is DELIBERATELY no unique on ``(user_id, event_date)`` — two
    services on one Sunday is normal. The ``(user_id, event_date)`` index
    serves the range scan; ``event_date`` is bidirectional so a plain
    ascending column list suffices (no DESC trick).

    ``updated_at`` carries ``server_default=func.now()`` for the insert but
    has NO ``onupdate`` (the schema-wide convention): the API bumps it
    EXPLICITLY via ``func.now()`` on PATCH.
    """

    __tablename__ = "sermon_events"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    # Day-anchored DATE (not timestamptz) — see class docstring.
    event_date: Mapped[date] = mapped_column(Date(), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # Optional free-text recurrence/series label (B3 — NOT an RRULE).
    series: Mapped[str | None] = mapped_column(Text, nullable=True)
    # NULLABLE FK to documents with ON DELETE SET NULL — the schema's first
    # SET NULL. Tenancy on this column is enforced by the API ownership check,
    # not the FK (the FK only fires on a real documents row delete).
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.document_id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        # Range hot path: a user's events within [start, end). The user_id
        # prefix scopes per tenant; event_date carries the half-open range
        # scan. DELIBERATELY no unique on (user_id, event_date) — two
        # services one Sunday is normal.
        Index("ix_sermon_events_user_date", "user_id", "event_date"),
    )
