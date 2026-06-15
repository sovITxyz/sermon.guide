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
//   * /upload        202 {task_id,upload_id,filename}; records token-scoped
//                    ownership (Phase 20)
//   * /tasks/{id}    200 {task_id,status,result} when the bearer OWNS the task;
//                    a UNIFORM 404 {detail} for a non-owned OR unknown id (the
//                    no-existence-oracle contract the upload E2E asserts)
//   * /documents     POST create (201 full doc), GET list (preview-only items,
//                    no `content`), GET/{id} full, PATCH/{id} (200 full / 409 on
//                    stale base_updated_at), DELETE/{id} (204 soft) — all bearer-
//                    scoped with the SAME uniform 404 for non-owned/unknown ids
//                    (the Phase 35 editor smoke create->PATCH->reload + 409 path)
//
// The LIVE/nightly path uses the real api + e2e/support/stub-llm.mjs instead
// (see web/AGENTS.md). Tokens are opaque random strings — no JWT, no secrets.

import { randomUUID } from "node:crypto";
import { createServer } from "node:http";

const PORT = Number(process.env.PORT ?? process.env.FAKE_API_PORT ?? 8081);
const HOST = process.env.HOST ?? "127.0.0.1";

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

const PREVIEW_CHARS = 280;
const SCHEMA_VERSION = 1;

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
      res.writeHead(204);
      return res.end();
    }
  }

  // Health / fallthrough.
  return send(res, 200, { ok: true });
});

server.listen(PORT, HOST, () => {
  process.stdout.write(`fake-api listening on http://${HOST}:${PORT}\n`);
});
