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
