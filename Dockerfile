# syntax=docker/dockerfile:1
# One image for the worker, the dashboard and migrations (see docker-compose.yml).
# config.yaml and .env are never baked in: compose mounts the first and passes the second.

FROM ghcr.io/astral-sh/uv:0.13.5 AS uv

FROM python:3.12-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Dependencies first, so a code change doesn't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra postgres --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra postgres --no-editable

FROM python:3.12-slim
RUN groupadd --system --gid 10001 jobpulse \
    && useradd --system --uid 10001 --gid jobpulse --home-dir /app --no-create-home \
        --shell /usr/sbin/nologin jobpulse
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# The SQLite database lives here, on the jobpulse-data volume.
RUN mkdir -p /app/data && chown jobpulse:jobpulse /app/data
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
USER jobpulse
EXPOSE 8000
CMD ["jobpulse", "--help"]
