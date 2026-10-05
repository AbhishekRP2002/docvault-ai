export interface ProcessingError {
  code: string;
  message: string;
  retryable: boolean;
  remediation: string;
}

export interface ProcessingStage {
  id: string;
  stage: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
}

export interface ProcessingAttempt {
  id: string;
  attempt: number;
  status: string;
  stage: string | null;
  started_at: string;
  finished_at: string | null;
  error: ProcessingError | null;
  stages: ProcessingStage[];
}

export interface JobDiagnostic {
  id: string;
  kind: string;
  resource_id: string;
  status: string;
  stage: string | null;
  attempts: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  next_retry_at: string | null;
  lease_until: string | null;
  queue_state:
    | "queued"
    | "retry_waiting"
    | "enqueued"
    | "running"
    | "complete"
    | "failed"
    | "cancelled";
  retry_eligible: boolean;
  dead_letter: boolean;
  error: ProcessingError | null;
  history: ProcessingAttempt[];
  history_truncated?: boolean;
}

export interface VersionDiagnostics {
  items: JobDiagnostic[];
  total: number;
}

const labels: Record<string, string> = {
  ingest: "Document processing",
  insights: "Document insights",
  parsing: "Parsing",
  conversion: "Conversion",
  chunking: "Chunking",
  persisting: "Saving chunks",
  embedding: "Embedding",
  activation: "Activating version",
  generating: "Generating insights",
  cleanup: "Cleanup",
  queued: "Queued",
  retry_waiting: "Waiting for retry",
  enqueued: "Waiting for a worker",
  running: "Running",
  complete: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
  interrupted: "Interrupted",
};

/** Keep unknown server stages readable without assigning them a known outcome. */
export function processingLabel(value: string | null) {
  if (!value) return "Not recorded";
  return labels[value] ?? value.replaceAll("_", " ");
}

/** Historical or invalid measurements remain unknown rather than appearing as zero. */
export function processingDuration(value: number | null) {
  if (value === null || !Number.isFinite(value) || value < 0)
    return "Not measured";
  if (value < 1000) return `${Math.round(value)} ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(1)} s`;
  return `${(value / 60_000).toFixed(1)} min`;
}

/** Retry controls reflect both the terminal state and the server's eligibility decision. */
export function canRetryProcessing(job: JobDiagnostic) {
  return job.status === "failed" && job.retry_eligible;
}

export function processingTimestamp(value: string | null) {
  if (!value) return "Not recorded";
  const timestamp = new Date(value);
  return Number.isFinite(timestamp.getTime())
    ? timestamp.toLocaleString()
    : "Not recorded";
}
