# ===========================================
# Stage 0: Web UI builder - build the Svelte SPA into salmon/webui/static
# ===========================================
FROM node:22-alpine AS webui-builder
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
    sox flac mp3val curl nano vim rclone \
    ca-certificates lame \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Copy the virtual environment from builder stage
COPY --from=builder /app/.venv /app/.venv

# Set environment variables for Python virtual environment
ENV PATH="/app/.venv/bin:$PATH"

# Ensure app directory and its contents are writable by any user
RUN mkdir -p /app/.music /app/.torrents && chmod -R 777 /app

# 55110: legacy spectral viewer during `salmon up`; 55155: `salmon web` interface
EXPOSE 55110 55155

# Set the entrypoint to run the 'salmon' script
ENTRYPOINT ["salmon"]
