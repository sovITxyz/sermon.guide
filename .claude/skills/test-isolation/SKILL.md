---
name: test-isolation
description: Run multi-tenant isolation tests after schema or query changes
disable-model-invocation: true
---

# test-isolation

Multi-tenant data leakage is the #1 architectural risk on sermon.guide. The
vector layer is shared (one set of vectors per deduped book, partitioned on
`book_id` — see [ARCHITECTURE.md §7.1](../../../ARCHITECTURE.md#71-dedup-vs-isolation-milvus-partition-key)).
Tenant scoping lives at the API: every Milvus search MUST pass
`book_id IN (<user's library>)` as the filter expression. If that filter
pushdown silently regresses, every user can read every other user's library.

This skill runs the isolation smoke test described in
[CLAUDE.md "Tenant isolation is not negotiable"](../../../CLAUDE.md). Invoke
it before merging anything that touches a Milvus or DB query, search code,
auth, or ingestion.

## Run

```sh
cd worker && make test-isolation
```

Requires a live Milvus (`make up` from the repo root). If Milvus is
unreachable the tests skip cleanly with a clear message — that is **not** a
pass: bring Milvus up and run again.

## On failure

**Halt immediately.** Report the failing test's failure-mode docstring
verbatim — do not paraphrase, do not summarize, do not paper over with a
retry or a marker skip.

A failing isolation test is a CVE-class signal: either tenant scoping is
broken or the test no longer matches the schema. Both block merge.

Then surface the full pytest output for whoever invoked the skill and stop.
The fix is owner-decision territory — never tweak the test to make it pass.
