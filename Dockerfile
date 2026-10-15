# syntax=docker/dockerfile:1
# ==============================================================================
# Synapse: Adaptive Engineering Memory Platform - Production Multi-Stage Dockerfile
# Minimal attack surface, unprivileged non-root execution, multi-service entrypoint
# ==============================================================================

# Stage 1: Build & Dependencies
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Minimal Production Runtime
FROM python:3.12-slim AS runtime

# Runtime environment settings
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    SYNAPSE_HOST=0.0.0.0 \
    SYNAPSE_PORT=8000 \
    SYNAPSE_MCP_PORT=8001 \
    SYNAPSE_SECRET_KEY=synapse-prod-secret-key-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c \
    SYNAPSE_ENV=production

WORKDIR /app

# Install minimal runtime dependencies for network health checks and backup utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    tar \
    gzip \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python site-packages from builder
COPY --from=builder /install /usr/local

# Create unprivileged system user and group (UID/GID 10001)
RUN groupadd -g 10001 synapse && \
    useradd -u 10001 -g synapse -s /bin/bash -m synapse

# Create persistent storage directories with ownership
RUN mkdir -p /app/data /app/graphify-out /app/backups /app/docs && \
    chown -R synapse:synapse /app

# Copy application modules, SDK, scripts, fixtures, and documentation
COPY --chown=synapse:synapse backend/ /app/backend/
COPY --chown=synapse:synapse scripts/ /app/scripts/
COPY --chown=synapse:synapse docs/ /app/docs/
COPY --chown=synapse:synapse synapse_sdk/ /app/synapse_sdk/
COPY --chown=synapse:synapse fixtures/ /app/fixtures/
COPY --chown=synapse:synapse benchmarks/ /app/benchmarks/
COPY --chown=synapse:synapse .env.example /app/.env.example
COPY --chown=synapse:synapse requirements.txt /app/requirements.txt
COPY --chown=synapse:synapse Dockerfile /app/Dockerfile
COPY --chown=synapse:synapse docker-compose.yml /app/docker-compose.yml
COPY --chown=synapse:synapse .dockerignore /app/.dockerignore

# Set execute permissions on entrypoint script
RUN chmod +x /app/scripts/docker-entrypoint.sh

# Drop privileges to non-root user
USER synapse:synapse

# Expose ports for FastAPI (8000) and Model Context Protocol SSE (8001)
EXPOSE 8000 8001

# Health check probe against API liveness endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://127.0.0.1:8000/health || exit 1

# Multi-service entrypoint
ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]

# Default command: launch FastAPI and Dashboard
CMD ["api"]
