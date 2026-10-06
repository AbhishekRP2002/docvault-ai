# Health and processing diagnostics

## Load this revision

The additive migration `4642d4fae103` was generated with `alembic revision --autogenerate`. It adds nullable failure classifications and per-attempt stage measurements; existing job/attempt records retain their values. It does not backfill inferred timings.

The subsequent generated migration `b91f270f1115` adds committed workspace revisions and transaction-owned triggers. Apply the current migration head before restarting services; an old database cannot serve the new WebSocket revision contract.

For the Docker workspace, stop any manually managed API/frontend processes that occupy the documented ports, then run `./start_up.sh`. The wrapper builds the current images, applies migrations and starts the services with health checks. It preserves workspace volumes. For local development, apply `uv run alembic upgrade head` before restarting the API, worker and dispatcher. Do not mix old and new workers during the restart.

## Health checks

- `GET /health/live` checks only whether the API process responds; dependency failures do not change liveness.
- `GET /health/ready` returns 200 only with the expected repository migration heads, a reachable database, the pgvector extension and valid HNSW/GIN indexes, usable storage, Redis, at least one fresh idle/busy RQ worker, and a dispatcher with a fresh successful cycle. Otherwise it returns 503 with independent component results and safe error codes.
- `GET /v1/diagnostics/system` returns a best-effort 200 snapshot even when degraded. It exposes dependency latency, per-worker identities/state/heartbeats, dispatcher cycle/success times, SQL backlog/dead-letter counts, RQ failed deliveries, storage capacity, parser configuration and recorded provider failures. Provider configuration and recent observations do not verify a key, provider reachability, model support or parser asset readiness. No model call or asset download occurs.
- Worker/dispatcher Compose probes check the container's own PID and exact runtime identity. A healthy replica cannot satisfy another replica's probe. Dispatcher clean shutdown removes its own observation; expiry covers abrupt termination. An idle worker or a busy worker can be healthy. Heartbeat freshness indicates observed activity, not guaranteed useful progress.

Database connections are reused through a bounded SQLAlchemy pool. New sessions borrow connections and return them after their transaction. Inference and parsing use short separate transactions rather than occupying one connection across the whole operation. A process guard replaces inherited connections after an RQ fork. The pool and connection/query/lock waits use defaults in `src/docvault/config.py`; probes use separate connections with shorter waits. Existing database URL options such as `search_path` are retained. Migration commands use their own engine rather than the application statement timeout.

Defaults per process: pool size 10 plus five overflow connections, connection timeout three seconds, pool wait five seconds, statement timeout 30 seconds, lock timeout five seconds. Health database connection/query limits are two seconds/one second; Redis probes have one-second connect/operation limits without retries. Heartbeat freshness defaults to 120 seconds. RQ dequeue/monitor intervals adapt to this window. `STORAGE_MIN_FREE_BYTES=0` disables the optional free-space threshold; storage access is still checked. These are component limits, not a fixed end-to-end deadline across all observations. Native RQ metadata inspection preserves the Redis probe timeout instead of inheriting RQ's long dequeue timeout.

## Processing details and dead letters

Open **Files → document details → Versions → Processing details** on the required version. The panel loads only when opened and has manual refresh. It shows separate ingestion/insight jobs, original attempt IDs, measured conversion/chunking/persistence/embedding/activation/generation stages, outcomes, errors, remediation and retry timing. Old attempts show no recorded stage measurements. A queued retry is not a completed document.

Endpoints:

| Endpoint | Behavior |
|---|---|
| `GET /v1/versions/{id}/diagnostics?limit=20&offset=0` | Live version's ingestion/insight jobs, newest first; limit 1–50. |
| `GET /v1/jobs/{id}/diagnostics` | Retained job with its newest 100 attempts and stage measurements; `history_truncated` signals older omitted attempts. |
| `GET /v1/diagnostics/dead-letters?limit=30&offset=0` | Retained terminal failed jobs; limit 1–100. List bodies omit attempt history. |
| `POST /v1/jobs/{id}/retry` | Manual replay through the existing SQL claim/dispatcher path; 202 means queued, 409 means the job is no longer failed, 404 means the job/source is unavailable. |

PostgreSQL owns the dead-letter workflow: retryable errors get the existing maximum of three automatic attempts with backoff; exhausted retries and non-retryable errors remain failed until explicit replay. Cancellations are excluded. Failed evidence may remain after source deletion, but replay eligibility is false. Replay locks documents before the job, validates all sources and fences previous runners; concurrent replays accept one state transition. Attempt counters restart for a manual retry while the old attempt IDs/history remain retained.

RQ `FailedJobRegistry` is a separate transport observation. Application failures caught by our runner can finish their RQ delivery successfully while becoming SQL dead letters. Do not directly requeue those delivery IDs; that would bypass the canonical retry transition. System diagnostics inspect the native registry with cleanup disabled; Redis outages leave its count unknown.

Failure descriptions use stable safe codes rather than raw parser/database exception payloads. Expected application/provider messages remain actionable. Recovery closes still-running stages as interrupted; cancellation closes them as cancelled. Old unclassified errors are labeled historical rather than assigned an invented cause.

## Monitoring semantics

`GET /v1/metrics/processing?days=30` retains lifetime completed/failed/active/queued counts and completed-job average/p50/p95 durations. Duration sample counts are explicit. It adds current due backlog, retry waiting, enqueued jobs, expired leases, oldest due age and retained failed-job count. Automatic retry attempts (`attempt > 1`) and completed-stage sample counts/p50/p95 cover the trailing UTC interval, days 1–90. Manual replay's first attempt is not counted as an automatic retry. Missing, incomplete, negative and future measurements are excluded. Empty durations are null. Redis observation failures yield unknown consumer counts instead of zero.

`GET /metrics` exports numeric samples and allowlisted stage labels only, without source/job/chat IDs or diagnostic strings. API latency histograms/p95, cost reconciliation and broader load/quality acceptance remain separate work; this revision does not claim those gates complete.

```sh
curl -i http://127.0.0.1:8000/health/ready
curl http://127.0.0.1:8000/v1/diagnostics/system
curl 'http://127.0.0.1:8000/v1/diagnostics/dead-letters?limit=30'
curl 'http://127.0.0.1:8000/v1/metrics/processing?days=30'
curl http://127.0.0.1:8000/v1/jobs/JOB_ID/diagnostics
curl -X POST http://127.0.0.1:8000/v1/jobs/JOB_ID/retry
```

Before replay, resolve the indicated dependency/storage/parser/provider issue. Upload a new source or request a new artifact when the original source/model configuration cannot be reused. There is no automatic replay of terminal failures or bulk replay control.

## Realtime reconciliation and verification

`/v1/events` carries `{type: "snapshot" | "update", revision: string}`. The revision is an opaque database epoch/count. SQL triggers record observable changes in the same transaction. Rollback has no effect; late commits still change the count. Redis failure does not lose changes: periodic snapshots query SQL directly. A database read failure closes the channel with 1013 so clients reconnect rather than advertise a current snapshot. Clients ignore unchanged revisions, reconcile once on reconnect, refresh message/version/insight caches, and preserve active local SSE text until finalization. Processing details retain manual refresh.

Revision rows currently accumulate one per changing transaction. Request metrics and heartbeat-only leases are excluded. Snapshot counting cost grows with this ledger; a production retention/compaction policy is deferred. Do not delete revision rows without rotating the epoch, because an unchanged count could mask a mutation.

The implementation uses installed RQ 2.12.0 native [worker metadata](https://python-rq.org/docs/workers/#retrieving-worker-information) and [failed-job registry](https://python-rq.org/docs/exceptions/), plus SQLAlchemy 2.1's [pooling and fork guard](https://docs.sqlalchemy.org/en/21/core/pooling.html#using-connection-pools-with-multiprocessing-or-os-fork). Actual verification and remaining browser/platform gates are recorded in [progress.md](progress.md).

## Document assistant startup and indexing

Startup applies the generated migration chain before API/worker/dispatcher start: `46ba1f6a7865` adds empty-default `messages.agent_trace` and a separate `document_overviews` table with HNSW/GIN indexes; `2f1465dc7863` adds persisted `chats.context_summary` for conversation-memory checkpoints. Original source chunks remain unchanged. Successful automatic insights enqueue `overview_index`; failures are visible through the existing version diagnostics/dead-letter/replay endpoints and do not mark ready source documents failed.

Bounded overview reads work independently of discovery indexing; missing insights yield a labeled source sample. Agent tools cannot traverse raw files or submit analysis jobs. Files/API summaries and comparisons remain available.

Configure both native tool turns and final answers with `OPENROUTER_CHAT_MODEL`. Token and output settings default in `src/docvault/llm/config.py`; they are omitted from the simplified environment sample. One `CHAT_SYSTEM_PROMPT` governs the entire loop; `AGENT_MAX_TOOL_ROUNDS` remains the round bound (default 20). The obsolete separate selector/rewrite model settings are ignored. Configure memory summarization independently through `OPENROUTER_CONVERSATION_SUMMARY_MODEL`; its token settings also default in code. Existing `GENERATION_TIMEOUT_SECONDS` bounds the entire tool workflow. All selection/generation/embedding/history-summary/reference-repair usage is recorded; traces retain IDs/statuses/referents rather than raw source passages. PostgreSQL remains history authority; interrupted turns are retryable, without a promise of durable mid-turn resume. History is not cut to the last 20 turns: before a new turn, at an estimated 85% of available chat context, using model metadata with configured fallback, older complete turns are summarized into persisted `Chat.context_summary`. Original messages remain intact. Memory is not document evidence. Generation calls are not rejected by a local configured-token preflight.

The tool catalog and strict inputs are in `tools/definitions.py` and `tools/models.py`; handlers/indexing are in `tools/runtime.py` and `tools/discovery.py`. Safe intermediate receipts persist in message traces and stream as `tool.updated`, rendered with assistant-ui `ToolFallback`. Public citations retain full original geometry; model-facing provenance excludes repeated rendering spans. Citation repair is bounded and reference-only, not a factuality guarantee. Candidate live results remain separate from the historical QA pilot and are reported in the progress tracker.
