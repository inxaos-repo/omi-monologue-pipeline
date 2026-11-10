# omi-monologue-pipeline: HTTP ingest (ESP32 BLE bridges) + intake watcher in one container.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra watch

FROM python:3.15-slim-bookworm
RUN useradd -u 10001 -m omi && mkdir -p /data/intake /data/archive && chown -R omi /data
COPY --from=build /app /app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    OMI_INTAKE_DIR=/data/intake \
    OMI_ARCHIVE_DIR=/data/archive \
    OMI_INGEST_PORT=8080 \
    OMI_WIKI_MODE=graphql
USER 10001
WORKDIR /data
EXPOSE 8080
HEALTHCHECK CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"OMI_INGEST_PORT\"]}/healthz',timeout=3)"
ENTRYPOINT ["omi-pipeline"]
CMD ["--serve", "/data/intake", "--interval", "60", "--settle", "15"]
