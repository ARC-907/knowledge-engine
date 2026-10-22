# Container image for the knowledge-engine HTTP server.
#
# Build:  docker build -t knowledge-engine:local .
# Run:    docker run --rm -p 127.0.0.1:9210:9210 \
#           -v "$PWD/corpus:/data/corpus:ro" knowledge-engine:local
#
# The corpus is mounted at /data/corpus; indexes and other runtime data live
# under /data/.knowledge-engine (mount a volume there to keep them). See
# 01-user-docs/DEPLOYMENT.md ("Docker") and compose.yaml.

ARG BUILDER_IMAGE=ghcr.io/astral-sh/uv:python3.12-bookworm-slim
ARG RUNTIME_IMAGE=python:3.12-slim-bookworm

# ---------------------------------------------------------------------------
# Builder: resolve the locked dependency set and install the package
# (non-editable) into a self-contained virtual environment at /opt/venv.
# ---------------------------------------------------------------------------
FROM ${BUILDER_IMAGE} AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /src/engine

# Dependencies first so this layer is reused when only the source changes.
COPY engine/pyproject.toml engine/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-editable

# Then the project itself.
COPY engine/ ./
RUN uv sync --locked --no-dev --no-editable \
    && /opt/venv/bin/knowledge-engine --help > /dev/null

# ---------------------------------------------------------------------------
# Runtime: slim Python, no build tooling, unprivileged user.
# ---------------------------------------------------------------------------
FROM ${RUNTIME_IMAGE} AS runtime

ARG KE_UID=10001
ARG KE_GID=10001

LABEL org.opencontainers.image.title="knowledge-engine" \
      org.opencontainers.image.description="Self-hosted, MCP-native knowledge engine (HTTP server)" \
      org.opencontainers.image.source="https://github.com/ARC-907/knowledge-engine" \
      org.opencontainers.image.licenses="MIT"

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KE_HOME=/data \
    KE_CORPUS_ROOT=/data/corpus \
    KE_DATA_DIR=/data/.knowledge-engine/data

RUN groupadd --system --gid "${KE_GID}" ke \
    && useradd --system --uid "${KE_UID}" --gid ke --home-dir /data \
         --no-create-home --shell /usr/sbin/nologin ke \
    && mkdir -p /data/corpus /data/.knowledge-engine/data \
    && chown -R ke:ke /data

# The virtual environment stays root-owned (read-only for the service user).
COPY --from=builder /opt/venv /opt/venv

USER ke
WORKDIR /data
VOLUME ["/data"]
EXPOSE 9210

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9210/health', timeout=3).status==200 else 1)"

ENTRYPOINT ["knowledge-engine"]
CMD ["serve", "--host", "0.0.0.0", "--port", "9210"]
