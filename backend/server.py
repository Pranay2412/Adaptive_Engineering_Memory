"""CLI Runner & Entry Point for Synapse FastAPI Service (Phase 10).

Usage:
    python -m backend.server [--host 127.0.0.1] [--port 8000] [--reload]
"""

from __future__ import annotations

import argparse
import sys
import uvicorn

from .config import load_config_from_env


def main() -> None:
    parser = argparse.ArgumentParser(description="Synapse Adaptive Engineering Memory REST API Service")
    parser.add_argument("--host", default=None, help="Host network interface to bind (default: from config)")
    parser.add_argument("--port", type=int, default=None, help="Port to bind (default: from config)")
    parser.add_argument("--reload", action="store_true", help="Enable hot reloading for local development")
    parser.add_argument("--env", default=None, help="Environment override: development, staging, production, testing")

    args = parser.parse_args()

    try:
        config = load_config_from_env()
    except Exception as e:
        sys.stderr.write(f"Startup configuration error: {e}\n")
        sys.exit(1)

    host = args.host or config.host
    port = args.port or config.port
    reload = args.reload or (config.environment == "development")

    print(f"Starting Synapse API Service on {host}:{port} [env={config.environment}]...")
    uvicorn.run("backend.api:app", host=host, port=port, reload=reload, log_level=config.log_level.lower())


if __name__ == "__main__":
    main()
