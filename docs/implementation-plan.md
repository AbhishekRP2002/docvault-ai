# DocVault AI implementation plan

Status: living implementation plan. Updated: 4 October 2026. Implementation exists; remaining acceptance gates are tracked explicitly below and in progress.md.

Build against [spec.md](spec.md). The approved direction is FastAPI, PostgreSQL/pgvector, RQ/Redis, LangGraph workflows, OpenRouter through the OpenAI Python SDK, and React/Vite/assistant-ui/shadcn. The service is one shared localhost workspace with IAM deferred. Research-backed decisions and alternatives are in [research.md](research.md). Preserve parallel implementation work; the file map below describes responsibilities rather than a claim that every listed file exists.

The first reviewable release completes M0-M3: upload, durable ingestion, citations, multi-turn history, and basic monitoring. M4-M7 complete all 18 bonus items and the submission. Keep every milestone runnable, add the checks that protect its behavior, and document actual outcomes. A rough planning allowance is 3-5 focused working days for the core and 8-12 total for the full bonus scope for one developer, depending on OCR/model integration and deployment. These are estimates pending the user's deadline.

## 1. Living status and repository layout

The feature-by-feature implementation ledger is [progress.md](progress.md). It is the current source for implemented, verified, pending, and deferred work; this plan retains the complete acceptance scope.

| Workstream | Status | Evidence / remaining gate |
|---|---|---|
| Specification and research | Implemented | Approved LangGraph/RQ/OpenRouter decisions and all 18 bonuses retained. UI contract updated to the user's two-column reference. |
| M0 provider/parser feasibility | Partially verified | Real embedding/structured-stream smoke; read-back parser log confirms four-page PDF and image-only OCR. Full corpus/resource benchmarks pending. |
| M1-M3 backend/core | Implemented; partial acceptance verified | Ruff exit 0; 53 backend tests pass with database integration enabled; file-scoped processing Pyright check passes. Live synthetic TXT upload/ready/streamed answer/history checked. Restart/fault matrix and full evaluation pending. |
| M4 insights/customization | Implemented; partially verified | Structured analysis and summary artifact code plus deterministic tests. Live coverage/long-document review pending. |
| M5 comparison/cache/quotas | Partially implemented | Comparison, scoped caches and reuse implemented. Daily budget reservations and full quota gates pending; DAILY_BUDGET_USD currently has no enforcement. |
| M6 frontend | Implemented; browser validation pending | Two-column SaaS shell, Files, Usage, Agent/New Run/Past Runs. assistant-ui External Store Runtime integrates the Python SSE/history with thread/composer primitives, scroll-to-bottom, Radix icon tooltips and streaming MarkdownText. Typecheck/build and 32 transport/runtime/rendering tests pass. User chose manual browser validation. |
| M6 realtime/operations | Partially implemented | Redis/WebSocket invalidation, SSE, metrics, worker heartbeat and readiness exist. Persistent revisions and complete reconnect/metrics checks pending. |
| M7 evaluation/submission | Pending | Frozen evaluation corpus/results, performance benchmark, clean Compose reproduction and demo recording/walkthrough remain; AI_USAGE.md now records actual assistance. |

Record actual commands and evidence in [progress.md](progress.md). Passing deterministic provider tests verifies application behavior, not model quality. No entire milestone or bonus is complete solely because its code exists.

### Responsibility map

```text
README.md
AI_USAGE.md
.env.example
pyproject.toml
uv.lock
Dockerfile
compose.yaml
alembic.ini
migrations/
src/docvault/
  main.py                 # app construction, route registration, lifecycle
  config.py               # validated settings
  db.py                   # sessions and transaction boundary
  models.py               # SQLAlchemy models; split only when needed
  schemas.py              # reusable API schemas; local schemas may live with routes
  api/
    documents.py
    chats.py
    insights.py
    metrics.py
    events.py
    health.py
  documents.py            # upload/version/status behavior
  processing.py           # stage functions and checkpoints
  parsing.py              # Docling conversion and provenance normalization
  chunking.py             # structure-aware chunk construction
  retrieval.py            # scoped SQL retrieval, fusion, context assembly
  chat.py                 # sessions, mutable selection, retry/cancel, message lifecycle
  workflows.py            # compiled LangGraph ingestion/chat/analysis workflows
  insights.py             # summaries, tags, evidence, comparison
  jobs.py                 # durable claim/retry/fencing rules
  dispatcher.py           # publish and reconcile durable jobs
  worker.py               # RQ entry points and resource configuration
  storage.py              # local storage operations
  ai.py                   # provider calls, schemas, usage capture
  cache.py                # explicit keys and Redis operations
  limits.py               # rate, resource, and budget admission
  telemetry.py            # structured logging and metrics
  prompts/                # versioned chat/rewrite/insight/comparison templates
tests/
  unit/
  integration/
  fixtures/
evals/
  tuning.jsonl
  acceptance.jsonl
  run.py
  results/                # checked-in aggregate report, no secrets
web/
  src/
  tests/
scripts/
  seed_demo.py
  demo.py
docs/
  spec.md
  implementation-plan.md
  research.md
  demo.md
```

This is a responsibility map, not a requirement to create empty files up front. Avoid generic repositories, service base classes, plugin registries, autonomous agents, and duplicate DTO layers. Test domain functions with explicit inputs; inject clocks, identifiers, and external clients only where nondeterminism or I/O needs control.

## 2. M0 - Resolve technical risks with a small corpus

**Build/check**

- Confirm Python/container compatibility and pin the initial package set. Choose a Docling-supported Python version; proposed baseline is Python 3.12, subject to package resolution.
- Create redistributable clean/scanned/mixed/two-column/table PDF, DOCX, and TXT fixtures with known source locations. Add negative files and prompt-injection text. Prepare 12-15 documents, a separate tuning set, and at least 40 held-out questions meeting the spec's answerable/unanswerable split.
- Measure Docling CPU time, peak RAM, first-use downloads, OCR inclusion, and provenance. Compare a lightweight parser on clean PDFs as a control. Record the installed version/options and limitations.
- Validate the configured OpenRouter account with `openai/gpt-4.1-mini` and `openai/text-embedding-3-small` through the OpenAI SDK at `https://openrouter.ai/api/v1`. Check embedding dimensionality, schema support, provider usage, actual incremental response text, cancellation, and context-overflow behavior. The user reports `.env` is ready; never print or commit the key. Documentation availability is not a live smoke pass.
- Resolve the structured `Answer {response, suggestions, citation_ids, outcome}` plus streaming contract; enforce at most three suggestions. Define golden examples for successful answers, abstention, clarification, insights, and comparisons. Require supporting OpenRouter routing parameters where needed.
- Compile bounded LangGraph ingestion/chat/analysis workflows and make their functions testable independently. Keep SQL-owned history/jobs/checkpoints and RQ dispatch; do not add a second LangGraph persistence authority initially. Record the resolved dependency versions before relying on unfamiliar APIs.

**Likely files:** `pyproject.toml`, `uv.lock`, `tests/fixtures/`, `evals/`, parser/provider spike scripts, a short results note in `docs/`.

**Gate:** a recorded parser/model choice with source-location examples; a measured resource envelope; an actual streaming/schema smoke result or an explicit unresolved credential dependency. Do not call M0 verified from mock outputs. If Docling exceeds the host budget, choose the lightweight digital path plus an explicit OCR fallback and update the spec before adding two competing stacks.

## 3. M1 - Runnable service foundation

**Build**

- Add FastAPI app/settings, PostgreSQL + pgvector, Redis, shared private storage volume, worker and dispatcher service definitions, and one-shot migrations.
- Add the single local workspace configuration, consistent error envelope, request IDs, liveness/readiness, and structured logging. Bind published ports to loopback, restrict browser origins, and keep OpenRouter credentials server-side. Do not add IAM, API-key models, or tenant-isolation tests in this phase.
- Add database/session helpers and real-infrastructure pytest fixtures. CI runs lint plus unit/integration checks with deterministic fake provider responses. SQL loads/stores conversation history explicitly; graph invocation state is transient.
- Add `.env.example`, startup/migration instructions, and `AI_USAGE.md` recording actual assistance and human review as work happens.

**Likely files:** root configuration, `main.py`, `config.py`, `db.py`, `models.py`, `schemas.py`, `api/health.py`, `telemetry.py`, first migration, initial tests.

**Gate:** a fresh Compose start applies migrations and exposes `/docs`; API and worker see the same database/storage; all local clients see the one shared workspace; provider secrets stay off browser responses; readiness detects stopped dependencies without paid API calls. Loopback exposure and local CORS/WS origin configuration are checked.

**Commit boundary:** service foundation and reproducible local environment.

## 4. M2 - Upload to a complete searchable version

**Build**

- Implement streamed file validation, size/format limits, hashing, safe storage keys, idempotency records, logical documents, immutable versions, and upload/batch APIs.
- Commit accepted versions and durable jobs together. Implement dispatcher publication, worker database claim/lease/fencing, checkpoints, bounded retry decisions, and stale-job recovery.
- Connect a compiled LangGraph ingestion workflow: canonical parsing -> source-aware chunks -> model-safe embedding microbatches -> staging index -> atomic ready activation. Re-run the graph using persisted application stage checkpoints and completed artifacts/batches after recovery. A missing LangGraph checkpointer does not provide automatic graph-state resume.
- Keep byte/page upload safeguards, but add no whole-file extracted-token cap. Split every embedding input under the verified provider input limit and preserve complete source coverage.
- Implement list/detail/version/content/status/retry endpoints, document deletion/cleanup, baseline statistics and processing-history APIs. Add batch membership/result records.
- Persist original source locations and normalize PDF/DOCX/TXT citation locators. Reuse computed values through independently owned per-version artifacts/chunks so deletion cannot damage another live resource. Capture provider usage at the call boundary from the first billable call.

**Likely files:** document/processing/parsing/chunking/storage/jobs/worker/dispatcher modules, document routes, relevant schemas/migrations, unit and integration tests.

**Gate:** all supported fixture types reach correct ready/failed outcomes; repeat idempotency keys cannot duplicate accepted versions; mixed batches isolate failures; restart during parsing/embedding resumes safely; duplicated jobs cannot publish two active generations; Redis loss after acceptance cannot strand a job; downloads validate live source/version relationships. Source maps and OCR text are inspected against the fixtures.

**Commit boundaries:** durable uploads and versions; complete ingestion; recovery and baseline metrics. Keep commits independently testable rather than committing an unusable scaffold as a feature.

## 5. M3 - Cited chat, history, and genuine streaming

**Build**

- Create multiple named chat sessions with mutable selected ready versions. The UI selects logical documents and resolves their current ready snapshots. Implement `PATCH /v1/chats/{id}` with `{title?, version_ids?}`. Copy the selection/index fingerprints onto each message; future selection edits never rewrite historical or active turns. Add no maximum selected-version count.
- Implement exact dense search through pgvector's SQLAlchemy `cosine_distance`, plus PostgreSQL lexical ranking, `calculate_rrf`, evidence packing, and citation resolution. The [official pgvector-python RRF example](https://github.com/pgvector/pgvector-python/blob/master/examples/hybrid_search/rrf.py) supplies custom SQL, not an importable fusion method; retain the small application helper with consensus/empty-result/tie checks. Use native HNSW/IVFFlat indexes if the later filtered-recall/latency benchmark warrants approximate search; do not implement ANN algorithms in application code. The message's captured version/index filters belong inside SQL. Retrieve relevant chunks without a fixed evidence-token cap; reserve output room and handle actual provider context limits explicitly, with split processing or a clear narrowing error rather than silent truncation.
- Implement a compiled LangGraph chat workflow with SQL-loaded recent history, conditional standalone-question rewriting, retrieval, evidence-only generation, `Answer` validation, and final persistence. Return up to three suggestions with the same structured answer; support insufficient-evidence/clarification outcomes.
- Persist message lifecycle and call usage; enforce one active generation per chat and idempotent submission. Recheck source deletion and the generation claim before answer commit.
- Keep worker document/artifact lookups guarded and typed, reuse required-resource helpers, and narrow optional ingest versions before parsing. Verify missing artifacts cause cancellation before provider work, summary results persist, and Pyright reports zero diagnostics for `processing.py`; this is a file-scoped check, not a whole-repository typecheck claim.
- Use [SQLAlchemy UPDATE RETURNING](https://docs.sqlalchemy.org/en/21/tutorial/data_update.html#using-returning-with-update-delete) with scalar IDs to detect heartbeat/recovery updates, rather than accessing `rowcount` on the general ORM `Result` type. Verify successful and lost-claim heartbeats, stale/live generation separation and no-change notifications; check `jobs.py` with Pyright. No casts or disabled diagnostics are needed.
- Add `POST /v1/chats/{id}/messages/{assistant_id}/retry` and `/cancel`. Retry/regenerate only the latest user turn after its most recent assistant attempt is terminal, using the original user message/version snapshot and a fresh idempotency key. Retain old attempts by message ID, return only the newest assistant attempt per turn in normal history, reject older-turn retries, and bypass answer reuse. No branch UI is required. Cancel fences a late result and stops the provider stream where possible; the active-run rule permits a new attempt once the previous one is terminal/fenced.
- Offer JSON and real SSE through the same chat service. Map internal `Answer.response`/`citation_ids` to frontend `Message.content`/resolved `citations`, preserving `suggestions` and `outcome`. Implement provisional deltas, validated canonical completion, interruptions, failed-call state, and status/history recovery.
- Provide a CLI/API demo that uploads a fixture, waits for readiness, asks a question, follows up, and resolves a source citation.

**Likely files:** `retrieval.py`, `chat.py`, `workflows.py`, `ai.py`, chat schemas/routes, prompt templates, message/chat migrations, `scripts/demo.py`, retrieval/stream integration tests.

**Gate:** the first end-to-end release works with OpenRouter; citations resolve exact source spans; history survives restarts; a follow-up resolves correctly; absent evidence causes abstention; injection text cannot alter source selection; no completion event precedes commit. Test independent sessions, selection changes between/during turns, model-window overflow, repeated idempotency keys, cancellation versus late completion, and explicit retry/regeneration. No accidental duplicate run occurs, and the one-active-run guard does not disable later retries. Run and record the first retrieval/grounding evaluation.

**Commit boundaries:** scoped retrieval and source resolution; persisted multi-turn chat; validated streaming and demo.

## 6. M4 - Insights and customizable summaries

**Build**

- Add independent insight jobs after index readiness. Use a compiled LangGraph analysis workflow for structured category/tags, source-linked key insights, summary, and up to three document-level suggestions, while SQL owns artifact/job persistence.
- Add section fact extraction plus reduction for long documents. Preserve evidence and coverage through reduction; never drop the tail of a long document silently.
- Add length/focus/tone options with artifact signatures and a job-based customization endpoint. Reuse completed results when signatures match.
- Add document filters by category/tag. Keep insight failure separate from document readiness.

**Likely files:** `insights.py`, insight routes/schemas/prompts, artifact migrations, job dispatch rules, relevant fixtures/tests.

**Gate:** each output conforms to its schema and citations; a fact at the end of a long fixture appears when appropriate; distinct summary options produce distinct artifacts; repeated options reuse the artifact; failure leaves document chat usable. Key-insight extraction demonstrates B05 without adding generic sentiment analysis.

**Commit boundary:** complete insights and summary customization.

## 7. M5 - Multi-document features and efficiency controls

**Build**

- Extend the core multi-document chat to fair evidence packing and comparison jobs with per-document/per-dimension retrieval, evidence-backed cells, and explicit missing/conflicting facts. Accept any selection count within existing resource/model constraints; large comparisons split work and preserve each selected source rather than applying a four-version cap.
- Evaluate the core answer suggestions against the current question, supported answer, and selected document context. Keep at most three within the same structured completion to avoid a call per suggestion.
- Implement local-workspace embedding reuse, exact response caching, deletion/source checks, and changed-selection/version/history cache misses. Explicit regeneration bypasses completed-answer reuse.
- Add atomic Redis rate limiting and database resource/budget reservations. Reconcile usage after timeout/crash, including unknown provider outcomes. Add the usage API and configured versioned price table.
- Optimize bulk enqueue, embedding batch size, and bounded concurrency. Reuse unchanged contextualized embedding inputs between versions. Verify the document-current-pointer race.

**Likely files:** retrieval/chat/insight extensions, comparison routes, `cache.py`, `limits.py`, AI usage/budget migrations, metrics routes, concurrency and cache tests.

**Gate:** at least one question needs both selected sources; comparison cells have the right source/version; absence is not inferred as agreement; changed versions/history cannot reuse stale answers; changed selections cannot reuse an answer grounded in different sources; concurrent calls cannot exceed reserved budgets. Repeat a request and show measured provider calls avoided. Batch and version tests demonstrate reuse without losing provenance.

**Commit boundaries:** multi-document chat and comparison; correct caching; cost/quotas/batch controls.

## 8. M6 - Small UI, realtime status, and operations

**Build**

- Build two coherent primary screens with React, Vite, TypeScript, assistant-ui for chat, and shadcn/ui for the dashboard: document library/upload with searchable/filterable rows and detail panels; chat with session navigation, selected-document control, transcript, suggestions, source drawer, and composer. Integrate versions, insights, customization, comparison, and metrics into these screens without unnecessary route proliferation. Use the user-provided fileAI reference: one sidebar and main canvas, Agent with New Run and nested Past Runs, compact neutral controls, a bar-chart icon for Usage, breadcrumb navigation, and no separate conversation rail or promotional cards. Add a + source-picker control, a small sparkle beside the new-run heading, and keyboard/touch-accessible per-run menus for rename and confirmed deletion; keep these management actions exclusively in the sidebar, without duplicate controls in the chat canvas.
- Use `@assistant-ui/react` 0.15.23 [External Store Runtime](https://www.assistant-ui.com/docs/runtimes/custom/external-store), with React Query messages, explicit status conversion, text-only send callbacks, latest-turn retry, and backend-confirmed cancellation. Use Thread/Message/Composer/Suggestion/Reload primitives while retaining the citation renderer and source picker. Keep visible retry history at one newest attempt per turn; SQL retains earlier attempts. Assistant Cloud and edit/branch controls are outside this integration.
- Use the official ThinkingIndicator registry source with the runtime's running/visible-text state. Replace the custom spinner/dots, support empty-text pending messages and optimistic placeholders, hide on answer text/terminal state, clean up the elapsed timer on inactivity/unmount, and provide reduced-motion styling. The visible elapsed badge is client waiting time, not backend stage timing.
- Add runtime `ThreadPrimitive.ScrollToBottom`, hidden at the bottom, while preserving reader-controlled scrolling. Use a Radix-compatible TooltipIconButton for icon-only chat controls. Render assistant text parts through MarkdownText with the approved `@assistant-ui/react-markdown` dependency; preserve GFM, citation drawer links and safe external links, and provide code-block copy success/failure feedback. Keep the standalone Markdown renderer for document panels. Verify citation/formatting behavior with runtime tests, typecheck/build, then manual scroll, tooltip, clipboard and streaming checks. Message editing and branches remain deferred after the user's explicit decision to skip editing on 3 October.
- Connect directly to Python JSON/SSE APIs, using a fetch stream and correct SSE framing. Implement explicit loading/errors/empty states, streamed provisional response text, canonical completion replacement, citation drawer, session renaming/selection updates, cancel/regenerate controls, and interrupted-request recovery. Keep the OpenRouter key entirely on the server; no app-key entry UI is needed.
- Add local-origin checks, revisioned WebSocket processing and persisted chat lifecycle notifications, reconnect, and periodic snapshot reconciliation. Reconcile both processing and chat state; retain SSE for token deltas and polling as a fallback. IAM remains deferred.
- Finish persistent processing/performance metrics, worker heartbeat, operational export, and readiness behavior. Bound metric-card queries and cardinality.
- For PDFs, open the live source at the cited physical page; provide an excerpt/location fallback. Pixel-perfect box overlays are outside the acceptance scope.

**Likely files:** `web/`, `api/events.py`, `telemetry.py`, metric storage/migrations, health/metrics routes, Playwright smoke flows.

**Gate:** browser flow covers upload -> progress -> insights -> question -> citation -> follow-up -> comparison, plus multiple sessions, changed selection, and cancel/regenerate; a failed parse and exhausted budget are understandable; refresh/reconnect recovers the correct status; no secrets appear in URL/history/logs. Metrics agree with database records and clarify estimates/no-data states.

**Commit boundaries:** basic document/chat UI; comparison/metrics UI; reliable realtime and monitoring.

## 9. M7 - Evaluate, harden, and package the submission

**Finish**

- Run the frozen evaluation set, distinct from tuning. Publish evidence hit rate, multi-source coverage, reviewed claim support, abstention counts, latency distribution, and estimated usage cost with configuration/hardware details.
- Fix failures introduced by the implementation and rerun the relevant checks. Change retrieval/chunk/model settings only against identified failure cases; preserve the frozen acceptance set.
- Run the recovery, selected-source, session, and retry matrix below with real PostgreSQL/Redis and deterministic provider faults. Run a separate small OpenRouter smoke for actual model integration. Do not claim IAM or tenant isolation as implemented.
- The workspace's `AGENTS.md` asks to confirm before driving UI validation. Prepare the runnable UI and checks first, then confirm whether the user prefers agent-driven browser verification or manual validation; report UI behavior as unverified until one is completed.
- Benchmark 10k chunks and five concurrent chats. Add HNSW only if exact retrieval misses the latency target; compare filtered recall against exact search before accepting it.
- Complete README setup, architecture, environment variables, API/curl examples, limits, prompt rationale, operational recovery, and known tradeoffs. Complete `AI_USAGE.md` with actual examples of assistance, corrections, and verification; invent no time-saving numbers.
- Write `docs/demo.md` and produce the required demo recording or live walkthrough. Document a clean reset and reseed process. A public deployment, if requested, needs a concrete hosting target and its configuration.

**Gate:** every row of the spec traceability table has working behavior and evidence, or is clearly recorded as incomplete; no partial feature is presented as a completed bonus. Fresh setup is reproduced. Core checks and evaluation gates pass. Secrets and private input documents are absent from committed artifacts.

**Commit boundary:** verified release documentation, evaluation results, and demo assets. Commit/push/deploy actions follow the user's implementation instructions. Do not add coauthor trailers; do not commit secrets or `.env`.

## 10. Verification matrix

| Level | Meaningful checks |
|---|---|
| Unit | Chunk source mapping and token budgets; table-header preservation; RRF/provider context-window handling; cache signatures; citation resolution; retry classification; cost accounting and prompt schema validation. Avoid tests that merely repeat a constant. |
| Database/Redis integration | Source/version relationships and per-message snapshots; transaction rollback; durable enqueue recovery; duplicate delivery; fencing/lease races; concurrent version numbers/current pointer; budget/resource reservation races; cache scope and expiration. |
| API contract | Multipart validation, pagination, idempotency conflicts/replay, stage errors, correct `202/409/429/503`, live content and citations, mutable selections, retry/cancel races, chat concurrency, structured SSE finalization. |
| Failure injection | Stop API/worker/Redis at stage boundaries; provider timeout/429/5xx/invalid schema; fail after provider success before DB commit; disk full; delete during parsing/generation; WS lost notifications; stream disconnect. |
| RAG evaluation | Known evidence locations, exact IDs, table relationships, late-document facts, paraphrases, follow-up references, multi-source comparison, conflicting facts, absent evidence, prompt injection, unsupported charts. |
| Browser E2E | One full happy path and focused parse-failure, budget, reconnect, multiple-session, document-selection, retry/cancel, and source-citation flows. Backend behavior is still independently testable. |
| Performance | Cold/warm parser resource usage; extraction/embedding/generation split; retrieval at stated corpus size; bounded concurrency; first delta and final latency; repeat-request call reduction. |

Deterministic provider fakes verify application behavior, not model quality. Live-model evaluation checks integration/grounding and records variability. Coverage percentage is a diagnostic; the listed critical paths and failure scenarios are the completion contract.

## 11. Demo narrative and deliverables

A concise demo should show:

1. Upload a digital contract and a scanned policy as a batch; watch progress and point out source-aware summaries/tags.
2. Ask a precise question, watch streaming, open the page citation, and ask a contextual follow-up.
3. Ask a question absent from the documents and show honest insufficient evidence.
4. Select both documents and compare a few dimensions with citations on both sides.
5. Upload a revised contract; select its newer ready version for the next turn, and show the earlier answer's unchanged version reference. Open another chat session to demonstrate independent history.
6. Cancel an active answer, retry it, then regenerate the latest completed answer; show its unchanged source snapshot and explain that prior attempts remain stored while normal history shows the newest response.
7. Request a focused short summary; repeat an identical question with the same source/history context in a fresh session to demonstrate cache reuse; show cost/processing metrics. Regeneration intentionally bypasses that answer cache.
8. Show one invalid file or retryable failure and its recovery. Use a labeled deterministic fault-injection mode for a reproducible failure demonstration.

Submission checklist: working backend, source repository, generated API docs, README, `AI_USAGE.md`, architecture diagram, documented assumptions, tests/evaluation report, demo fixtures/script, and the required chat demo. The optional UI and all completed bonus features should be demonstrable rather than just mentioned.

## 12. Scheduling and remaining decisions

Implementation is underway. Carry the smallest flow through M3 while frontend work proceeds against the shared API contracts, then complete the remaining milestones. If the deadline is tight, reduce UI polish and optional research experiments first; all 18 requested bonus items remain visible in the plan until any scope reduction is explicitly agreed. Avoid claiming that an API stub or design document satisfies a functional bonus.

The deadline remains to be confirmed. Provider/UI/orchestration choices are settled for this phase, and the user reports the provider key is configured privately. IAM, public hosting, a LangGraph checkpointer, and measured evidence-token tuning are deferred. Update the living status table only with actual verification results; no feature, bonus, or performance target is complete solely because it appears in this plan.
