#!/bin/sh
# Run on the host; Docker Compose owns the service startup order and migrations.
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$project_dir"

if [ ! -f .env ]; then
    (umask 077; cp .env.example .env)
    printf '%s\n' 'Created .env. Set OPENROUTER_API_KEY there, then run ./start_up.sh again.'
    exit 1
fi

# Read the key without executing .env as shell code or printing its value.
if ! awk '
    /^[[:space:]]*(export[[:space:]]+)?OPENROUTER_API_KEY[[:space:]]*=/ {
        value = $0
        sub(/^[^=]*=[[:space:]]*/, "", value)
        sub(/[[:space:]]+$/, "", value)
        quote = substr(value, 1, 1)
        if (quote == "\"" || quote == sprintf("%c", 39)) {
            value = substr(value, 2)
            end = index(value, quote)
            if (end) value = substr(value, 1, end - 1)
        } else {
            sub(/[[:space:]]+#.*$/, "", value)
            if (substr(value, 1, 1) == "#") value = ""
        }
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
        configured = length(value) > 0
    }
    END { exit !configured }
' .env; then
    printf '%s\n' 'Set OPENROUTER_API_KEY in .env, then run ./start_up.sh again.' >&2
    exit 1
fi

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    printf '%s\n' 'Install Docker with the Compose plugin (Docker Desktop includes both).' >&2
    exit 1
fi

case "$(docker compose up --help)" in
    *--wait-timeout*) ;;
    *)
        printf '%s\n' 'Update Docker Compose to a version supporting up --wait --wait-timeout.' >&2
        exit 1
        ;;
esac

if ! docker info >/dev/null 2>&1; then
    printf '%s\n' 'Docker is unavailable. Start Docker Desktop or the Docker daemon and retry.' >&2
    exit 1
fi

printf '%s\n' 'Building and starting DocVault. The first build and parser model download may take several minutes.'
if ! docker compose up --build --wait --wait-timeout 300; then
    printf '%s\n' 'Startup failed. Inspect docker compose ps and docker compose logs; retry ./start_up.sh after fixing the error.' >&2
    exit 1
fi

printf '\n%s\n' 'DocVault is ready: http://127.0.0.1:5173' 'API documentation: http://127.0.0.1:8000/docs' 'Status: make status  |  Logs: make logs  |  Stop without deleting data: make stop'
