# DocVault

DocVault is a local workspace for PDF, DOCX, and TXT documents. Upload files, get summaries and key insights, compare documents, and chat with source citations. It uses React, FastAPI, PostgreSQL/pgvector, Redis, Docling, and OpenRouter.

## Setup

Install Docker with Compose (Docker Desktop includes both). No local Python or Node installation is required.

1. Copy [`.env.example`](.env.example) to `.env` if you do not already have one.
2. Set `OPENROUTER_API_KEY` in `.env` to your OpenRouter API key. Keep this file private.
3. Start from the repository root:

   ```sh
   ./start_up.sh
   ```

The script builds and starts all services, applies database migrations, and waits for them to be ready. The first build and parser model downloads can take several minutes. If `.env` is missing, the script creates it and asks you to set the key before running it again.

- Workspace: <http://127.0.0.1:5173>
- API documentation: <http://127.0.0.1:8000/docs>

Upload documents in **Files**, wait for processing to finish, then start a chat or comparison.

## Environment variables

Only `OPENROUTER_API_KEY` needs to be filled in to get started. The other values in `.env.example` already have defaults.

| Variable | Purpose | Value in `.env.example` |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | Required API key for model requests | Set your key |
| `OPENROUTER_CHAT_MODEL` | Chat and suggested follow-ups | `openai/gpt-4.1-mini` |
| `OPENROUTER_SUMMARY_MODEL` | Document summaries and insights | `openai/gpt-4.1-mini` |
| `OPENROUTER_COMPARISON_MODEL` | Document comparisons | `openai/gpt-4.1` |
| `OPENROUTER_CONVERSATION_SUMMARY_MODEL` | Condense older chat history | `openai/gpt-4.1-mini` |
| `OPENROUTER_EMBEDDING_MODEL` | Document search embeddings | `openai/text-embedding-3-small` |
| `AGENT_MAX_TOOL_ROUNDS` | Maximum tool-use rounds per answer | `20` |

Docker Compose supplies `DATABASE_URL`, `REDIS_URL`, and `STORAGE_PATH` inside the containers. The sample leaves token limits, database/health tuning, and parser settings at their code defaults. No frontend environment file is needed. Run the startup command again after changing `.env`.

## Start, stop, and logs

Run these commands from the repository root:

| Action | Command |
| --- | --- |
| Start or rebuild services | `./start_up.sh` or `make start` |
| Stop services | `make stop` or `docker compose stop` |
| Follow logs | `make logs` or `docker compose logs --follow` |
| Follow only API and worker logs | `docker compose logs --follow api worker` |
| Check service status | `make status` or `docker compose ps --all` |

Stopping keeps uploaded files and database records in Docker volumes. Ports **5173**, **8000**, **15432**, and **16379** must be available. The startup script runs on macOS/Linux or Windows through WSL/Git Bash; in PowerShell, configure `.env` and use `docker compose up --build --wait --wait-timeout 300`.

See [operations](docs/operations.md) for diagnostics and recovery, and the [specification](docs/spec.md) for technical details.
