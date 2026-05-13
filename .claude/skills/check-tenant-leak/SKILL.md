---
name: check-tenant-leak
description: Audit codebase for unscoped DB or vector queries
disable-model-invocation: true
---

# check-tenant-leak

Grep-based companion to the [`tenant-auditor`](../../agents/tenant-auditor.md)
subagent. `tenant-auditor` reasons across files; this skill is the fast
mechanical sweep — run it before merging anything that touches a Milvus
search, a SQLAlchemy query, or an auth dependency.

The invariants come from
[`ARCHITECTURE.md` §3](../../../ARCHITECTURE.md#3-milvus-schema--library_vectors)
+ [§7.1](../../../ARCHITECTURE.md#71-dedup-vs-isolation-milvus-partition-key)
and [`CLAUDE.md` "Tenant isolation is not negotiable"](../../../CLAUDE.md):

- Every Milvus search MUST filter on `book_id IN (<user's library>)`.
- Every `user_library`, `highlights`, `collections` query MUST filter on
  `user_id` derived from the JWT — never from the request body / params.
- `highlights` is doubly scoped: `user_id` AND `book_id`.

## Run

From the repo root:

```sh
# 1. Milvus search sites — every match must show a `filter=` / `expr=`
#    with `book_id in [...]` nearby.
rg -n --type py -C 5 'client\.search\(|collection\.search\(' worker/ api/ 2>/dev/null

# 2. SQLAlchemy / raw-SQL query sites — every match against
#    user_library / highlights / collections must show a user_id filter.
rg -n --type py -C 5 'session\.(query|execute|scalars?)\(|await session\.|conn\.execute\(' worker/ api/ 2>/dev/null

# 3. Suspicious sources: any user_id / book_id pulled from a request
#    body, query param, header, or path — should be JWT-derived instead.
rg -n --type py \
  '(user_id|book_id)\s*[:=].*\b(request|body|query_params|headers|path_params)\b' \
  worker/ api/ 2>/dev/null

# 4. Final gate (live Milvus required, so `make up` first):
( cd worker && make test-isolation )
```

## How to read the output

For each match from #1 and #2: open the file and verify the filter
predicate is present **and** the value bound to it comes from a
JWT-derived `user_id` (directly, or via a `user_library` lookup keyed by
JWT-`user_id`). A search with a `filter=` argument is not enough — the
argument must include `book_id in [...]` derived from server-side state.

#3 should return zero hits in production code. Test fixtures that
construct synthetic IDs are fine; flag anything in `api/` or
`worker/ingest.py` / `worker/tasks/` paths.

## On any finding

Halt. Surface the file path, line number, and the offending construct
verbatim — do not paraphrase. Tenant-isolation findings block merge
until fixed. The Phase 3 isolation test is the load-bearing enforcement;
if step 4 fails, see
[`.claude/skills/test-isolation/SKILL.md`](../test-isolation/SKILL.md)
for the failure protocol.
