FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock ./
# Allow slow downloads of the parsing dependencies during image builds.
ARG UV_HTTP_TIMEOUT=300
RUN uv sync --frozen --extra parsing --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --extra parsing --no-dev
EXPOSE 8000
