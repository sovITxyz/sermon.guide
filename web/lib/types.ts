/**
 * Wire types shared between the route-handler proxy layer and the UI. Field
 * names are snake_case to match the FastAPI JSON payloads verbatim (api/auth.py,
 * api/uploads.py, api/library.py) — no remapping layer to drift out of sync.
 */

export interface LibraryBook {
  book_id: string;
  title: string;
  author: string | null;
  category: string | null;
  added_at: string;
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
