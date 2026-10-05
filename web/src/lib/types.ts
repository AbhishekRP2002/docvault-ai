export type DocumentStatus =
  | "queued"
  | "parsing"
  | "chunking"
  | "embedding"
  | "ready"
  | "failed";
export interface DocumentProcessing {
  run_id: string;
  status: string;
  attempts: number;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
}
export interface VaultDocument {
  id: string;
  title: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  created_at: string;
  updated_at: string;
  current_version_id: string | null;
  latest_version_id: string;
  status: DocumentStatus;
  version_number: number;
  page_count: number | null;
  chunk_count: number;
  token_count?: number;
  parser?: string | null;
  embedding_model?: string | null;
  processing?: DocumentProcessing | null;
  insight_status: string;
  summary: string | null;
  category: string | null;
  tags: string[];
  error: string | null;
}
export interface DocumentVersion {
  id: string;
  version_number: number;
  status: DocumentStatus;
  filename: string;
  created_at: string;
  error: string | null;
  insight_status: string;
}
export interface Chat {
  id: string;
  title: string;
  version_ids: string[];
  created_at: string;
  updated_at: string;
}
export interface Citation {
  citation_id: string;
  document_id: string;
  version_id: string;
  chunk_id: string;
  filename: string;
  version_number: number;
  location: {
    page?: number;
    section?: string;
    paragraph?: number;
    line?: number;
  };
  quote: string;
}
export interface Message {
  id: string;
  chat_id: string;
  role: "user" | "assistant";
  status: "pending" | "streaming" | "complete" | "failed" | "cancelled";
  content: string;
  suggestions: string[];
  citations: Citation[];
  error: string | null;
  created_at: string;
  parent_id: string | null;
}
export interface Insights {
  summary: string;
  category: string;
  tags: string[];
  key_insights: { text: string; citation_ids: string[] }[];
  suggestions: string[];
}
export interface Artifact {
  id: string;
  status: string;
  data: Record<string, unknown> | null;
  error: string | null;
}
export interface ComparisonSource {
  version_id: string;
  title: string;
  filename: string | null;
  version_number: number | null;
  available: boolean;
}
export interface ComparisonRun {
  id: string;
  status: string;
  created_at: string;
  version_ids: string[];
  dimensions: string[];
  sources: ComparisonSource[];
  error: string | null;
}
export interface ComparisonHistory {
  items: ComparisonRun[];
  total: number;
}
export interface ProviderConfig {
  provider: string;
  configured: boolean;
  chat_model: string;
  embedding_model: string;
}
export interface DocumentMetrics {
  documents: number;
  versions: number;
  ready: number;
  processing: number;
  failed: number;
  storage_bytes: number;
}
export interface ProcessingMetrics {
  completed: number;
  failed: number;
  active: number;
  queued: number;
  average_duration_ms: number | null;
  p95_duration_ms: number | null;
}
export interface UsageMetrics {
  input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
  unknown_cost_calls: number;
  requests: number;
  cache_hits: number;
}
