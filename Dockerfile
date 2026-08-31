# ===========================================
# Stage 0: Web UI builder - build the Svelte SPA into salmon/webui/static
# ===========================================
FROM node:24.20.0-alpine AS webui-builder
WORKDIR /build/webui
COPY webui/package.json webui/package-lock.json ./
RUN npm ci
COPY webui/ ./
# vite outDir is ../src/salmon/webui/static (see webui/vite.config.ts)
RUN npm run build

# ===========================================
# Stage 1: Builder - Install dependencies and build the project
# ===========================================
FROM python:3.13-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set working directory
WORKDIR /app

# Set environment variables for uv optimization
ENV UV_CACHE_DIR=/opt/uv-cache
ENV UV_PYTHON_CACHE_DIR=/opt/uv-cache/python
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Copy dependency files first for better layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN --mount=type=cache,target=/opt/uv-cache \
    uv sync --locked --no-install-project --no-editable --no-dev

# Copy source code
COPY . .
COPY --from=webui-builder /build/src/salmon/webui/static src/salmon/webui/static
# Fail the build if the web UI didn't compile, rather than shipping a UI-less image.
RUN test -f src/salmon/webui/static/index.html

# Install the project in non-editable mode for production
RUN --mount=type=cache,target=/opt/uv-cache \
    uv sync --locked --no-editable --no-dev

# ===========================================
# Stage 2: Runtime - Minimal Python slim image
# ===========================================
FROM python:3.13-slim-trixie

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    sox libsox-fmt-mp3 flac mp3val curl nano vim rclone \
    ca-certificates lame \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Copy the virtual environment from builder stage
COPY --from=builder /app/.venv /app/.venv

# Set environment variables for Python virtual environment
ENV PATH="/app/.venv/bin:$PATH"
# Single-mount container layout: config.toml, rclone.conf and tmp_dir all live in /config.
ENV SALMON_CONFIG_DIR=/config
ENV PYTHONDONTWRITEBYTECODE=1

# /app itself is writable so a configured relative download/torrent dir can still
# be created; recursing would re-materialise the venv in a layer and expose it.
RUN mkdir -p /app/.music /app/.torrents && \
    chmod 777 /app /app/.music /app/.torrents && \
    unreadable="$(find /app \( -type f ! -perm -0004 \) -o \( -type d ! -perm -0005 \) | head -20)"; \
    if [ -n "$unreadable" ]; then echo "not world-readable:"; echo "$unreadable"; exit 1; fi

# 55155: `salmon web`. The legacy spectral viewer binds loopback inside the
# container, so publishing its port could never work.
EXPOSE 55155

# Set the entrypoint to run the 'salmon' script
ENTRYPOINT ["salmon"]
