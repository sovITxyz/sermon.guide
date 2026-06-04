/**
 * Celery task-status → UI phase mapping. Pure + unit-tested so the upload
 * polling UI has a single, predictable source for "what does this state mean".
 * Celery states: PENDING, STARTED, RETRY, SUCCESS, FAILURE, REVOKED.
 */

export type TaskPhase = "pending" | "running" | "done" | "duplicate" | "failed";

/** True once the task will not change state again — stop polling. */
export function isTerminal(status: string): boolean {
  return status === "SUCCESS" || status === "FAILURE" || status === "REVOKED";
}

/**
 * Collapse a Celery status (+ the ingest result payload, when present) into a
 * UI phase. A successful ingest that deduplicated to an existing book is
 * surfaced distinctly so the user understands why no new vectors were created.
 */
export function taskPhase(status: string, result: { was_duplicate: boolean } | null): TaskPhase {
  switch (status) {
    case "SUCCESS":
      return result?.was_duplicate ? "duplicate" : "done";
    case "FAILURE":
    case "REVOKED":
      return "failed";
    case "STARTED":
    case "RETRY":
      return "running";
    default:
      // PENDING and any unknown/queued state.
      return "pending";
  }
}

const PHASE_LABELS: Record<TaskPhase, string> = {
  pending: "Queued…",
  running: "Ingesting…",
  done: "Added to library",
  duplicate: "Already in your library (deduplicated)",
  failed: "Ingestion failed",
};

export function taskLabel(phase: TaskPhase): string {
  return PHASE_LABELS[phase];
}
