// Deterministic OpenAI-compatible chat-completions stub for the LIVE/nightly
// E2E path (Phase 25 pre-made decision 2).
//
// This is the "local mock provider endpoint" the spec calls for: the REAL api
// boots with SERMON_API_LLM_PROVIDER=<row> + SERMON_API_LLM_BASE_URL pointed
// here, so /search-summary runs real retrieval (Postgres + Milvus + a seeded
// corpus) and only the LLM round-trip is short-circuited — no DeepInfra call,
// no ~134s wait, fully deterministic.
//
// The api hands the model a user-turn prompt whose context passages each begin
// with their `[book:chunk]` citation marker (api/summary.py:_build_prompt). We
// echo those exact markers back inside a grounded sentence so the api's
// _extract_citations resolves them and the browser renders one chip per source.
// We never invent a marker (that would resolve to nothing and prove nothing).
//
// Usage (live path, documented in web/AGENTS.md):
//   node e2e/support/stub-llm.mjs            # binds 127.0.0.1:8099
//   PORT=9000 node e2e/support/stub-llm.mjs  # custom port
// then boot the api with:
//   SERMON_API_LLM_BASE_URL=http://127.0.0.1:8099/v1
//   SERMON_API_LLM_PROVIDER=deepinfra DEEPINFRA_API_KEY=stub-key-ignored
//
// No secrets, no network egress, no real model.

import { createServer } from "node:http";

const PORT = Number(process.env.PORT ?? process.env.STUB_LLM_PORT ?? 8099);
const HOST = process.env.HOST ?? "127.0.0.1";

// Every `[book:chunk]` marker, in first-appearance order. Markers are bracket-
// delimited and contain no nested bracket (api strips `[`/`]`/`:` from labels),
// so a flat group match is exact.
const MARKER_RE = /\[[^[\]]*\]/g;

function markersFrom(prompt) {
  const seen = new Set();
  const ordered = [];
  for (const match of prompt.matchAll(MARKER_RE)) {
    const marker = match[0];
    if (!seen.has(marker)) {
      seen.add(marker);
      ordered.push(marker);
    }
  }
  return ordered;
}

function groundedSummary(prompt) {
  const markers = markersFrom(prompt);
  if (markers.length === 0) {
    // No context markers in the prompt — say so plainly (the api's deterministic
    // no-context guard normally fires before us, so this is belt-and-suspenders).
    return "The provided passages do not address that question.";
  }
  // A short grounded paragraph citing each source inline, immediately after the
  // clause it supports — the exact shape the system instruction asks for.
  const clauses = markers.map(
    (marker, i) =>
      `${i === 0 ? "The sources speak directly to this" : "and they reinforce it"} ${marker}`,
  );
  return `${clauses.join(", ")}. Taken together the passages form a consistent answer.`;
}

const server = createServer((req, res) => {
  if (req.method === "POST" && req.url && req.url.endsWith("/chat/completions")) {
    let raw = "";
    req.on("data", (chunk) => {
      raw += chunk;
    });
    req.on("end", () => {
      let userPrompt = "";
      try {
        const body = JSON.parse(raw);
        const messages = Array.isArray(body?.messages) ? body.messages : [];
        userPrompt = messages
          .filter((m) => m?.role === "user")
          .map((m) => (typeof m?.content === "string" ? m.content : ""))
          .join("\n");
      } catch {
        // Malformed body → empty prompt → no-context summary below.
      }
      const completion = {
        id: "stub-cmpl-0",
        object: "chat.completion",
        created: Math.floor(Date.now() / 1000),
        model: "stub-grounded",
        choices: [
          {
            index: 0,
            message: { role: "assistant", content: groundedSummary(userPrompt) },
            finish_reason: "stop",
          },
        ],
        usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
      };
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(completion));
    });
    return;
  }
  // Health probe / anything else.
  res.writeHead(200, { "content-type": "application/json" });
  res.end(JSON.stringify({ ok: true }));
});

server.listen(PORT, HOST, () => {
  process.stdout.write(`stub-llm listening on http://${HOST}:${PORT}\n`);
});
