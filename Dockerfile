# syntax=docker/dockerfile:1.7
# Build stage: resolve and install dependencies with uv from the lockfile.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock ./
# Both production extras are installed so one image serves every mode; the stub
# defaults still work without any of the services they talk to.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --extra elastic --extra apm

# Runtime stage: no uv, no build tools, non-root user, writable /data volume.
FROM python:3.12-slim-bookworm
RUN useradd --system --uid 10001 --create-home app \
    && mkdir -p /data && chown app:app /data
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app app ./app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    APP_SQLITE_PATH=/data/app.db \
    APP_TELEMETRY_FILE=/data/telemetry.log
USER app
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/info')" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
