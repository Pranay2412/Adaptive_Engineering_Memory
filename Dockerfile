# syntax=docker/dockerfile:1
# ==============================================================================
# Synapse: Adaptive Engineering Memory Platform - Production Dockerfile
# Multi-stage, minimal attack surface, non-root execution (Phase 17)
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

# Stage 2: Minimal Runtime
FROM python:3.12-slim AS runtime

# Set environment variables for production execution
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    SYNAPSE_HOST=0.0.0.0 \
    SYNAPSE_PORT=8000 \
    SYNAPSE_ENV=production

WORKDIR /app

# Install runtime dependencies for health checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Create unprivileged system user and group (UID/GID 10001)
RUN groupadd -g 10001 synapse && \
    useradd -u 10001 -g synapse -s /bin/bash -m synapse

# Create persistent storage directories with correct ownership
RUN mkdir -p /app/data /app/graphify-out /app/backups /app/docs && \
    chown -R synapse:synapse /app

# Copy application code and static assets
COPY --chown=synapse:synapse backend/ /app/backend/
COPY --chown=synapse:synapse scripts/ /app/scripts/
COPY --chown=synapse:synapse docs/ /app/docs/
COPY --chown=synapse:synapse .env.example /app/.env.example

# Drop privileges to non-root user
USER synapse:synapse

# Expose FastAPI HTTP service port
EXPOSE 8000

# Health check against production liveness probe
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://127.0.0.1:8000/health || exit 1

# Launch production server via uvicorn with validated startup
CMD ["python", "-m", "uvicorn", "backend.api:app", "--host", "0.0.0.0", "--port", "8000"]
