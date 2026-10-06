# DocVault

DocVault is a local workspace for PDF, DOCX, and TXT documents. Upload files, get summaries and key insights, compare documents, and chat with source citations. It uses React, FastAPI, PostgreSQL/pgvector, Redis, Docling, and OpenRouter.

## Design choices and assumptions

See [design-decisions.md](docs/design-decisions.md) for why we chose OpenRouter, Docling, LangGraph, hybrid retrieval, background jobs, and immutable source versions, along with assignment assumptions and the planned LiteParse evaluation.

## Setup

Install Docker with Compose (Docker Desktop includes both). No local Python or Node installation is required.

1. Copy [`.env.example`](.env.example) to `.env` if you do not already have one.
2. Set `OPENROUTER_API_KEY` in `.env` to your OpenRouter API key. Keep this file private.
3. Start from the repository root:

   ```sh
   ./start_up.sh
   ```

The script builds and starts all services, applies database migrations, and waits for them to be ready. The first build and parser model downloads can take several minutes. If `.env` is missing, the script creates it and asks you to set the key before running it again.

Dependency downloads during Docker builds use a 300-second HTTP timeout. If a download still times out, retry `./start_up.sh` once the connection is stable.

- Workspace: <http://127.0.0.1:5173>
- API documentation: <http://127.0.0.1:8000/docs>

Upload documents in **Files**, wait for processing to finish, then start a chat or comparison.

## Environment variables

Only `OPENROUTER_API_KEY` needs to be filled in to get started. The other values in `.env.example` already have defaults.

Model defaults live in [src/docvault/llm/config.py](src/docvault/llm/config.py). Values in `.env` override them; process environment variables override `.env`. Restart services after changing configuration. The table lists the values supplied by `.env.example`.

| Variable | Purpose | Value in `.env.example` |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | Required API key for model requests | Set your key |
| `OPENROUTER_CHAT_MODEL` | Chat and suggested follow-ups | `openai/gpt-6-luna` |
| `OPENROUTER_SUMMARY_MODEL` | Document summaries and insights | `openai/gpt-5.6-luna` |
| `OPENROUTER_COMPARISON_MODEL` | Document comparisons | `openai/gpt-5.6-luna` |
| `OPENROUTER_CONVERSATION_SUMMARY_MODEL` | Condense older chat history | `openai/gpt-5.6-luna` |
| `OPENROUTER_EMBEDDING_MODEL` | Document search embeddings | `openai/text-embedding-3-small` |
| `OPENROUTER_CHAT_REASONING_EFFORT` | Chat reasoning effort | `medium` |
| `OPENROUTER_SUMMARY_REASONING_EFFORT` | Summary reasoning effort | `medium` |
| `OPENROUTER_COMPARISON_REASONING_EFFORT` | Comparison reasoning effort | `medium` |
| `OPENROUTER_CONVERSATION_SUMMARY_REASONING_EFFORT` | Conversation memory reasoning effort | `medium` |
| `AGENT_MAX_TOOL_ROUNDS` | Maximum tool-use rounds per answer | `20` |

Both Luna models use `medium` reasoning by default. Supported effort values are `none`, `low`, `medium`, `high`, `xhigh`, and `max`; `none` disables reasoning. Reasoning and the visible answer share the completion budget (4,096 tokens, or 2,048 for conversation memory). Higher effort can increase latency and leave less room for the answer. If switching to a model without reasoning support, remove its effort override. See [OpenRouter's reasoning documentation](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).

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
