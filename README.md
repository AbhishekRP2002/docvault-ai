# DocVault LLM

A local document workspace with asynchronous PDF/DOCX/TXT ingestion, Docling HybridChunker, source-aware insights, cited chat sessions, comparisons, versions, streaming, and operational metrics. Python/FastAPI, PostgreSQL/pgvector HNSW plus full-text hybrid retrieval, RQ/Redis, LangGraph and OpenRouter power the backend; React/Vite/assistant-ui/shadcn power the frontend.

Implementation exists. Acceptance evidence and remaining work are tracked in the [progress ledger](docs/progress.md); this is not yet a fully verified submission. IAM and public hosting are deferred.

Project documents:

- [Product and technical specification](docs/spec.md)
- [Implementation plan and verification gates](docs/implementation-plan.md)
- [Document processing and RAG research](docs/research.md)
- [Implemented / verified / pending ledger](docs/progress.md)

## Local development

Copy `.env.example` to `.env` and set `OPENROUTER_API_KEY` privately. Never commit the key. The default models are `openai/gpt-4.1-mini` and `openai/text-embedding-3-small` (1536 dimensions).

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

The local runtime was exercised on Python 3.13. The supplied Docker image targets Python 3.12; full `docker compose up --build` reproduction is still pending. Parser model assets download on first use; `uv run --extra parsing python scripts/warm_parser.py /path/to/small-scanned.pdf` can warm them first.

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
```

Integration checks require a dedicated `docvault_test` database on the local PostgreSQL instance. They create temporary schemas and never reset the application's database:

```sh
docker compose exec postgres createdb -U docvault docvault_test
RUN_INTEGRATION=1 TEST_DATABASE_URL=postgresql+psycopg://docvault:docvault@127.0.0.1:15432/docvault_test uv run --extra parsing pytest tests -q
```

In `web/`: `bun run typecheck`, `bun test src/lib`, and `bun run build`. Browser validation is manual at the user's request.

There is no extracted-token-per-file, chat-source-count, comparison-source-count, or fixed 6,000-token evidence cap. Provider context limits still apply. Upload bytes and request rates remain configurable. `DAILY_BUDGET_USD` is currently a placeholder and does not enforce a budget; its reservation implementation remains on the ledger.
