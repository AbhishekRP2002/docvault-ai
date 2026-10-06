# Design choices and assumptions

Current implementation choices as of 6 October 2026. Planned changes are labeled; test results are in [evaluation-findings.md](evaluation-findings.md) and [progress.md](progress.md).

## Model provider: OpenRouter

We chose OpenRouter to test models from different vendors through one API and API key, using the OpenAI Python SDK. This keeps model experiments independent of the rest of the application. OpenRouter supports provider routing, price-based load balancing with availability checks, provider fallbacks, and routing preferences for price, latency, or throughput. It also supports ordered model fallbacks when the requested model fails, including rate limits and context-length errors. [Provider routing](https://openrouter.ai/docs/guides/routing/provider-selection), [model fallbacks](https://openrouter.ai/docs/guides/routing/model-fallbacks).

**Current use:** One model per task, default provider routing, and required support for requested parameters. Custom routing policies and a model-fallback list are not configured.

Chat uses GPT-6 Luna; summaries, comparisons, and conversation memory use GPT-5.6 Luna, with `medium` reasoning. These are configurable starting choices, not an evaluated best-model claim. [llm/config.py](../src/docvault/llm/config.py) owns settings; live context metadata takes precedence over the fallback.

## Document parser: Docling

We chose Docling to retain reading order, headings, tables, OCR, and source locations through parsing and chunking. These support retrieval and citations beyond plain-text extraction. Parsing runs locally; excerpts still go to OpenRouter for embeddings and generation. [Docling features](https://docling-project.github.io/docling/), [chunking](https://docling-project.github.io/docling/concepts/chunking/).

**Current use:** Docling converts PDF/DOCX, with PDF OCR and table recognition enabled. TXT is read directly and uses the same chunker. Citations retain PDF/DOCX item provenance or TXT character/line offsets; exact split-chunk PDF offsets are unavailable.

**Planned change:** We reviewed Rust-based LiteParse v2 and its Python bindings for faster parsing. We plan to evaluate switching to it, potentially retaining Docling for richer extraction. Neither integration nor a comparative benchmark is complete. Before switching, compare latency, OCR, reading order, tables, chunk coverage, and citation mapping on identical files. Published speedups are not DocVault measurements. [LiteParse v2 announcement](https://www.llamaindex.ai/blog/liteparse-v2-0-runs-everywhere).

## Agent framework: LangGraph

We chose LangGraph for explicit state, small Python nodes, and configurable conditional transitions. The agent can read evidence, inspect results, and request further reads before answering. Tools and validation steps can evolve independently. LangGraph supports cycles; our chat graph contains a loop and is not a DAG. [Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api).

**Current use:** `chat → tools → chat`, then `validate → end`, with concurrent independent reads, a 20-round bound, and a timeout. Parsing and analysis use ordinary Python functions. PostgreSQL owns durable jobs/history/stage outputs. No LangGraph checkpointer is configured; mid-turn graph resume is unavailable. [Implementation](../src/docvault/llm/graphs.py), [persistence](https://docs.langchain.com/oss/python/langgraph/persistence).

## Other implementation choices

| Choice | Why we made it and the tradeoff |
| --- | --- |
| **PostgreSQL + pgvector** | Keep document metadata, versions, jobs, conversations, full-text indexes, and vectors in one transactional database. This simplifies joins, source filtering, and local setup. HNSW speeds vector lookup but is approximate; a separate vector service is deferred. [pgvector](https://github.com/pgvector/pgvector). |
| **Hybrid retrieval with RRF** | Combine semantic matches with exact-term matches because either can miss useful passages alone. Lexical ranking is PostgreSQL `ts_rank_cd`, not BM25 or TF-IDF. Each branch contributes up to 15 candidates, fused to at most five passages. Only semantic candidates require cosine similarity **> 0.6**; lexical matches qualify independently. An RRF score is a rank-fusion score, not a similarity probability. These limits need corpus-specific calibration. [Implementation](../src/docvault/retrieval.py). |
| **Structure-aware, bounded chunks** | Docling's HybridChunker uses hierarchy and token limits, repeats table headers, and adds filename/heading context. The 750-token budget includes that context. This gives retrieval useful local passages without a separate LLM call to enrich each chunk. Sentence boundaries are not always preserved; see [chunking comparison](chunking-comparison.md). |
| **Immutable document versions** | Replacements create new revisions instead of overwriting cited content. Each turn captures its source versions; later turns follow the newest ready revision. A pending or failed replacement leaves the previous ready version usable. This preserves historical answers and comparisons at the cost of additional storage. |
| **RQ/Redis with SQL-owned jobs** | Parsing, embeddings, and analysis run outside API requests. RQ transports work; PostgreSQL records intent, attempts, leases, fencing tokens, and completed stages. Retries reuse saved work where possible. Source indexing and insight generation are separate jobs, so failed insights do not disable an indexed document. Delivery may repeat; this is not an exactly-once execution claim. |
| **Scoped tools and validated citations** | The backend fixes selected-source scope and exposes six read-only tools. Strict Pydantic schemas constrain output and allowed citation IDs; document claims require original passages, while operational answers require fresh tool results. Missing evidence can produce a clarification or abstention. Valid JSON and valid IDs do not prove that a claim is correct. |
| **Full-document analysis separate from chat retrieval** | Summaries and comparisons process all supplied chunks in model-sized batches and reduce results while retaining original citation IDs. Chat tools return bounded passages or previews for interactive use. A preview is labeled partial and does not claim full-document coverage. Key insights were chosen over generic sentiment because they are useful across technical documents and policies. |
| **Reuse and conversation memory** | Content hashes reuse unchanged embedding inputs. Completed-answer reuse includes source versions, conversation, retrieval configuration, model settings, prompts, and schemas to avoid stale results. Older chat turns can be summarized near context pressure; conversation memory resolves references and never becomes document evidence. |
| **FastAPI, React, and real streaming** | FastAPI provides the required Python API and generated API documentation. React with assistant-ui provides chat interaction primitives. Python SSE forwards actual model text deltas; only a validated, persisted result is marked complete. Cancelled or interrupted attempts remain distinct from success. |

## Scaling to thousands of documents

Async processing and caching are implemented; sustained capacity at thousands of real documents has not been demonstrated.

- **Ingestion:** Uploads return `202` after durable acceptance. RQ workers process queued jobs, and SQL leases/fencing coordinate ownership. Additional workers can consume the same queue on a host with shared storage; CPU, parser memory, and provider limits bound useful concurrency.
- **Reuse:** Saved parser artifacts and completed embedding batches survive retries. Input hashes reuse unchanged vectors; Redis caches query embeddings and eligible completed answers. This reduces repeated work, not the cost of processing new content.
- **Retrieval:** HNSW/GIN indexes and bounded candidates avoid sending the whole library to the model. API lists are paginated. Document count alone is insufficient: chunk count, document size/layout, and concurrent queries determine the workload.

**Current limits:** Compose starts one API process and one worker. Default admission is 20 uploads/hour and 20 chats/minute. There is one shared job queue, local file storage, and no autoscaling or global provider-concurrency controller. SQL revision events also accumulate without a retention policy. Larger deployments need measured worker sizing, queue/backlog handling, storage and database tuning, and event retention.

**Verification boundary:** The [progress ledger](progress.md) records a historical 12,000-vector retrieval check and worker/recovery exercises; these are not a 1,000-document load test. Validate representative digital/scanned files, ingestion throughput, queue age, peak memory, retrieval recall/p95 latency, cache hits, and provider cost before claiming capacity.

## Assignment assumptions and limits

- This is a **local, shared workspace** started with Docker Compose. Authentication, tenant isolation, public deployment, and managed object storage are deferred; uploaded originals live in a local storage volume.
- Supported inputs are PDF, DOCX, and UTF-8 TXT, with English OCR. Upload/page limits and upload/chat rate controls bound resource use. Provider costs are recorded when reported; enforced daily spend limits are deferred.
- Model quality, retrieval recall, parser speed, and sustained throughput require separate evaluation. Existing results are recorded in the evaluation documents above; this document does not claim production readiness.
