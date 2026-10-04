import type { VaultDocument } from "./types";

export const fileColumns = [
  {
    id: "started",
    label: "Started at",
    width: 168,
    description:
      "Start of the latest ingestion attempt, in your local timezone.",
  },
  {
    id: "duration",
    label: "Processing time",
    width: 124,
    description:
      "Latest ingestion attempt only. Excludes upload, queue wait, retry backoff, and insight generation.",
  },
  {
    id: "run",
    label: "Run ID",
    width: 154,
    description:
      "Ingestion job ID for this version. Stable across retries. Click to copy the full ID.",
  },
  {
    id: "pages",
    label: "Pages",
    width: 72,
    description:
      "Page count recorded by the parser. A dash means it is not available.",
  },
  {
    id: "chunks",
    label: "Chunks",
    width: 80,
    description: "Chunks recorded for the latest document version.",
  },
  {
    id: "size",
    label: "Size",
    width: 90,
    description: "Size of the uploaded source file.",
  },
  {
    id: "added",
    label: "Added at",
    width: 168,
    description: "When the document was first added to the workspace.",
  },
  {
    id: "finished",
    label: "Finished at",
    width: 168,
    description:
      "When the latest attempt ended, successfully or with an error.",
  },
  {
    id: "attempts",
    label: "Attempts",
    width: 90,
    description:
      "Attempts in the current retry cycle. Resets after a manual retry.",
  },
  {
    id: "tokens",
    label: "Content tokens",
    width: 120,
    description:
      "Tokens in the parsed document chunks, not billed model usage.",
  },
  {
    id: "parser",
    label: "Parser",
    width: 132,
    description: "Parser recorded for this document version.",
  },
  {
    id: "embedding",
    label: "Embedding model",
    width: 220,
    description: "Embedding model recorded for this document version.",
  },
] as const;

export type FileColumn = (typeof fileColumns)[number]["id"];
export const defaultFileColumns: FileColumn[] = [
  "started",
  "duration",
  "run",
  "pages",
  "chunks",
  "size",
];
export const fileColumnsStorageKey = "docvault.file-columns.v1";

export function parseFileColumns(value: string | null): FileColumn[] {
  if (value === null) return [...defaultFileColumns];
  try {
    const parsed: unknown = JSON.parse(value);
    if (!Array.isArray(parsed)) return [...defaultFileColumns];
    return fileColumns
      .filter((column) => parsed.includes(column.id))
      .map((column) => column.id);
  } catch {
    return [...defaultFileColumns];
  }
}

export function processingDuration(milliseconds: number | null | undefined) {
  if (
    milliseconds == null ||
    !Number.isFinite(milliseconds) ||
    milliseconds < 0
  )
    return "—";
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  if (milliseconds < 60_000) return `${(milliseconds / 1000).toFixed(1)} s`;
  const seconds = Math.floor(milliseconds / 1000);
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

export function documentTimestamp(value: string | null | undefined) {
  if (!value || !Number.isFinite(Date.parse(value))) return "—";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

/** A queued/failed parse has not necessarily produced output; do not imply an observed zero. */
export function documentCount(
  document: VaultDocument,
  field: "chunk_count" | "token_count",
) {
  const value = document[field];
  if (value == null) return "—";
  if (
    value === 0 &&
    !document.parser &&
    !["embedding", "ready"].includes(document.status)
  )
    return "—";
  return new Intl.NumberFormat().format(value);
}
