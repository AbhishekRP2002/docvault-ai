# Implementation and verification ledger

Updated: 2 October 2026. This ledger records the current repository, not just the planned architecture. The [specification](spec.md) defines acceptance; the [implementation plan](implementation-plan.md) defines remaining work.

**Implemented** means code exists. **Verified** describes a specific executed check. **Partial** means some scope or acceptance evidence is missing. A bonus is not complete merely because its endpoint exists.

## Core implementation

| Area | Current state | Evidence and remaining work |
|---|---|---|
| FastAPI, PostgreSQL/pgvector, Redis/RQ, private local storage | Implemented; partially verified | Database migrations applied locally; health/readiness and synthetic ingestion exercised. README local setup exists; full clean Compose startup not yet reproduced. |
| Validated TXT/PDF/DOCX uploads, batch acceptance, immutable versions | Implemented; partially verified | API tests cover malformed uploads, persistence, replay/conflict and mixed batch. Live TXT reached ready. Parser checks below. Broader parser corpus and concurrent upload stress pending. |
| Durable jobs, leases, fencing, retries, recovery, cleanup | Implemented; partially verified | Nine processing integration tests pass. Complete process-kill/Redis-loss fault matrix pending. |
| Hybrid retrieval and cited multi-turn sessions | Implemented; partially verified | API tests verify history, source snapshots, sessions and deletion checks. Live cited answer persisted. Frozen retrieval/grounding evaluation pending. |
| LangGraph and OpenRouter generation/embeddings | Implemented; live smoke verified | Configurable provider boundary; explicit graph stages, strict output schema, real response deltas. SQL owns persistence; no LangGraph checkpointer/resume claim. |
| Retry, regeneration, cancellation, one active generation | Implemented; API tests verified | Latest-turn regeneration retains source snapshot and bypasses answer cache; older-turn retry rejected. Admission and cancellation races covered. UI flow pending manual validation. |
| API docs, metrics, local configuration | Implemented; partial evidence | FastAPI OpenAPI, usage and processing metrics, request logs, readiness. Metrics reconciliation and complete operations gates pending. |
| Spec and implementation plan | Updated | Both reflect the user's UI revision and link this ledger. |

## All 18 bonus items

| ID | Feature | Implementation / remaining gate |
|---|---|---|
| B01 | Multi-document chat | Implemented. Source-count caps removed and API checks pass. Live multi-source evidence coverage evaluation pending. |
| B02 | Follow-up suggestions | Implemented. At most three validated; live answer returned three. Broader usefulness review pending. |
| B03 | Customized summaries | Implemented with persisted artifacts and length/focus/tone options. Live customization/long-document coverage review pending. |
| B04 | Categorization and tags | Implemented. Live synthetic policy has persisted category/tags. Manual filter validation pending. |
| B05 | Key insights | Implemented with evidence IDs. Live policy insights returned HTTP 200. Human claim-support and late-document tests pending. |
| B06 | Comparison | Implemented with per-cell source IDs and missing-information states. Full live comparison quality gate pending. |
| B07 | Frontend/dashboard | Implemented; redesigned to one sidebar plus main workspace. Agent contains New Run and Past Runs. Build/typecheck pass; user chose manual browser validation. |
| B08 | Realtime updates | Partial. WebSocket invalidations, polling/reconciliation and SSE exist; persistent database revisions and full reconnect/lifecycle matrix pending. |
| B09 | Smart caching | Implemented with scoped answer/query/embedding/parser reuse. Deterministic cache and retry checks pass. Measured live reuse report pending. |
| B10 | Vector database | Implemented with pgvector plus lexical search/RRF. Exact retrieval; no HNSW performance claim. 10k-chunk benchmark pending. |
| B11 | Streaming chat | Implemented. Live run produced 39 response text deltas, then canonical persisted completion. Browser stream/reconnect validation pending. |
| B12 | Comprehensive testing | Partial: 47 backend and six frontend tests pass. Full fault matrix, held-out evaluation and browser tests remain. |
| B13 | Cost tracking/optimization | Partial. Durable actual provider usage/cost ledger, unknown-cost counts, caches and embedding microbatches exist. Versioned estimate rates and measured savings report pending. |
| B14 | Rate limiting/quotas | Partial. Atomic Redis rate admission exists. Daily cost reservation/enforcement is not implemented; `DAILY_BUDGET_USD` currently has no enforcement. Budget/resource concurrency gates pending. |
| B15 | Versions | Backend implemented; snapshots and retry scope verified. UI version details/upload exist. Explicit historical-version selection in the chat picker and full current-pointer stress gate pending. |
| B16 | Batch optimization | Implemented per-file ingestion, embedding microbatches, checkpoint reuse and bounded worker dispatch. Throughput benchmark and batch membership/version audit pending. |
| B17 | Observability | Implemented request IDs/logs, job attempts, stage history and database metric buckets. Full metric reconciliation and load behavior pending. |
| B18 | Health/monitoring | Implemented live/ready, worker heartbeat, usage/document/processing metrics. Local readiness checked; stale-worker/failure UI and operational demo pending. |

## Executed checks

| Check | Command or retained evidence | Actual outcome |
|---|---|---|
| Backend lint | `.venv/bin/ruff check src tests migrations` | Exit 0: `All checks passed!` |
| Full backend tests | `RUN_INTEGRATION=1 TEST_DATABASE_URL=postgresql+psycopg://docvault:docvault@127.0.0.1:15432/docvault_test .venv/bin/python -m pytest tests -q` | Exit 0: **47 passed in 9.85s**; no skipped tests. Tests create isolated schemas; application data is preserved. PostgreSQL is real; providers and selected transport boundaries are deterministic fakes. |
| Frontend transport tests | In `web/`: `bun test src/lib` | Exit 0: **6 passed, 0 failed, 11 assertions**. Covers split SSE frames, interrupted generation, actionable errors, and complete pagination. These are not browser tests. |
| Frontend typecheck/build | In `web/`: `bun run typecheck`; `bun run build` | Exit 0 after redesign. Production bundle generated. |
| 21st design review | `21st review web/src/App.tsx web/src/pages/chat.tsx web/src/index.css --json` | Local deterministic review executed; no errors. Composer uses a responsive maximum width, not a fixed minimum width. Dialog autofocus removed. Token-definition color suggestions are intentional. Final review: five files, zero errors/warnings; 12 informational color suggestions. |
| Real PDF/OCR parser | Read back `/private/tmp/docvault-parser-verification.log` | Supplied assignment: 4 pages, 26 chunks with provenance. Image-only fixture: 1 page, 1 chunk, expected OCR text recovered. This does not verify the entire planned parser corpus or performance targets. |
| Live upload/answer | `/private/tmp/docvault-api-smoke.py`; `/private/tmp/docvault-api-smoke-result.json` | Synthetic renewal policy ready; answer contains USD 1200 and 30 days; **39 deltas, 2 citations, 3 suggestions**; deltas match final content and persisted history matches. Smoke script was corrected to decode SSE event names separately from data. |
| Live insights | `GET /v1/versions/58f2c9de-4fc6-4a1d-aa82-bc058d1c5928/insights` | HTTP 200; ready summary, category, tags, evidence-backed key insights; coverage reports all four chunks processed. No broad model-quality claim. |
| Browser validation | User response on 2 October 2026 | **Manual validation selected. Agent did not drive the browser.** Desktop/mobile appearance and interactive flow remain unverified. |

21st catalog search returned HTTP 401. Public [sidebar guidance](https://docs.21st.dev/blog/react-sidebar-component-examples) informed nested navigation; project shadcn/Radix primitives were reused. No catalog component is claimed installed. Durable design choices live in [`.21st/design.json`](../.21st/design.json).

## Next work, in order

1. User's manual UI review: New Run, select files, send, Past Runs, rename/delete, stop/regenerate, citations, mobile drawer, and Files/Usage.
2. Implement optional cost reservations and versioned price estimates; test concurrent budget admission and unknown provider outcomes. Review resource quotas without restoring removed token/source-count caps.
3. Complete historical source selection, persistent realtime revisions/recovery, and batch/version audit gaps.
4. Run the held-out RAG/grounding corpus, full recovery matrix, comparison/long-summary review and stated performance benchmarks.
5. Reproduce fresh Compose setup; maintain `AI_USAGE.md`, complete demo fixtures/script and required recording/live walkthrough. No public deployment has been performed.

## Deferred by the user

- IAM and multi-tenant authorization.
- A fixed 6,000-token evidence budget; reconsider later with measurements.
- Extracted tokens per file and selected-source-count caps remain removed.
- LangGraph checkpointer and public hosting are outside the current initial implementation.

## Local processes retained for manual review

Frontend: http://127.0.0.1:5173/#agent, PID **47231**. API: http://127.0.0.1:8000/docs, PID **47097**. These are the existing development processes, retained for the user's review. Stop only these owned processes with `kill 47231` or `kill 47097` when finished. Worker PID **47157** and dispatcher PID **47739** remain available; stop with `kill 47157` and `kill 47739` when finished. PostgreSQL/Redis are the `docvault-ai` Compose services; stop just these with `docker compose stop postgres redis` when no processing is running. No unrelated containers were stopped.
