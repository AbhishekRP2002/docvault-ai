# DocVault LLM

A local document workspace with asynchronous PDF/DOCX/TXT ingestion, Docling HybridChunker, source-aware insights, cited chat sessions, comparisons, versions, streaming, and operational metrics. Python/FastAPI, PostgreSQL/pgvector HNSW plus full-text hybrid retrieval, RQ/Redis, LangGraph and OpenRouter power the backend; React/Vite/assistant-ui/shadcn power the frontend.

Implementation exists. Acceptance evidence and remaining work are tracked in the [progress ledger](docs/progress.md); this is not yet a fully verified submission. IAM and public hosting are deferred.

Project documents:

- [Product and technical specification](docs/spec.md)
- [Implementation plan and verification gates](docs/implementation-plan.md)
- [Document processing and RAG research](docs/research.md)
- [Implemented / verified / pending ledger](docs/progress.md)
- [Health, processing diagnostics, and recovery](docs/operations.md)
- [Docling / Chonkie / LlamaIndex chunking comparison](docs/chunking-comparison.md)

## Reviewer setup

The assignment requires a working backend, source repository, documentation, and a demo. It does not explicitly require a publicly hosted URL; PostgreSQL and file storage may be local. This submission uses a local Docker Compose workspace.

Install Docker with Compose (Docker Desktop includes both). From the repository root:

```sh
./start_up.sh
```

On the first run, the script creates a private `.env` and stops. Set `OPENROUTER_API_KEY` in that file, then run the same command again. Existing `.env` files are preserved. The sample already sets the task models and capacities; only the key is required initially. Database, Redis, and storage addresses are supplied by Compose inside the containers. The host script builds the images, starts PostgreSQL/Redis/API/worker/dispatcher/frontend, applies migrations, and waits for health checks before showing the URLs. The first build and parser asset download can take several minutes. No host Python, Node, Bun, or PostgreSQL installation is needed.

Open http://127.0.0.1:5173 for the workspace or http://127.0.0.1:8000/docs for interactive API testing. Upload files, wait for Ready, and start a chat. For comparisons, select two or more ready files and choose Compare; saved results are available through **Files → Comparisons**, including runs started before this update. Closing a result does not cancel it.

`make up` calls the same script. `make status`, `make logs`, and `make stop` inspect, follow logs, and stop the stack without deleting its volumes. Without Make, use `docker compose ps --all`, `docker compose logs --follow`, and `docker compose stop`. The script is for POSIX shells on macOS/Linux or Windows through WSL/Git Bash; PowerShell users can copy `.env.example` to `.env`, set the key, then run `docker compose up --build --wait --wait-timeout 300` directly. [Compose's wait option](https://docs.docker.com/reference/cli/docker/compose/up/) waits for services to be running/healthy and leaves them in the background.

Ports 5173, 8000, 15432, and 16379 must be available. If you already run DocVault manually, stop those owned API/frontend processes before switching to the complete Compose stack. The startup script does not kill host processes. Files, database records, Redis data, and parser assets persist in Docker volumes; avoid `docker compose down --volumes` if you want to retain them. Public hosting and IAM remain deferred.

## Parser tuning

Parser tuning is available in `.env.example`: `DOCLING_NUM_THREADS`, optional `DOCLING_PARSER_THREADS`, and per-stage `DOCLING_*_BATCH_SIZE`. Defaults retain four threads/batches; larger CPU settings were slower in the measured sample. Restart your worker after edits. Each active ingestion process has its own thread/model budget, so account for other workers on the same machine. A page batch is not a worker count. Existing ready documents retain their stored chunks; fresh ingestion uses the 750-token contextualized chunk limit.

To measure your own PDF without indexing or LLM calls, run `uv run --extra parsing python scripts/benchmark_parser.py /path/to/document.pdf --threads 4 --layout-batch-size 4 --device cpu --runs 2`, then compare another configuration. The command prints initialization/conversion/chunking time, page/chunk counts, and a text digest; it does not print document text. Uncached Docling assets may download during initialization. See the progress ledger for measured profiles and limitations.

## Local development

Copy `.env.example` to `.env` and set `OPENROUTER_API_KEY` privately. Never commit the key. Models are configured independently for chat, question rewriting, summaries, comparisons and embeddings; see the task settings below.

Start dependencies and install the parser/runtime:

```sh
docker compose up -d postgres redis
uv sync --python 3.13 --extra parsing
uv run --extra parsing alembic upgrade head
```

In separate terminals run:

```sh
uv run --extra parsing uvicorn docvault.main:app --host 127.0.0.1 --port 8000
uv run --extra parsing python -m docvault.worker
uv run --extra parsing python -m docvault.dispatcher
```

In `web/` run `bun install --frozen-lockfile` and `bun run dev --host 127.0.0.1`. Open http://127.0.0.1:5173/#agent. Use Files to upload, then Agent → New Run to select ready files and ask questions. Past Runs retains sessions and history. Usage shows processing and provider metrics. API documentation is at http://127.0.0.1:8000/docs.

After pulling the LLM naming/HNSW update, stop your API/worker/dispatcher, run `uv run --extra parsing alembic upgrade head`, and restart them. The additive migration preserves existing usage records while renaming `ai_calls` to `llm_calls` and adds the HNSW index. PostgreSQL needs pgvector 0.8+ for filtered iterative scans. New uploads use Docling HybridChunker; existing ready versions retain their stored chunks. No automatic re-embedding or file deletion is performed.

The local runtime was exercised on Python 3.13. Python 3.12 Linux ARM Docker images now build successfully; a fresh isolated Compose workspace passed startup, migrations, API/frontend readiness, proxy routes, and native Docling TXT chunking. Real-provider generation and PDF/OCR inside Docker, x86 Docker, and Windows remain separate unverified checks. Parser model assets download on first use; `uv run --extra parsing python scripts/warm_parser.py /path/to/small-scanned.pdf` can warm them for local development.

Linux installs the same locked PyTorch releases from their CPU-only index, avoiding 19 CUDA/NVIDIA/Triton packages. macOS wheels and other package versions are unchanged. The existing Torch dependencies are declared explicitly in the parsing extra so uv can select their platform-specific sources; see [uv's PyTorch configuration](https://docs.astral.sh/uv/guides/integration/pytorch/).

To replace a document, open its details and use **Upload new version**. Identical bytes reuse the latest revision; changed bytes are parsed/chunked and reuse vectors for unchanged contextualized input hashes. After indexing completes, new questions automatically use that document's newest ready revision. Pending/failed revisions keep the previous ready source active. Completed/in-flight answers and retries retain their exact historical snapshots; comparisons may explicitly select historical versions. Ordinary uploads are new logical documents, so matching filenames alone do not imply replacement.

Hybrid retrieval takes at most 15 semantic and 15 lexical candidates across the selected sources, then returns at most five chunks with cosine similarity strictly above 0.7. It can return fewer or none. The threshold is not a probability/confidence score; retrieval quality calibration remains pending.

## Configure LLM tasks

Edit [`src/docvault/llm/prompts.py`](src/docvault/llm/prompts.py) for system prompts and summary word targets. [`src/docvault/llm/config.py`](src/docvault/llm/config.py) contains model defaults, context/output reservations, optional temperature and embedding batch capacities. LLM response/evidence models are in [`src/docvault/llm/models.py`](src/docvault/llm/models.py). Document conversion, chunking and the `ParsedChunk`/`ParsedDocument` models belong to [`src/docvault/parsing.py`](src/docvault/parsing.py), beside the ingestion orchestration in `processing.py`.

| Task | Application default | Environment override |
|---|---|---|
| Chat answer and suggestions | `openai/gpt-6-luna` | `OPENROUTER_CHAT_MODEL` |
| Follow-up question rewriting | `openai/gpt-4.1-nano` | `OPENROUTER_INPUT_QUERY_REWRITE_MODEL` |
| Summary, category, tags and key insights | `openai/gpt-4.1-mini` | `OPENROUTER_SUMMARY_MODEL` |
| Comparison dimension findings | `openai/gpt-4.1` | `OPENROUTER_COMPARISON_MODEL` |
| Embeddings | `openai/text-embedding-3-small` | `OPENROUTER_EMBEDDING_MODEL` |

Input-query rewriting uses `InputQueryRewriteModelSettings` and task `input_query_rewrite`; legacy `OPENROUTER_REWRITE_*` environment names remain supported. Use the new `OPENROUTER_INPUT_QUERY_REWRITE_*` names for new configuration.

Use the separate capacity variables in `.env.example`. Existing `OPENROUTER_CONTEXT_TOKENS`/`OPENROUTER_MAX_OUTPUT_TOKENS` configure chat only; the other tasks use `OPENROUTER_<TASK>_CONTEXT_TOKENS`/`OPENROUTER_<TASK>_MAX_OUTPUT_TOKENS`. Optional `OPENROUTER_<TASK>_TEMPERATURE` is omitted by default. Restart the API/worker/dispatcher after configuration or prompt edits. Configured capacities must fit the selected model's actual limits. Changing embedding model/dimensions requires compatible document/query embeddings and index migration/reprocessing.

Generation passes Pydantic classes directly to the SDK's `parse`/`stream` helpers. OpenRouter receives a strict JSON schema with supporting-endpoint routing. Pydantic's native partial JSON parser exposes provisional response text during streaming; the SDK validates the complete model before persistence. Citation checks remain necessary: a correct JSON shape does not prove source support. Prompt/schema/task-setting changes invalidate answer/artifact reuse; queued artifacts fail clearly if their generation configuration changed before execution. Existing completed historical insights remain retained.

## Generate migrations

Create migration files with Alembic rather than authoring them manually:

```sh
uv run --extra parsing alembic revision --autogenerate -m "describe schema change"
```

Review the generated revision before applying it. Generation runs Ruff lint fixes and formatting automatically. Alembic cannot infer table renames; the project's generation hook preserves the known `ai_calls` → `llm_calls` ledger rename and rejects simultaneous ledger structural changes. It also emits the pgvector compatibility check when creating the HNSW index. Historical applied revisions stay unchanged. See [Alembic autogeneration hooks](https://alembic.sqlalchemy.org/en/latest/api/autogenerate.html).

## Checks

```sh
uv run --extra parsing ruff check src tests migrations scripts
uv run --extra parsing pytest tests/unit -q
npm exec --yes --package pyright@1.1.414 -- pyright
```

Pyright reads the shared configuration in `pyproject.toml` and checks all Python under `src`, `tests`, `migrations`, and `scripts` using `.venv`. The same configuration is available to Pylance; select `.venv/bin/python` in your editor. The npm command runs the standalone checker without adding an application dependency. Narrow optional values before use; use the required-resource helpers for database lookups. Do not suppress optional-value diagnostics. The settings constructor retains the documented dotenv override and validation behavior ([Pydantic settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/#dotenv-env-support)).

Integration checks require a dedicated `docvault_test` database on the local PostgreSQL instance. They create temporary schemas and never reset the application's database:

```sh
docker compose exec postgres createdb -U docvault docvault_test
RUN_INTEGRATION=1 TEST_DATABASE_URL=postgresql+psycopg://docvault:docvault@127.0.0.1:15432/docvault_test uv run --extra parsing pytest tests -q
```

In `web/`: `bun run typecheck`, `bun test src/lib`, and `bun run build`. Browser validation is manual at the user's request.

There is no extracted-token-per-file, chat-source-count, comparison-source-count, or fixed 6,000-token evidence cap. Provider context limits still apply. Upload bytes and request rates remain configurable. `DAILY_BUDGET_USD` is currently a placeholder and does not enforce a budget; its reservation implementation remains on the ledger.
