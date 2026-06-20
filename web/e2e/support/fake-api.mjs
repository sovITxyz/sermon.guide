// Lightweight in-memory fake of the sermon.guide api/ for the headless CI E2E
// path (Phase 25). It is NOT the real api — it speaks the exact WIRE shapes the
// web proxies consume (FastAPI `{detail}` errors, snake_case bodies) so the
// browser -> same-origin /api/* proxy -> api HTTP contract is exercised end to
// end without booting Postgres + Milvus + a Celery worker + a seeded corpus
// (the web CI job has no services — documented boundary in web/AGENTS.md).
//
// What it faithfully reproduces (the things the E2E asserts):
//   * /auth/signup   201 {user_id,email}; 409 {detail} on collision
//   * /auth/login    200 {access_token}; 401 {detail} on bad creds
//   * /search-summary  a GROUNDED summary whose [book:chunk] markers exactly
//                      match the returned citations' markers, so the Phase 24
//                      chip renderer resolves them in the real browser. No real
//                      LLM, no retrieval — the determinism the stub-llm gives
//                      the live path, baked straight in here.
//   * /search        RAW hybrid hits (Phase 37, no LLM) for the in-editor
//                    LibraryDrawer: {hits:[{book_id,content_chunk,metadata,
//                    score}], degraded:[]}, bearer-scoped. book_ids match
//                    /library so an inserted citation resolves as OWNED.
//   * /library       the owned-book listing (Phase 15 shape) the editor shell
//                    fetches once on doc open for the citation owned-set +
//                    {id -> title} map; bearer-scoped.
//   * /upload        202 {task_id,upload_id,filename}; records token-scoped
//                    ownership (Phase 20)
//   * /tasks/{id}    200 {task_id,status,result} when the bearer OWNS the task;
//                    a UNIFORM 404 {detail} for a non-owned OR unknown id (the
//                    no-existence-oracle contract the upload E2E asserts)
//   * /documents     POST create (201 full doc), GET list (preview-only items,
//                    no `content`), GET/{id} full, PATCH/{id} (200 full / 409 on
//                    stale base_updated_at), DELETE/{id} (204 soft), POST
//                    /{id}/restore (200 full, content intact) — all bearer-scoped
//                    with the SAME uniform 404 for non-owned/unknown ids (the
//                    Phase 35 editor smoke + the Phase 36 list delete/restore)
//
// The LIVE/nightly path uses the real api + e2e/support/stub-llm.mjs instead
// (see web/AGENTS.md). Tokens are opaque random strings — no JWT, no secrets.

import { randomUUID } from "node:crypto";
import { createServer } from "node:http";

const PORT = Number(process.env.PORT ?? process.env.FAKE_API_PORT ?? 8081);
const HOST = process.env.HOST ?? "127.0.0.1";

// The WEB origin this fake api drives the OAuth callback back to (Phase 44). In
// the real flow Google top-level-redirects the browser to the operator-
// registered web redirect URI; the stub `/oauth/consent` below stands in for
// Google's consent screen and 302s straight back to that callback with a
// deterministic code+state. Playwright sets this to the dev-server origin.
const WEB_ORIGIN = process.env.E2E_WEB_ORIGIN ?? "http://127.0.0.1:3100";

/** email -> { userId, password } */
const users = new Map();
/** token -> userId */
const sessions = new Map();
/** taskId -> { userId, filename } */
const tasks = new Map();
/**
 * documentId -> { userId, title, content, content_text, schema_version,
 * created_at, updated_at, deleted_at }. Mirrors the api/documents.py row well
 * enough for the editor smoke: preview-only list, full-doc GET, optimistic-
 * concurrency PATCH (409 on stale base_updated_at), uniform 404, soft delete.
 */
const documents = new Map();

/**
 * OAuth connections store (Phase 44 — backs /integrations). Keyed
 * `${userId}:${provider}` so a reconnect overwrites in place (the real api's
 * ON CONFLICT(user_id, provider) upsert). The wire shape carries NO token
 * material — only the provider, the account email fetched from the stubbed
 * userinfo, the scopes, and timestamps. The stub never stores or returns a
 * refresh/access token (the real vault encrypts those; the web layer never sees
 * them), so there is nothing token-shaped here to leak.
 *
 * `${userId}:${provider}` -> { provider, provider_account_email, scopes,
 *                              connected_at, token_expiry }.
 */
const oauthConnections = new Map();

/**
 * One-shot PKCE/state surrogates the stub mints at authorize and pops at
 * callback. The real api stores the PKCE verifier in Redis keyed by the state
 * nonce and validates the state HMAC + account binding before any token
 * exchange; the stub models only the SINGLE-USE, account-bound behavior the
 * E2E can observe: `state` -> { userId } popped on first callback. A second
 * redemption (or a state minted for a different user) 400s. `state` -> { userId,
 * provider }.
 */
const oauthStates = new Map();

const PREVIEW_CHARS = 280;
const SCHEMA_VERSION = 1;
const OAUTH_SCOPES = "openid email profile https://www.googleapis.com/auth/drive.file";

/**
 * Trivial plain-text projection of a ProseMirror/TipTap content tree — the
 * fake-api stand-in for api/documents.py:derive_content_text. Concatenates the
 * `text` of every node, depth-first, space-joined. Enough to back a non-empty
 * preview after the editor smoke types real text; the exact whitespace shape is
 * not asserted.
 */
function deriveContentText(content) {
  const parts = [];
  const stack = [content];
  while (stack.length > 0) {
    const node = stack.pop();
    if (!node || typeof node !== "object") {
      continue;
    }
    if (typeof node.text === "string") {
      parts.push(node.text);
    }
    if (Array.isArray(node.content)) {
      // Push in reverse so the leftmost child is processed first (document order).
      for (let i = node.content.length - 1; i >= 0; i--) {
        stack.push(node.content[i]);
      }
    }
  }
  return parts.join(" ").trim();
}

/** Full DocumentResponse shape (POST create, GET full, PATCH all return this). */
function fullDoc(id, record) {
  return {
    document_id: id,
    title: record.title,
    content: record.content,
    content_text: record.content_text,
    schema_version: record.schema_version,
    created_at: record.created_at,
    updated_at: record.updated_at,
  };
}

/** Preview-only DocumentSummary list item (no `content`). */
function summaryDoc(id, record) {
  return {
    document_id: id,
    title: record.title,
    preview: record.content_text.slice(0, PREVIEW_CHARS),
    schema_version: record.schema_version,
    created_at: record.created_at,
    updated_at: record.updated_at,
  };
}

function send(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json" });
  res.end(payload);
}

/** FastAPI's handled-error shape — the web proxies read `{detail}`. */
function detail(res, status, message) {
  send(res, status, { detail: message });
}

function readJson(req) {
  return new Promise((resolve) => {
    let raw = "";
    req.on("data", (chunk) => {
      raw += chunk;
    });
    req.on("end", () => {
      try {
        resolve(JSON.parse(raw || "{}"));
      } catch {
        resolve(null);
      }
    });
  });
}

function bearer(req) {
  const auth = req.headers.authorization ?? "";
  return auth.startsWith("Bearer ") ? auth.slice("Bearer ".length) : null;
}

function userIdFor(req) {
  const token = bearer(req);
  return token ? (sessions.get(token) ?? null) : null;
}

/**
 * A strictly-monotonic ISO timestamp. The optimistic-concurrency gate compares
 * `base_updated_at` against the stored `updated_at`, so two writes must never
 * collide on the same millisecond — a real DB-backed updated_at would differ,
 * and the 409 / round-trip assertions depend on it. The counter guarantees a
 * distinct, ordered value per call even within one millisecond.
 */
let timestampCounter = 0;
function nextTimestamp() {
  timestampCounter += 1;
  return new Date(Date.now() + timestampCounter).toISOString();
}

// A deterministic grounded summary. The markers below are the exact citation
// markers returned alongside it, so segmentSummary() resolves every one into a
// chip linking to its source card. Two distinct sources -> [1] and [2].
const CITATIONS = [
  {
    marker: "[Grace:3]",
    book_id: "11111111-1111-1111-1111-111111111111",
    title: "On Grace",
    chunk_index: 3,
    content:
      "Grace is the unearned favor of God, given freely and not as a wage for any work performed.",
    filename: "on-grace.epub",
    parent_section: "Chapter 1",
  },
  {
    marker: "[Faith:7]",
    book_id: "22222222-2222-2222-2222-222222222222",
    title: "Of Faith",
    chunk_index: 7,
    content: "Faith receives what grace offers; the two are never set against one another.",
    filename: "of-faith.epub",
    parent_section: "Chapter 2",
  },
];

const SUMMARY =
  "Grace is given freely, not earned [Grace:3], and faith is the means by " +
  "which it is received [Faith:7]. The passages agree on this point.";

// The user's owned library (Phase 37 — backs GET /library). Its book_ids are the
// SAME ones the raw-/search hits below reference, so a citation inserted from the
// drawer resolves as OWNED in the editor (the shell's one-shot /library fetch
// puts these ids in the owned set + the {id -> title} map): the inserted card
// shows the title + a Read-in-context link, NOT the degraded badge. Shape mirrors
// api/library.py LibraryResponse (the Phase-32 progress fields are nullable).
const LIBRARY = [
  {
    book_id: "11111111-1111-1111-1111-111111111111",
    title: "On Grace",
    author: "An Author",
    category: null,
    added_at: "2026-01-01T00:00:00Z",
    chunk_count: 100,
    last_chunk_index: null,
    progress: null,
  },
  {
    book_id: "22222222-2222-2222-2222-222222222222",
    title: "Of Faith",
    author: "An Author",
    category: null,
    added_at: "2026-01-01T00:00:00Z",
    chunk_count: 100,
    last_chunk_index: null,
    progress: null,
  },
];

// Deterministic RAW POST /search hits (Phase 37 — backs the in-editor
// LibraryDrawer). Shape mirrors api/search.py SearchHit EXACTLY: book_id +
// content_chunk + metadata{filename,chunk_index,parent_section} + score, NO
// top-level title and no `snippet` field (the drawer maps content_chunk ->
// snippet and sources the title from /library). The book_ids match LIBRARY so an
// inserted citation is owned.
const SEARCH_HITS = [
  {
    book_id: "11111111-1111-1111-1111-111111111111",
    content_chunk:
      "Grace is the unearned favor of God, given freely and not as a wage for any work performed.",
    metadata: { filename: "on-grace.epub", chunk_index: 3, parent_section: "Chapter 1" },
    score: 0.91,
  },
  {
    book_id: "22222222-2222-2222-2222-222222222222",
    content_chunk: "Faith receives what grace offers; the two are never set against one another.",
    metadata: { filename: "of-faith.epub", chunk_index: 7, parent_section: "Chapter 2" },
    score: 0.84,
  },
];

// Sermon-calendar event store (Phase 39 read + Phase 40 CRUD — backs
// /calendar/events). Each record mirrors api/calendar_routes.py CalendarEvent
// EXACTLY (snake_case, event_date a day-only YYYY-MM-DD string, series +
// document_id nullable) plus an internal `userId` for tenant scoping (the
// CalendarEvent wire shape has NO user_id — it is stripped before sending).
//
// The five deterministic 2028 seeds are owned by `userId: null` = SHARED /
// visible to ALL authenticated users, which is what the read-only Phase 39
// year/month specs assert (a freshly-signed-up user sees them). The dates sit
// in 2028 so the year E2E can spot-check a leap February (29 days) and a
// Sunday-starting October. Rows created via POST are owned by the creating user
// and are visible only to them (the no-existence-oracle 404 covers cross-tenant
// reads of a created row), so the Phase 40 mutation specs sign up a fresh user
// per spec and assert only their OWN rows.
//
// eventId -> { userId|null, event_date, title, series, document_id,
//              created_at, updated_at }.
const calendarEvents = new Map();
const CALENDAR_SEEDS = [
  ["aaaaaaaa-0000-0000-0000-000000000001", "2028-02-06", "Sermon on the Mount", "Matthew"],
  ["aaaaaaaa-0000-0000-0000-000000000002", "2028-02-29", "Leap-day Vespers", "Matthew"],
  ["aaaaaaaa-0000-0000-0000-000000000003", "2028-10-01", "Harvest Thanksgiving", "Psalms"],
  ["aaaaaaaa-0000-0000-0000-000000000004", "2028-10-01", "Evening Prayer", "Psalms"],
  ["aaaaaaaa-0000-0000-0000-000000000005", "2028-10-15", "Reformation Sunday", "Romans"],
];
for (const [eventId, eventDate, title, series] of CALENDAR_SEEDS) {
  calendarEvents.set(eventId, {
    userId: null, // shared/visible to all (the read-only specs depend on this)
    event_date: eventDate,
    title,
    series,
    document_id: null,
    created_at: "2028-01-01T00:00:00Z",
    updated_at: "2028-01-01T00:00:00Z",
  });
}

/** The materializer cap (api/calendar_routes.py MATERIALIZER_CAP_ROWS). */
const MATERIALIZER_CAP_ROWS = 53;

/**
 * A poison destination date (Phase 42 drag-rollback test): a PATCH that would
 * move an event ONTO this date returns a 500 instead of saving, so the E2E spec
 * can exercise CalendarView's optimistic-move ROLLBACK + visible-error path. It
 * sits in the Phase-42 test year (2031) on a date no spec legitimately drops to.
 */
const FORCE_500_DATE = "2031-12-25";

/** Public CalendarEvent wire shape — strips the internal `userId`. */
function calendarWire(eventId, record) {
  return {
    event_id: eventId,
    event_date: record.event_date,
    title: record.title,
    series: record.series,
    document_id: record.document_id,
    created_at: record.created_at,
    updated_at: record.updated_at,
  };
}

/**
 * Add `delta` calendar days to a YYYY-MM-DD string, returning a fresh
 * YYYY-MM-DD. Uses Date.UTC with numeric args (never `new Date("YYYY-MM-DD")`)
 * so it is timezone-immune — mirrors web/lib/dates.ts:addDays for the weekly
 * materializer below.
 */
function addUtcDays(value, delta) {
  const [y, m, d] = value.split("-").map(Number);
  const ms = Date.UTC(y, m - 1, d) + delta * 86400000;
  const dt = new Date(ms);
  const yy = String(dt.getUTCFullYear()).padStart(4, "0");
  const mm = String(dt.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(dt.getUTCDate()).padStart(2, "0");
  return `${yy}-${mm}-${dd}`;
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", `http://${HOST}:${PORT}`);
  const path = url.pathname;

  // --- auth -----------------------------------------------------------------
  if (req.method === "POST" && path === "/auth/signup") {
    const body = await readJson(req);
    const email = typeof body?.email === "string" ? body.email : "";
    const password = typeof body?.password === "string" ? body.password : "";
    if (!email || !password) {
      return detail(res, 422, "Invalid signup payload.");
    }
    if (users.has(email)) {
      return detail(res, 409, "Email already registered.");
    }
    const userId = randomUUID();
    users.set(email, { userId, password });
    return send(res, 201, { user_id: userId, email });
  }

  if (req.method === "POST" && path === "/auth/login") {
    const body = await readJson(req);
    const email = typeof body?.email === "string" ? body.email : "";
    const password = typeof body?.password === "string" ? body.password : "";
    const record = users.get(email);
    if (!record || record.password !== password) {
      return detail(res, 401, "Invalid email or password.");
    }
    const token = `tok-${randomUUID()}`;
    sessions.set(token, record.userId);
    return send(res, 200, { access_token: token, token_type: "bearer" });
  }

  // --- search-summary -------------------------------------------------------
  if (req.method === "POST" && path === "/search-summary") {
    if (!userIdFor(req)) {
      return detail(res, 401, "Not authenticated.");
    }
    await readJson(req); // drain {query}
    return send(res, 200, { summary: SUMMARY, citations: CITATIONS, degraded: [] });
  }

  // --- search (raw hybrid hits, no LLM) -------------------------------------
  // Backs the Phase 37 in-editor LibraryDrawer. Bearer-scoped like the rest;
  // returns the deterministic SEARCH_HITS (already tenant-scoped in the real api
  // — the JWT user's library — so the stub just gates on a valid session).
  if (req.method === "POST" && path === "/search") {
    if (!userIdFor(req)) {
      return detail(res, 401, "Not authenticated.");
    }
    await readJson(req); // drain {query}
    return send(res, 200, { hits: SEARCH_HITS, degraded: [] });
  }

  // --- library --------------------------------------------------------------
  // The editor shell fetches this ONCE on doc open (server-side, bearer) for the
  // owned-book_id set + the {id -> title} map the drawer uses. Bearer-scoped.
  if (req.method === "GET" && path === "/library") {
    if (!userIdFor(req)) {
      return detail(res, 401, "Not authenticated.");
    }
    return send(res, 200, { books: LIBRARY });
  }

  // --- calendar events collection (Phase 39 GET + Phase 40 POST) ------------
  // Same path for the range GET and the create POST (a FastAPI collection
  // route). Bearer-scoped; a row is visible when it is a shared seed
  // (userId === null) OR owned by the requesting user.
  if (path === "/calendar/events") {
    const userId = userIdFor(req);
    if (!userId) {
      return detail(res, 401, "Not authenticated.");
    }

    // GET ?start&end — half-open [start, end) on event_date (an event dated
    // exactly `end` is EXCLUDED), ordered event_date ascending. Range validation
    // (start <= end, span <= 400 days) is the real api's job; the E2E only
    // exercises in-range fetches. String compares are correct because the dates
    // are zero-padded YYYY-MM-DD.
    if (req.method === "GET") {
      const start = url.searchParams.get("start");
      const end = url.searchParams.get("end");
      const events = [];
      for (const [id, record] of calendarEvents) {
        if (record.userId !== null && record.userId !== userId) {
          continue;
        }
        if (start !== null && record.event_date < start) {
          continue;
        }
        if (end !== null && record.event_date >= end) {
          continue;
        }
        events.push(calendarWire(id, record));
      }
      events.sort((a, b) =>
        a.event_date < b.event_date ? -1 : a.event_date > b.event_date ? 1 : 0,
      );
      return send(res, 200, { events });
    }

    // POST create. Validates event_date + title (the proxy already dropped
    // document_id). When repeat_weekly_until is set, MATERIALIZES independent
    // weekly rows from event_date THROUGH that date inclusive (anchor + every +7
    // days <= until), enforcing the real api's caps: until < event_date -> 422,
    // and occurrence_count > MATERIALIZER_CAP_ROWS (53) -> 422. Each occurrence
    // is its own row owned by the creator. Response: 201 { events } (a LIST,
    // event_date ascending) even for a single create.
    if (req.method === "POST") {
      const body = await readJson(req);
      const eventDate = typeof body?.event_date === "string" ? body.event_date : null;
      const title = typeof body?.title === "string" ? body.title : null;
      if (!eventDate || !title) {
        return detail(res, 422, "event_date and title are required.");
      }
      const series = typeof body?.series === "string" ? body.series : null;
      const until = typeof body?.repeat_weekly_until === "string" ? body.repeat_weekly_until : null;

      let occurrences = 1;
      if (until !== null) {
        if (until < eventDate) {
          return detail(res, 422, "repeat_weekly_until must be on or after event_date.");
        }
        // (until - event_date).days // 7 + 1, computed on UTC-midnight ms.
        const [sy, sm, sd] = eventDate.split("-").map(Number);
        const [uy, um, ud] = until.split("-").map(Number);
        const days = Math.floor((Date.UTC(uy, um - 1, ud) - Date.UTC(sy, sm - 1, sd)) / 86400000);
        occurrences = Math.floor(days / 7) + 1;
        if (occurrences > MATERIALIZER_CAP_ROWS) {
          return detail(
            res,
            422,
            `A weekly repeat would create ${occurrences} events, over the ${MATERIALIZER_CAP_ROWS} limit.`,
          );
        }
      }

      const created = [];
      for (let i = 0; i < occurrences; i += 1) {
        const id = randomUUID();
        const now = nextTimestamp();
        const record = {
          userId,
          event_date: addUtcDays(eventDate, i * 7),
          title,
          series,
          document_id: null,
          created_at: now,
          updated_at: now,
        };
        calendarEvents.set(id, record);
        created.push(calendarWire(id, record));
      }
      created.sort((a, b) =>
        a.event_date < b.event_date ? -1 : a.event_date > b.event_date ? 1 : 0,
      );
      return send(res, 201, { events: created });
    }
  }

  // --- single calendar event (Phase 40 PATCH / DELETE) ----------------------
  // PATCH edits (title/series/event_date, present-only); DELETE hard-deletes.
  // A non-owned / unknown / non-UUID id collapses to the SAME uniform 404 (the
  // no-existence-oracle contract). Shared seeds (userId === null) are read-only
  // here — a mutation on one is NOT owned by the caller, so it 404s, matching
  // "the user can only mutate their own rows". The Phase 40 specs create their
  // own rows before editing/deleting.
  const calendarMatch = path.match(/^\/calendar\/events\/([^/]+)$/);
  if (calendarMatch && (req.method === "PATCH" || req.method === "DELETE")) {
    const userId = userIdFor(req);
    if (!userId) {
      return detail(res, 401, "Not authenticated.");
    }
    const id = decodeURIComponent(calendarMatch[1]);
    const record = calendarEvents.get(id);
    const owned = record && record.userId === userId;

    if (req.method === "PATCH") {
      if (!owned) {
        return detail(res, 404, "Event not found.");
      }
      const body = await readJson(req);
      // Forced-500 sentinel (Phase 42 drag rollback test): a PATCH that would
      // move an event ONTO this poison date fails with a server error so the
      // optimistic move is rolled back. Read the body ONCE (above) so this early
      // return and the normal path never both consume the request stream. The
      // body is an OPAQUE 500 (no `{detail}` string) — like a genuinely
      // unhandled API error — so the proxy's errorDetail() falls back and
      // CalendarView surfaces the generic "Could not save the event." banner.
      if (body?.event_date === FORCE_500_DATE) {
        return send(res, 500, { error: "Internal Server Error" });
      }
      const hasDate = typeof body?.event_date === "string";
      const hasTitle = typeof body?.title === "string";
      // series + document_id are three-state: present-and-null detaches,
      // present-and-string re-sets, absent leaves alone (key-presence, NOT
      // truthiness — an explicit null must survive to detach / unlink).
      const hasSeries = body !== null && "series" in body;
      const hasDoc = body !== null && "document_id" in body;
      if (!hasDate && !hasTitle && !hasSeries && !hasDoc) {
        return detail(res, 422, "PATCH must set at least one field.");
      }
      // Phase 38 ownership gate: a NON-NULL document_id must be a doc owned by
      // the caller (active OR soft-deleted — ownership is what matters). A
      // cross-tenant / nonexistent / non-UUID id collapses to the SAME no-oracle
      // 404 (no title/existence leak). A null document_id always passes (unlink).
      if (hasDoc && typeof body.document_id === "string") {
        const doc = documents.get(body.document_id);
        if (!doc || doc.userId !== userId) {
          return detail(res, 404, "Document not found.");
        }
      }
      if (hasDate) {
        record.event_date = body.event_date;
      }
      if (hasTitle) {
        record.title = body.title;
      }
      if (hasSeries) {
        record.series = typeof body.series === "string" ? body.series : null;
      }
      if (hasDoc) {
        record.document_id = typeof body.document_id === "string" ? body.document_id : null;
      }
      record.updated_at = nextTimestamp();
      return send(res, 200, calendarWire(id, record));
    }

    if (req.method === "DELETE") {
      if (!owned) {
        return detail(res, 404, "Event not found.");
      }
      calendarEvents.delete(id);
      res.writeHead(204);
      return res.end();
    }
  }

  // --- upload ---------------------------------------------------------------
  if (req.method === "POST" && path === "/upload") {
    const userId = userIdFor(req);
    if (!userId) {
      return detail(res, 401, "Not authenticated.");
    }
    // Drain the multipart body; we don't parse it — the filename is cosmetic.
    await new Promise((resolve) => {
      req.on("data", () => {});
      req.on("end", resolve);
    });
    const taskId = randomUUID();
    const filename = url.searchParams.get("filename") ?? "book.epub";
    tasks.set(taskId, { userId, filename });
    return send(res, 202, { task_id: taskId, upload_id: randomUUID(), filename });
  }

  // --- tasks/{id} -----------------------------------------------------------
  const taskMatch = path.match(/^\/tasks\/(.+)$/);
  if (req.method === "GET" && taskMatch) {
    const userId = userIdFor(req);
    if (!userId) {
      return detail(res, 401, "Not authenticated.");
    }
    const taskId = decodeURIComponent(taskMatch[1]);
    const record = tasks.get(taskId);
    // Phase 20: non-owned AND unknown collapse to the SAME 404 (no oracle).
    if (!record || record.userId !== userId) {
      return detail(res, 404, "Task not found.");
    }
    return send(res, 200, {
      task_id: taskId,
      status: "SUCCESS",
      result: { book_id: randomUUID(), was_duplicate: false, rows_inserted: 42 },
    });
  }

  // --- documents ------------------------------------------------------------
  // Collection: POST create, GET list (preview-only).
  if (path === "/documents") {
    const userId = userIdFor(req);
    if (!userId) {
      return detail(res, 401, "Not authenticated.");
    }

    if (req.method === "POST") {
      const body = await readJson(req);
      const title = typeof body?.title === "string" ? body.title : null;
      const content = body?.content;
      if (!title || typeof content !== "object" || content === null || Array.isArray(content)) {
        return detail(res, 422, "Invalid document payload.");
      }
      const id = randomUUID();
      const now = nextTimestamp();
      const record = {
        userId,
        title,
        content,
        content_text: deriveContentText(content),
        schema_version: SCHEMA_VERSION,
        created_at: now,
        updated_at: now,
        deleted_at: null,
      };
      documents.set(id, record);
      return send(res, 201, fullDoc(id, record));
    }

    if (req.method === "GET") {
      const items = [];
      for (const [id, record] of documents) {
        if (record.userId === userId && record.deleted_at === null) {
          items.push(summaryDoc(id, record));
        }
      }
      // updated_at DESC, matching api/documents.py list ordering.
      items.sort((a, b) =>
        a.updated_at < b.updated_at ? 1 : a.updated_at > b.updated_at ? -1 : 0,
      );
      return send(res, 200, { documents: items });
    }
  }

  // Single document: GET full, PATCH, DELETE.
  const docMatch = path.match(/^\/documents\/([^/]+)$/);
  if (docMatch) {
    const userId = userIdFor(req);
    if (!userId) {
      return detail(res, 401, "Not authenticated.");
    }
    const id = decodeURIComponent(docMatch[1]);
    const record = documents.get(id);
    // Phase 20/34: non-owned, unknown, AND soft-deleted collapse to ONE 404.
    const visible = record && record.userId === userId && record.deleted_at === null;

    if (req.method === "GET") {
      if (!visible) {
        return detail(res, 404, "Document not found.");
      }
      return send(res, 200, fullDoc(id, record));
    }

    if (req.method === "PATCH") {
      if (!visible) {
        return detail(res, 404, "Document not found.");
      }
      const body = await readJson(req);
      const base = typeof body?.base_updated_at === "string" ? body.base_updated_at : null;
      if (!base) {
        return detail(res, 422, "base_updated_at is required.");
      }
      const hasTitle = typeof body.title === "string";
      const hasContent =
        typeof body.content === "object" && body.content !== null && !Array.isArray(body.content);
      if (!hasTitle && !hasContent) {
        return detail(res, 422, "PATCH must set at least one of title or content.");
      }
      // Optimistic concurrency: a base that doesn't match the stored updated_at
      // means another write landed first -> 409 (the stale-tab editor path).
      if (base !== record.updated_at) {
        return detail(res, 409, "Document was modified since base_updated_at; reload and retry.");
      }
      if (hasTitle) {
        record.title = body.title;
      }
      if (hasContent) {
        record.content = body.content;
        record.content_text = deriveContentText(body.content);
      }
      record.updated_at = nextTimestamp();
      return send(res, 200, fullDoc(id, record));
    }

    if (req.method === "DELETE") {
      if (!visible) {
        return detail(res, 404, "Document not found.");
      }
      record.deleted_at = nextTimestamp();
      // Cross-item contract (Phase 41 Verify / ON DELETE SET NULL): any calendar
      // event still linked to this document loses the link, so the event
      // survives with document_id NULL and its chip reverts to the edit-popover
      // (unlinked) click. The stub nulls it on delete to model the FK SET NULL
      // the Verify checklist asserts.
      for (const ev of calendarEvents.values()) {
        if (ev.document_id === id) {
          ev.document_id = null;
          ev.updated_at = nextTimestamp();
        }
      }
      res.writeHead(204);
      return res.end();
    }
  }

  // Restore a soft-deleted document: POST /documents/{id}/restore. Clears
  // deleted_at and returns the full doc (content intact — restore never touches
  // it). The uniform 404 covers non-owned / unknown / non-UUID; an already-
  // active doc restores idempotently. Mirrors api/documents.py restore.
  const restoreMatch = path.match(/^\/documents\/([^/]+)\/restore$/);
  if (req.method === "POST" && restoreMatch) {
    const userId = userIdFor(req);
    if (!userId) {
      return detail(res, 401, "Not authenticated.");
    }
    const id = decodeURIComponent(restoreMatch[1]);
    const record = documents.get(id);
    // Restore is reachable on a non-deleted row too (idempotent), so ownership
    // is the only gate — but a non-owned / unknown id is still the uniform 404.
    if (!record || record.userId !== userId) {
      return detail(res, 404, "Document not found.");
    }
    if (record.deleted_at !== null) {
      record.deleted_at = null;
      record.updated_at = nextTimestamp();
    }
    return send(res, 200, fullDoc(id, record));
  }

  // --- documents DOCX round-trip (Phase 43) ---------------------------------
  // GET /documents/{id}/export.docx — streams a STUB .docx (not real OOXML; the
  // E2E only asserts the proxy forwards the binary content-type + the sanitized
  // Content-Disposition filename, never that the bytes open in Word). Bearer-
  // scoped with the SAME uniform 404 for non-owned / unknown / soft-deleted ids.
  const exportMatch = path.match(/^\/documents\/([^/]+)\/export\.docx$/);
  if (req.method === "GET" && exportMatch) {
    const userId = userIdFor(req);
    if (!userId) {
      return detail(res, 401, "Not authenticated.");
    }
    const id = decodeURIComponent(exportMatch[1]);
    const record = documents.get(id);
    const visible = record && record.userId === userId && record.deleted_at === null;
    if (!visible) {
      return detail(res, 404, "Document not found.");
    }
    // The API sanitizes the user-controlled title into the filename; the stub
    // mirrors a benign sanitized name (no quote/CR/LF/slash) so the proxy has a
    // realistic Content-Disposition to forward. Real OOXML is not needed.
    const safeTitle = record.title.replace(/[^A-Za-z0-9 _-]/g, "").trim() || "sermon";
    const body = Buffer.from(`PK stub-docx for ${id}`);
    res.writeHead(200, {
      "content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      "content-disposition": `attachment; filename="${safeTitle}.docx"`,
    });
    return res.end(body);
  }

  // POST /documents/{id}/import — multipart .docx. The real API runs the
  // attacker-controlled-upload pipeline (size cap, libmagic docx sniff, /tmp
  // staging, snapshot-first overwrite); the stub just drains the multipart body
  // and OVERWRITES content with a deterministic imported TipTap doc, then bumps
  // updated_at and returns the full document (the editor reloads it as JSON).
  // Bearer-scoped with the SAME uniform 404. A file named exactly
  // "reject.docx" returns a 415 so the E2E can assert the visible-error path
  // (mirrors the API's libmagic 415 on a non-docx upload, surfaced to the user).
  const importMatch = path.match(/^\/documents\/([^/]+)\/import$/);
  if (req.method === "POST" && importMatch) {
    const userId = userIdFor(req);
    if (!userId) {
      return detail(res, 401, "Not authenticated.");
    }
    // Drain the multipart body; capture the filename for the reject sentinel
    // without a full multipart parse (the boundary carries `filename="…"`).
    let raw = "";
    await new Promise((resolve) => {
      req.on("data", (chunk) => {
        // Cap what we buffer — only need the headers' filename token.
        if (raw.length < 4096) {
          raw += chunk.toString("latin1");
        }
      });
      req.on("end", resolve);
    });
    const id = decodeURIComponent(importMatch[1]);
    const record = documents.get(id);
    const visible = record && record.userId === userId && record.deleted_at === null;
    if (!visible) {
      return detail(res, 404, "Document not found.");
    }
    // Reject sentinel: a file the API's libmagic sniff would refuse -> 415.
    if (/filename="[^"]*reject\.docx"/i.test(raw)) {
      return detail(res, 415, "Unsupported file type. Upload a .docx document.");
    }
    // Overwrite with a deterministic imported TipTap doc (the snapshot-first
    // prior-content row is the API's job; the stub models only the visible
    // result the editor reloads).
    const importedContent = {
      type: "doc",
      content: [
        {
          type: "paragraph",
          content: [{ type: "text", text: "Imported from a Word document." }],
        },
      ],
    };
    record.content = importedContent;
    record.content_text = deriveContentText(importedContent);
    record.updated_at = nextTimestamp();
    return send(res, 200, fullDoc(id, record));
  }

  // --- integrations / OAuth vault (Phase 44) --------------------------------
  // GET /integrations — the JWT user's connections, NO token material. POST
  // /integrations/{provider}/authorize — mint a one-shot state and return an
  // authorize_url pointing at the stub consent screen below. DELETE
  // /integrations/{provider} — revoke (delete the row); uniform 404 when the
  // user has no such connection.
  if (req.method === "GET" && path === "/integrations") {
    const userId = userIdFor(req);
    if (!userId) {
      return detail(res, 401, "Not authenticated.");
    }
    const connections = [];
    for (const [key, record] of oauthConnections) {
      if (key.startsWith(`${userId}:`)) {
        connections.push(record);
      }
    }
    return send(res, 200, { connections });
  }

  const authorizeMatch = path.match(/^\/integrations\/([^/]+)\/authorize$/);
  if (req.method === "POST" && authorizeMatch) {
    const userId = userIdFor(req);
    if (!userId) {
      return detail(res, 401, "Not authenticated.");
    }
    const provider = decodeURIComponent(authorizeMatch[1]);
    // The real api 503s when the provider/enc-key is unconfigured and 404s an
    // unknown provider; the web proxy already 404s a non-allow-set provider, so
    // here we only model the happy path for `google`.
    if (provider !== "google") {
      return detail(res, 404, "Unknown provider.");
    }
    // Mint a single-use, account-bound state surrogate. The authorize_url points
    // at the stub consent screen, which 302s back to the WEB callback with this
    // code+state (standing in for Google's top-level redirect).
    const state = randomUUID();
    oauthStates.set(state, { userId, provider });
    const consent = new URL(`http://${HOST}:${PORT}/oauth/consent`);
    consent.searchParams.set("state", state);
    consent.searchParams.set("provider", provider);
    return send(res, 200, { authorize_url: consent.toString() });
  }

  // Stub consent screen — stands in for Google's accounts.google.com top-level
  // redirect. It immediately 302s the BROWSER back to the operator-registered
  // web callback (WEB_ORIGIN) with a deterministic code + the minted state, so
  // the E2E never needs a real Google round-trip.
  if (req.method === "GET" && path === "/oauth/consent") {
    const state = url.searchParams.get("state");
    const provider = url.searchParams.get("provider") ?? "google";
    const callback = new URL(`${WEB_ORIGIN}/api/integrations/${provider}/callback`);
    callback.searchParams.set("code", `stub-code-${randomUUID()}`);
    if (state) {
      callback.searchParams.set("state", state);
    }
    res.writeHead(302, { location: callback.toString() });
    return res.end();
  }

  const callbackMatch = path.match(/^\/integrations\/([^/]+)\/callback$/);
  if (req.method === "GET" && callbackMatch) {
    const userId = userIdFor(req);
    if (!userId) {
      return detail(res, 401, "Not authenticated.");
    }
    const provider = decodeURIComponent(callbackMatch[1]);
    const code = url.searchParams.get("code");
    const state = url.searchParams.get("state");
    if (!code || !state) {
      return detail(res, 400, "Missing code or state.");
    }
    // SINGLE-USE + ACCOUNT-BINDING (the phase's whole point): pop the state and
    // require it was minted for THIS user. A replayed state (already popped) or
    // a cross-user state both 400 — BEFORE any "token exchange". The real api
    // does the HMAC + exp + PKCE + user_id compare; the stub models the
    // observable single-use + binding rejection.
    const minted = oauthStates.get(state);
    oauthStates.delete(state);
    if (!minted || minted.userId !== userId || minted.provider !== provider) {
      return detail(res, 400, "Invalid or expired state.");
    }
    // "Exchange" the code + "fetch userinfo": a deterministic account email
    // derived from the user so reconnect overwrites in place. NO token is ever
    // surfaced — the row stores only the email/scopes/timestamps.
    const now = nextTimestamp();
    const email = `oauth-${userId.slice(0, 8)}@example.com`;
    oauthConnections.set(`${userId}:${provider}`, {
      provider,
      provider_account_email: email,
      scopes: OAUTH_SCOPES,
      connected_at: now,
      token_expiry: null,
    });
    return send(res, 200, { provider, provider_account_email: email });
  }

  const revokeMatch = path.match(/^\/integrations\/([^/]+)$/);
  if (req.method === "DELETE" && revokeMatch) {
    const userId = userIdFor(req);
    if (!userId) {
      return detail(res, 401, "Not authenticated.");
    }
    const provider = decodeURIComponent(revokeMatch[1]);
    const key = `${userId}:${provider}`;
    // No connection for this user/provider collapses to the uniform 404 (no
    // existence oracle — same as a cross-tenant id).
    if (!oauthConnections.has(key)) {
      return detail(res, 404, "Integration not found.");
    }
    oauthConnections.delete(key);
    res.writeHead(204);
    return res.end();
  }

  // Health / fallthrough.
  return send(res, 200, { ok: true });
});

server.listen(PORT, HOST, () => {
  process.stdout.write(`fake-api listening on http://${HOST}:${PORT}\n`);
});
