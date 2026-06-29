/**
 * Wire types shared between the route-handler proxy layer and the UI. Field
 * names are snake_case to match the FastAPI JSON payloads verbatim (api/auth.py,
 * api/uploads.py, api/library.py, api/reader.py) — no remapping layer to drift
 * out of sync.
 */

export interface LibraryBook {
  book_id: string;
  title: string;
  author: string | null;
  category: string | null;
  added_at: string;
  // Phase 32 progress fields (api/library.py): all null when no saved
  // position; chunk_count is also null when the book has no chunks.
  // progress = (last_chunk_index + 1) / chunk_count, clamped to 1.0.
  chunk_count: number | null;
  last_chunk_index: number | null;
  progress: number | null;
}

export interface LibraryResponse {
  books: LibraryBook[];
}

/**
 * The minimal library projection the editor shell hands the client (Phase 37):
 * just `book_id` + `title`. A raw POST /search hit carries NO title, so the
 * in-editor LibraryDrawer sources `bookTitle` from this map when caching a
 * citation at insert; the owned-`book_id` set for the degraded badge is derived
 * from the same list. `LibraryBook` itself does not cross the RSC boundary — the
 * page projects it to this small shape (plain JSON) before passing it down.
 */
export interface LibraryBookRef {
  book_id: string;
  title: string;
}

export interface SummaryCitation {
  marker: string;
  book_id: string;
  title: string;
  chunk_index: number;
  content: string;
  filename: string | null;
  parent_section: string | null;
}

export interface SummaryResponse {
  summary: string;
  citations: SummaryCitation[];
}

/**
 * POST /search body (api/search.py SearchRequest, extra="forbid"). The search
 * proxy forwards ONLY `query` — `limit`/`rerank` stay at the API defaults so a
 * client cannot widen the retrieval fan-out or flip off the rerank/highlight
 * pipeline through this proxy. A smuggled `user_id`/`book_ids` never reaches
 * the API's 422 because the proxy drops it before serializing the body.
 */
export interface SearchRequest {
  query: string;
}

/**
 * One raw hybrid-retrieval hit (api/search.py SearchHit). Field names/casing
 * match the FastAPI JSON verbatim. `book_id` is a UUID serialized as a string.
 * `metadata` is the chunk metadata written by the worker (worker/ingest.py):
 * `chunk_index` is always present; `filename`/`parent_section` are often null.
 * There is NO top-level title and no `snippet`/`content` field — the citation
 * node maps `content_chunk` -> snippet and `metadata.chunk_index` -> chunkIndex,
 * and sources `bookTitle` from the one-shot /library fetch (raw hits carry no
 * title). Extra `metadata` keys (`rrf_score`, `sentences_kept`, …) are ignored.
 */
export interface SearchHit {
  book_id: string;
  content_chunk: string;
  metadata: {
    filename: string | null;
    chunk_index: number;
    parent_section: string | null;
  };
  score: number;
}

/**
 * POST /search response (api/search.py SearchResponse). `hits` is the final
 * ranked list (raw — no LLM summary). `degraded` (Phase 22) names any pipeline
 * stage that failed and was bypassed ("dense"/"sparse"/"rerank"/"highlight");
 * always present, `[]` on a fully-healthy search.
 */
export interface SearchResponse {
  hits: SearchHit[];
  degraded: string[];
}

export interface UploadAccepted {
  task_id: string;
  upload_id: string;
  filename: string;
}

export interface IngestResult {
  book_id: string;
  was_duplicate: boolean;
  rows_inserted: number;
}

export interface TaskStatus {
  task_id: string;
  status: string;
  result: IngestResult | null;
}

/** One chunk of book text (markdown) in a reader window (api/reader.py). */
export interface ChunkItem {
  chunk_index: number;
  content: string;
}

/**
 * GET /books/{book_id}/chunks — an OBJECT wrapping the window, not a bare
 * list. `chunks` is chunk_index-ascending; a `start` past the end of the
 * book is 200 with an empty array, not an error.
 */
export interface ChunkWindowResponse {
  book_id: string;
  chunks: ChunkItem[];
}

/**
 * GET/PUT /books/{book_id}/position — saved reading position. All three
 * nullable fields are null when no position has been saved yet (200, never
 * 404; 404 is reserved for the ownership gate).
 */
export interface PositionResponse {
  book_id: string;
  chunk_index: number | null;
  offset_ratio: number | null;
  updated_at: string | null;
}

/**
 * PUT /books/{book_id}/position body. Full-replace semantics: an omitted
 * `offset_ratio` clears the stored value to NULL — this is not a patch.
 */
export interface PositionUpdate {
  chunk_index: number;
  offset_ratio?: number | null;
}

/**
 * A ProseMirror/TipTap JSON node tree (the sermon `content`). The API stores
 * it as JSONB and types it `dict[str, object]` (an arbitrary JSON object) —
 * the editor owns the internal shape, the proxy only checks it is a non-null
 * object. Typed as an open record of JSON values rather than a fixed node
 * schema so the proxy/types never drift against TipTap's StarterKit output.
 */
export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };
export type ProseMirrorDoc = { [key: string]: JsonValue };

/**
 * GET /documents list item (api/documents.py DocumentSummary). Preview-only:
 * the first PREVIEW_CHARS (280) of the server-derived `content_text`, never
 * the full `content` JSON. `preview` renders as PLAIN TEXT in the UI — never
 * dangerouslySetInnerHTML.
 */
export interface DocumentListItem {
  document_id: string;
  title: string;
  preview: string;
  schema_version: number;
  created_at: string;
  updated_at: string;
}

/** GET /documents wrapper (api/documents.py DocumentListResponse). */
export interface DocumentListResponse {
  documents: DocumentListItem[];
}

/**
 * Full document (api/documents.py DocumentResponse) — returned by POST create,
 * GET /documents/{id}, and PATCH. Includes the full `content` node tree plus
 * the server-derived `content_text`. `content_text` and `schema_version` are
 * server-owned and never sent back on a write.
 */
export interface DocumentFull {
  document_id: string;
  title: string;
  content: ProseMirrorDoc;
  content_text: string;
  schema_version: number;
  created_at: string;
  updated_at: string;
}

/**
 * POST /documents body (api/documents.py DocumentCreate, extra="forbid"). The
 * create proxy forwards ONLY these two fields — `content_text`/`schema_version`
 * are server-derived/-managed and a smuggled one is dropped here before it can
 * reach the API's 422.
 */
export interface DocumentCreate {
  title: string;
  content: ProseMirrorDoc;
}

/**
 * PATCH /documents/{id} body (api/documents.py DocumentUpdate, extra="forbid").
 * `base_updated_at` (the optimistic-concurrency token) is REQUIRED; at least
 * one of `title`/`content` must be present (the API's 422 owns that rule). The
 * patch proxy forwards ONLY these three fields.
 */
export interface DocumentPatch {
  base_updated_at: string;
  title?: string;
  content?: ProseMirrorDoc;
}

/**
 * One sermon calendar event (api/calendar_routes.py CalendarEvent). Field
 * names/casing match the FastAPI JSON verbatim. `event_date` is a Postgres
 * DATE serialized day-only as `YYYY-MM-DD` (NO time/timezone) — keep it a
 * string end-to-end and do string arithmetic (web/lib/dates.ts); never
 * `new Date("YYYY-MM-DD")` (UTC parse shifts the day in UTC-minus zones).
 * `created_at`/`updated_at` are full ISO datetimes. `series` and `document_id`
 * are nullable (`document_id` FK ON DELETE SET NULL → null when the linked
 * sermon is gone). There is NO `user_id` — the response is tenant-scoped
 * server-side via the JWT.
 */
export interface CalendarEvent {
  event_id: string;
  event_date: string;
  title: string;
  series: string | null;
  document_id: string | null;
  created_at: string;
  updated_at: string;
}

/**
 * GET /calendar/events response (api/calendar_routes.py
 * CalendarEventListResponse). Just `events`, ordered by `event_date`
 * ascending — no pagination/total. POST /calendar/events also returns this
 * shape (a LIST even for a single create — a weekly-repeat materializes many
 * independent rows), so the create proxy/client must read `data.events`.
 */
export interface CalendarEventListResponse {
  events: CalendarEvent[];
}

/**
 * POST /calendar/events body (api/calendar_routes.py CalendarEventCreate,
 * extra="forbid"). The create proxy forwards `event_date` and `title` (required),
 * plus the optional/nullable `series`, `repeat_weekly_until`, and `document_id`.
 * `document_id` (Phase 47, schedule-from-sermon) lets the editor create an event
 * already linked to a sermon in ONE POST — the sermon doc always exists by the
 * time the editor opens, so the calendar-first POST-then-PATCH two-step is
 * unnecessary. The API (Phase 38) ownership-checks a non-null `document_id`
 * against the JWT user's documents and returns a no-oracle 404 on a
 * cross-tenant/nonexistent id; the proxy forwards it verbatim and never
 * pre-validates. `series: null` is a meaningful value (no series), distinct from
 * omitting the field. Length/range/cap validation (title 1..512, repeat cap,
 * until >= event_date) is the API's 422 to own.
 */
export interface CalendarEventCreate {
  event_date: string;
  title: string;
  series?: string | null;
  repeat_weekly_until?: string | null;
  document_id?: string | null;
}

/**
 * PATCH /calendar/events/{id} body (api/calendar_routes.py CalendarEventUpdate,
 * extra="forbid"). All fields are optional; the API's 422 owns the
 * at-least-one-of rule and length checks. Three-state `series` and
 * `document_id` are preserved: present-and-null DETACHES (series cleared /
 * sermon unlinked), present-and-non-null re-links, absent leaves it alone — so
 * each is `string | null` when present, omitted when absent. The API
 * distinguishes the three states via `model_fields_set`; for `document_id` the
 * API also ownership-checks a non-null value against the JWT user's documents
 * (Phase 38) and returns a no-oracle 404 on a cross-tenant/nonexistent id — the
 * proxy never does that check, it just forwards the value verbatim.
 * `repeat_weekly_until` stays DELIBERATELY EXCLUDED: it is NOT a PATCH field on
 * the API (materialized rows are independent) — forwarding it would trip the
 * API's `extra="forbid"` 422. The patch proxy drops it.
 */
export interface CalendarEventPatch {
  event_date?: string;
  title?: string;
  series?: string | null;
  document_id?: string | null;
}

/**
 * One OAuth connection (api/integrations.py — GET /integrations list item).
 * Field names/casing match the FastAPI JSON verbatim. This is the ONLY
 * connection projection that ever crosses the wire: it carries `provider`, the
 * `provider_account_email` fetched from the provider's userinfo endpoint (the
 * single token-derived value the API ever returns), the granted `scopes`, and
 * two timestamps. There are NO token fields — neither the refresh nor the
 * access token, nor any ciphertext, ever leaves the API. `connected_at` is the
 * row's `created_at`; `token_expiry` is the stored access token's expiry (null
 * when none is stored). There is NO `user_id` — the response is tenant-scoped
 * server-side via the JWT.
 */
export interface IntegrationConnection {
  provider: string;
  provider_account_email: string;
  scopes: string;
  connected_at: string;
  token_expiry: string | null;
}

/** GET /integrations response (api/integrations.py list wrapper). */
export interface IntegrationsResponse {
  connections: IntegrationConnection[];
}

/**
 * The external-editor link state (Phase 45, B4) — the only editor-link payload
 * that crosses the wire to the browser. Field names/casing match the FastAPI
 * JSON verbatim (api/editor_links.py). `state` is the link lifecycle
 * (`linked` | `error` | `unlinked`); the editor is HARD read-only while
 * `linked`. `web_url` is the Drive `webViewLink` — the ONLY external string ever
 * shown, opened with `rel="noopener noreferrer"`; NO token, file id, or other
 * provider material ever reaches the browser. `remote_changed` is true when the
 * native Doc has edits not yet pulled (status compares the stored cursor, which
 * never crosses the wire). There is NO `provider_file_id` here — the API owns
 * the file id as a server-side capability fetched from the user's own row; the
 * client never sees it and never forwards one as authoritative.
 */
export type EditorLinkState = "linked" | "error" | "unlinked";

export interface EditorLinkStatus {
  state: EditorLinkState;
  web_url: string | null;
  remote_changed: boolean;
}

/**
 * POST /documents/{id}/editor-link/unlink body (api/editor_links.py
 * UnlinkRequest, extra="forbid"). The settled mandatory user choice: `mode`
 * is REQUIRED and exactly one of `pull-final` (run the pull pipeline once —
 * snapshot + overwrite — THEN unlink) or `keep-app` (leave the app content
 * untouched, just unlink). The unlink proxy whitelists ONLY this field; a
 * smuggled key never reaches the API's `extra="forbid"` gate.
 */
export type UnlinkMode = "pull-final" | "keep-app";

export interface UnlinkRequest {
  mode: UnlinkMode;
}

/**
 * POST /integrations/{provider}/authorize response (api/integrations.py). The
 * API mints the state HMAC + PKCE challenge and stores the verifier server-side
 * (Redis), then returns the provider auth URL the browser is sent to. No token
 * material is involved — this is the kickoff before any consent.
 */
export interface AuthorizeResponse {
  authorize_url: string;
}
