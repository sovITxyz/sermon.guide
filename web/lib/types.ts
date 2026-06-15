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
