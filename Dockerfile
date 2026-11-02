# syntax=docker/dockerfile:1
# jobpulse in one container: `jobpulse up` backs up and migrates the database, then serves the
# dashboard with the scheduler alongside. A new install opens the setup wizard.
# config.yaml and .env are never baked in: every infrastructure key has a default, and
# secrets come from the environment or the dashboard's Settings page.

FROM ghcr.io/astral-sh/uv:0.13.5 AS uv

FROM python:3.12-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Dependencies first, so a code change doesn't reinstall them.
COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY_NOTICES.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra postgres --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra postgres --no-editable
# The resume-similarity model (bge-small-en-v1.5, 64 MB), outside the data volume so an image
# upgrade brings its own copy.
RUN /app/.venv/bin/jobpulse embeddings download --cache-dir /opt/jobpulse/models \
    && chmod -R a+rX /opt/jobpulse/models

FROM python:3.12-slim
RUN groupadd --system --gid 10001 jobpulse \
    && useradd --system --uid 10001 --gid jobpulse --home-dir /app --no-create-home \
        --shell /usr/sbin/nologin jobpulse
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /opt/jobpulse/models /opt/jobpulse/models
# The SQLite database and its backups live here, on the data volume.
RUN mkdir -p /app/data && chown jobpulse:jobpulse /app/data
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    JOBPULSE_MODELS_DIR=/opt/jobpulse/models \
    ORT_DISABLE_TELEMETRY=1 \
    HF_HUB_DISABLE_TELEMETRY=1
USER jobpulse
VOLUME ["/app/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
CMD ["jobpulse", "up", "--host", "0.0.0.0"]
