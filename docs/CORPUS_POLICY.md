# Corpus sourcing & licensing policy

What content is allowed into the **platform seed corpus** (Phase 23), where
it comes from, and how rights stay auditable. The operator recipe for
actually ingesting it lives in [SEED_CORPUS.md](./SEED_CORPUS.md).

## The two content paths

1. **Seed corpus (platform-provided)** — **public domain only.** Classic
   theological works sourced from [Project Gutenberg](https://www.gutenberg.org)
   and the [Christian Classics Ethereal Library (CCEL)](https://www.ccel.org).
   These are ingested under the dedicated `corpus-seed` user (see
   "Ownership" below) and shared at the vector layer by dedup like any
   other book.
2. **User uploads (tenant path)** — the ONLY way copyrighted material
   enters the system. A user may upload books they own; ownership is
   recorded per-user in `user_library`, vectors are deduplicated globally,
   and tenant scoping at the API (`book_id IN (<user's library>)`) keeps
   one user's uploads invisible to everyone else
   ([ARCHITECTURE.md §7.1](../ARCHITECTURE.md#71-dedup-vs-isolation-milvus-partition-key)).

**No gray-area content, ever.** Not "abandonware", not out-of-print works
still in copyright, not scanned modern editions, not fan re-uploads.
Watch translations in particular: a 2001 translation of a 4th-century text
is a **separate copyrighted work** — only public-domain translations are
seedable (e.g. Pusey's Confessions, Beveridge's Institutes).

Per [ADR 0003](./adr/0003-embedding-model-choice.md) the corpus is
**English-first** (the embedding model is `BAAI/bge-large-en-v1.5`);
multilingual seeding is a v2 concern.

## The manifest is the rights record

The repo holds **only the manifest** — never ebook files. The tracked
manifest is [`worker/seeds/manifest.jsonl`](../worker/seeds/manifest.jsonl),
one JSON object per line:

| field          | meaning                                                          |
| -------------- | ---------------------------------------------------------------- |
| `title`        | canonical title                                                  |
| `author`       | canonical author                                                  |
| `source`       | `gutenberg` or `ccel` (allowlisted — anything else is refused)   |
| `source_id`    | Gutenberg ebook number, or CCEL path (`ccel/<author>/<work>`)    |
| `source_url`   | the catalog page where the rights status is verifiable           |
| `download_url` | the direct EPUB fetch URL (used by the runbook's curl lines)     |
| `license`      | must be exactly `public-domain` — machine-checked                |
| `filename`     | expected on-disk name: lowercase-kebab, `.epub`/`.pdf` only      |

Enforcement is in code, not just prose: `scripts/seed_corpus.py` refuses
any entry whose `license` is not `public-domain` or whose `source` is not
allowlisted, and the keyless unit test
`tests/test_seed_corpus.py::test_committed_manifest_is_policy_clean`
audits the committed manifest on every CI run. Adding a book = adding a
manifest line that passes those gates, with the `source_url` pointing at a
page that demonstrates public-domain status.

Downloaded files live in `worker/tests/samples/` (gitignored — the same
location the golden retrieval suite resolves sample filenames from, so one
download serves both the live seed and the golden rows). The `filename`
convention (lowercase-kebab, exact match) is load-bearing: golden query
rows reference books by exact on-disk filename.

## Starter corpus (v0 seed)

The Phase 23 manifest — verified live against the sources on 2026-06-12:

| Title                                  | Author                  | Source / id                    | PD basis                          |
| -------------------------------------- | ----------------------- | ------------------------------ | --------------------------------- |
| The Confessions of St. Augustine       | Augustine of Hippo      | Gutenberg `3296`               | Pusey translation (1838)          |
| Institutes of the Christian Religion   | John Calvin             | CCEL `ccel/calvin/institutes`  | Beveridge translation (1845)      |
| All of Grace                           | Charles H. Spurgeon     | CCEL `ccel/spurgeon/grace`     | original English (1886)           |
| Sermons on Several Occasions           | John Wesley             | CCEL `ccel/wesley/sermons`     | original English (18th c.)        |
| The Pilgrim's Progress                 | John Bunyan             | Gutenberg `131`                | original English (1678)           |
| The Imitation of Christ                | Thomas à Kempis         | Gutenberg `1653`               | PD translation (Gutenberg ed.)    |
| The Practice of the Presence of God    | Brother Lawrence        | Gutenberg `5657`               | PD translation (Gutenberg ed.)    |
| On the Incarnation of the Word         | Athanasius of Alexandria | CCEL `ccel/athanasius/incarnation` | Robertson translation (1892) |

Spurgeon's *All of Grace* is deliberately in the starter set: it gives the
golden suite's "grace" query real corpus support (the Phase 23 plan's
motivating gap — the spec's own "grace" query prunes to zero on the 5
synthetic dev books).

Source notes:

- **Project Gutenberg** items are public domain in the USA (each book page
  carries the copyright status; the Project Gutenberg trademark license
  applies to redistribution *of their files with the trademark*, not to the
  underlying public-domain text — we ingest for retrieval, we do not
  redistribute the files, and the repo never contains them).
- **CCEL** hosts classic Christian texts whose underlying works are public
  domain; verify the individual book page (linked as `source_url`) before
  adding an entry. CCEL's value-added formatting claims do not attach to
  the public-domain text itself.

## Ownership of seeded books

Seeded books are owned by the deterministic **`corpus-seed`** user
(`user_id d296b559-28f8-54d6-9577-a5539913335c`, email
`corpus-seed@tenants.sermon.guide.local` — the same identity
`make enqueue TENANT=corpus-seed` resolves). Seeding writes `user_library`
rows **only** for that user; no existing tenant's library is touched, and
no tenant can see seeded books until a product surface explicitly grants
them (or their own upload of identical content dedup-hits onto the shared
`book_id`). The post-seed gates (`make test-isolation` +
`/check-tenant-leak`) verify exactly that — see the
[runbook](./SEED_CORPUS.md).
