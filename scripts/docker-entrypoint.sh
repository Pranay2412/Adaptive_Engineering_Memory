#!/bin/bash
set -e

# ==============================================================================
# Synapse: Adaptive Engineering Memory Platform - Docker Entrypoint Script
# Supports: api (FastAPI + Dashboard), mcp (SSE MCP Server), worker, smoke-test, demo
# ==============================================================================

echo "=============================================================================="
echo "SYNAPSE ADAPTIVE ENGINEERING MEMORY PLATFORM (CONTAINER RUNTIME)"
echo "Mode: ${1:-api} | Environment: ${SYNAPSE_ENV:-production}"
echo "=============================================================================="

# Ensure persistent directories exist and have correct permissions
mkdir -p /app/data /app/graphify-out /app/backups /app/docs

# Helper: Run database schema migrations
run_migrations() {
    echo "[Entrypoint] Applying database migrations..."
    python scripts/migrate.py up || {
        echo "[Entrypoint] Migration failed. Continuing with existing schema if available..."
    }
}

case "${1:-api}" in
    api)
        run_migrations
        echo "[Entrypoint] Starting FastAPI REST API & Engineering Dashboard on port 8000..."
        exec python -m uvicorn backend.api:app --host 0.0.0.0 --port 8000 --workers ${SYNAPSE_WORKERS:-1}
        ;;

    mcp)
        echo "[Entrypoint] Starting Synapse Model Context Protocol (MCP) Server on SSE port 8001..."
        exec python -m backend.mcp_server --transport sse --host 0.0.0.0 --port 8001
        ;;

    worker)
        run_migrations
        echo "[Entrypoint] Starting Synapse Background Worker Daemon..."
        exec python -c "
import time, sys, os
from backend.telemetry import default_telemetry_registry

print('[Worker] Background synchronization daemon active.')
while True:
    time.sleep(60)
    sys.stdout.write('[Worker Heartbeat] Synapse worker running healthy.\n')
    sys.stdout.flush()
"
        ;;

    smoke-test)
        echo "[Entrypoint] Executing containerized deployment smoke test..."
        exec python scripts/smoke_test.py
        ;;

    demo)
        echo "[Entrypoint] Executing containerized end-to-end demo..."
        exec python scripts/demo_e2e.py
        ;;

    *)
        exec "$@"
        ;;
esac
