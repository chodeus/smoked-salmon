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
    sox libsox-fmt-mp3 flac mp3val curl nano vim \
    ca-certificates lame \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Keep rclone from its official release, not apt: Debian's is years old. Checksum-verified.
# No RCLONE_ prefix on these: rclone reads every RCLONE_* variable as a flag.
ARG VERSION_RCLONE=1.75.2
ARG SHA256_RCLONE=349ac8fba6ff65d6247043f1750cdcb518ec5d500ef91463a10d37c0ccdf3702
RUN curl -fsSL -o /tmp/rclone.zip \
        "https://downloads.rclone.org/v${VERSION_RCLONE}/rclone-v${VERSION_RCLONE}-linux-amd64.zip" \
    && echo "${SHA256_RCLONE}  /tmp/rclone.zip" | sha256sum -c - \
    && python3 -c "import zipfile; zipfile.ZipFile('/tmp/rclone.zip').extract('rclone-v${VERSION_RCLONE}-linux-amd64/rclone', '/tmp')" \
    && install -m 0755 "/tmp/rclone-v${VERSION_RCLONE}-linux-amd64/rclone" /usr/local/bin/rclone \
    && rm -rf /tmp/rclone.zip "/tmp/rclone-v${VERSION_RCLONE}-linux-amd64" \
    && rclone version

# The base image's own pip bundles a certifi with compromised emSign roots (GHSA-6q48-6c7w-9gw2); 26.2.2 is the
# first pip without it. salmon itself runs from the venv below.
RUN python -m pip install --no-cache-dir --upgrade "pip>=26.2.2"

# Copy the virtual environment from builder stage
COPY --from=builder /app/.venv /app/.venv

# Set environment variables for Python virtual environment
ENV PATH="/app/.venv/bin:$PATH"
# Single-mount container layout: config.toml, rclone.conf and tmp_dir all live in /config.
ENV SALMON_CONFIG_DIR=/config
ENV PYTHONDONTWRITEBYTECODE=1

# Sticky+writable so a configured relative dir can still be created but one uid
# cannot remove another's files; recursing would re-materialise the venv.
RUN set -e; \
    mkdir -p /app/.music /app/.torrents; \
    chmod 1777 /app /app/.music /app/.torrents; \
    unreadable="$(find /app \( -type f ! -perm -0004 \) -o \( -type d ! -perm -0005 \) | head -20)"; \
    if [ -n "$unreadable" ]; then echo "not world-readable:"; echo "$unreadable"; exit 1; fi

# 55155: `salmon web`. The legacy spectral viewer binds loopback inside the
# container, so publishing its port could never work.
EXPOSE 55155

# Set the entrypoint to run the 'salmon' script
ENTRYPOINT ["salmon"]
