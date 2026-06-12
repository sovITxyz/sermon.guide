#!/usr/bin/env bash
# Live-suite runner + skip classifier behind `make test-live` (Phase 23).
#
# Why this exists: plain `make test` is keyless-fast BY DESIGN — it never
# sources infra/.env, so on a keyed dev box the live-gated suites (golden
# retrieval, ingest e2e incl. kill-9 redelivery, embedding weight-parity)
# skip silently under it (Phase 20 deviation ii). `make test-live` sources
# ../infra/.env first (the migrate-up pattern), then runs this script, which:
#
#   1. refuses to run if DEEPINFRA_API_KEY is empty — every keyed test would
#      skip, which is exactly the trap this target exists to kill;
#   2. runs the live suites with `-rs` + COLUMNS=200 so pytest emits full,
#      untruncated `SKIPPED` reason lines;
#   3. reuses the CI live-gate guard's classification locally
#      (.github/workflows/ci.yml, retrieval-golden-live job, "Live-gate
#      guard" step): the ONLY tolerated skip reason is the corpus gap —
#      lines matching "corpus sample(s) missing" (case-insensitive). Any
#      other skip (key/infra/WordNet/collection) is a wiring bug → exit 1.
#
# The tolerated-substring is load-bearing API shared with
# tests/test_retrieval_golden.py and the CI guard — change all three in
# lockstep or none.
#
# Exit codes: 0 = all live suites ran (corpus-gap skips, if any, warned);
# 1 = test failure or non-corpus skip; 2 = env not wired.
set -euo pipefail

if [ -z "${DEEPINFRA_API_KEY:-}" ]; then
    echo 'test-live: DEEPINFRA_API_KEY is empty.' >&2
    echo 'test-live: run via `make test-live` (it sources ../infra/.env) and set the operator key in infra/.env — the tracked example ships it blank; never commit the real one.' >&2
    exit 2
fi

LOG_FILE="${SERMON_TEST_LIVE_LOG:-/tmp/pytest-worker-test-live.log}"
SUITES=(
    tests/test_retrieval_golden.py
    tests/test_ingest.py
    tests/test_embedding.py
)

pytest_exit=0
COLUMNS=200 uv run pytest "${SUITES[@]}" -v -rs 2>&1 | tee "$LOG_FILE" || pytest_exit=$?

skips="$(grep -E '^SKIPPED' "$LOG_FILE" || true)"
if [ -n "$skips" ]; then
    bad="$(printf '%s\n' "$skips" | grep -viE 'corpus sample\(s\) missing' || true)"
    if [ -n "$bad" ]; then
        printf '%s\n' "$bad" >&2
        echo 'test-live: non-corpus skips above must RUN here (compose up, env sourced, key set). Fix the wiring; do not exempt the test.' >&2
        exit 1
    fi
    echo 'test-live: tolerated corpus-gap skip(s) below — add the sample files under tests/samples/ to activate them:' >&2
    printf '%s\n' "$skips" >&2
fi

exit "$pytest_exit"
