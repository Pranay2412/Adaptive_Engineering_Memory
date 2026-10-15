"""Comprehensive Test Suite for Phase 10: FastAPI Service, Lifespan, Probes, Limits & Safety.

Verifies:
1. Configuration loading, typing, bounds checking, and production security guardrails.
2. Lifespan startup and shutdown lifecycle management.
3. Liveness (/health) and readiness (/ready) probes under healthy and degraded states.
4. Early rejection of oversized request payloads (HTTP 413 Payload Too Large).
5. Request timeout execution and graceful HTTP 504 Gateway Timeout responses.
6. Traceback and error message sanitization in production vs development modes.
7. Safe configuration diagnostics endpoint (/api/v1/config).
8. CLI entry point options in backend.server.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from backend.api import create_app, get_protocol_engine, reset_protocol_engine, router
from backend.config import (
    ConfigValidationError,
    DEFAULT_DEV_SECRET,
    EnvironmentMode,
    SynapseConfig,
    load_config_from_env,
)
from backend.security import TokenService
from backend.session_protocol import (
    CURRENT_PROTOCOL_VERSION,
    ErrorCode,
    SessionProtocolEngine,
)
from backend.team_memory_store import TeamMemoryDocumentStore


# ---------------------------------------------------------------------------
# 1. Configuration Validation & Production Security Tests
# ---------------------------------------------------------------------------

def test_default_config_valid():
    config = SynapseConfig()
    config.validate()
    assert config.environment == "development"
    assert config.port == 8000
    assert config.request_timeout_seconds == 30.0
    assert config.max_request_payload_bytes == 10 * 1024 * 1024


def test_invalid_port_raises():
    with pytest.raises(ConfigValidationError) as exc:
        SynapseConfig(port=0).validate()
    assert "Invalid port 0" in str(exc.value)

    with pytest.raises(ConfigValidationError) as exc:
        SynapseConfig(port=70000).validate()
    assert "Invalid port 70000" in str(exc.value)


def test_invalid_timeout_raises():
    with pytest.raises(ConfigValidationError) as exc:
        SynapseConfig(request_timeout_seconds=0).validate()
    assert "Invalid request_timeout_seconds" in str(exc.value)

    with pytest.raises(ConfigValidationError) as exc:
        SynapseConfig(request_timeout_seconds=-5.0).validate()
    assert "Invalid request_timeout_seconds" in str(exc.value)


def test_invalid_payload_limit_raises():
    with pytest.raises(ConfigValidationError) as exc:
        SynapseConfig(max_request_payload_bytes=500).validate()
    assert "Invalid max_request_payload_bytes" in str(exc.value)

    with pytest.raises(ConfigValidationError) as exc:
        SynapseConfig(max_request_payload_bytes=200 * 1024 * 1024).validate()
    assert "Invalid max_request_payload_bytes" in str(exc.value)


def test_production_default_secret_forbidden():
    config = SynapseConfig(
        environment=EnvironmentMode.PRODUCTION.value,
        secret_key=DEFAULT_DEV_SECRET,
    )
    with pytest.raises(ConfigValidationError) as exc:
        config.validate()
    assert "Default development secret_key cannot be used in production" in str(exc.value)


def test_production_short_secret_forbidden():
    config = SynapseConfig(
        environment=EnvironmentMode.PRODUCTION.value,
        secret_key="too-short-secret-key-12345",
    )
    with pytest.raises(ConfigValidationError) as exc:
        config.validate()
    assert "Production secret_key is too short" in str(exc.value)


def test_production_wildcard_cors_with_auth_forbidden():
    config = SynapseConfig(
        environment=EnvironmentMode.PRODUCTION.value,
        secret_key="a" * 32,
        require_auth=True,
        cors_allowed_origins=["*"],
    )
    with pytest.raises(ConfigValidationError) as exc:
        config.validate()
    assert "Wildcard CORS origin ('*') is forbidden in production" in str(exc.value)


def test_production_valid_configuration():
    config = SynapseConfig(
        environment=EnvironmentMode.PRODUCTION.value,
        secret_key="a" * 32,
        require_auth=True,
        cors_allowed_origins=["https://ide.company.internal"],
    )
    config.validate()  # Passes without raising


def test_safe_dict_redaction():
    config = SynapseConfig(
        secret_key="super-confidential-secret-key-phrase",
    )
    safe = config.to_safe_dict()
    assert "secret_key" not in safe
    assert safe["secret_key_configured"] is True
    assert safe["secret_key_length"] == len("super-confidential-secret-key-phrase")
    assert safe["environment"] == "development"
    assert safe["port"] == 8000


def test_load_config_from_env(monkeypatch):
    monkeypatch.setenv("SYNAPSE_ENV", "staging")
    monkeypatch.setenv("SYNAPSE_HOST", "0.0.0.0")
    monkeypatch.setenv("SYNAPSE_PORT", "9090")
    monkeypatch.setenv("SYNAPSE_REQUEST_TIMEOUT", "15.5")
    monkeypatch.setenv("SYNAPSE_MAX_PAYLOAD_BYTES", "5242880")
    monkeypatch.setenv("SYNAPSE_CORS_ORIGINS", "http://localhost:3000,http://localhost:5173")

    cfg = load_config_from_env()
    assert cfg.environment == "staging"
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9090
    assert cfg.request_timeout_seconds == 15.5
    assert cfg.max_request_payload_bytes == 5242880
    assert cfg.cors_allowed_origins == ["http://localhost:3000", "http://localhost:5173"]


# ---------------------------------------------------------------------------
# 2. Lifespan Startup & Shutdown Lifecycle Tests
# ---------------------------------------------------------------------------

def test_lifespan_lifecycle():
    config = SynapseConfig(environment="testing")
    app_instance = create_app(config)

    assert app_instance.state.ready is True

    with TestClient(app_instance) as client:
        # App is active in lifespan context
        res = client.get("/health")
        assert res.status_code == 200
        assert app_instance.state.ready is True

    # After exiting context, shutdown has run
    assert app_instance.state.ready is False


# ---------------------------------------------------------------------------
# 3. Liveness & Readiness Probe Tests
# ---------------------------------------------------------------------------

def test_health_liveness_probe():
    config = SynapseConfig(environment="staging")
    app_instance = create_app(config)
    client = TestClient(app_instance)

    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["service"] == "synapse-session-context-api"
    assert data["mode"] == "staging"
    assert data["version"] == CURRENT_PROTOCOL_VERSION

    # Root /api/v1/health alias
    res_v1 = client.get("/api/v1/health")
    assert res_v1.status_code == 200
    assert res_v1.json()["status"] == "ok"


def test_readiness_probe_healthy():
    engine = get_protocol_engine()
    reset_protocol_engine(engine)

    try:
        app_instance = create_app(SynapseConfig())
        client = TestClient(app_instance)

        res = client.get("/ready")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ready"
        assert "ok" in data["checks"]["database"]
        assert data["checks"]["orchestrator"] == "ok"

        # Alias /api/v1/ready
        res_v1 = client.get("/api/v1/ready")
        assert res_v1.status_code == 200
        assert res_v1.json()["status"] == "ready"
    finally:
        reset_protocol_engine(None)


def test_readiness_probe_database_error():
    # Mock broken document store that raises an error
    mock_store = MagicMock()
    mock_store.count_memories.side_effect = RuntimeError("Database disk I/O failure")

    broken_engine = SessionProtocolEngine(doc_store=mock_store)
    reset_protocol_engine(broken_engine)

    try:
        app_instance = create_app(SynapseConfig())
        client = TestClient(app_instance)

        res = client.get("/ready")
        assert res.status_code == 503
        data = res.json()
        assert data["status"] == "not_ready"
        assert "Database disk I/O failure" in data["checks"]["database"]
    finally:
        reset_protocol_engine(None)


def test_readiness_probe_shutdown_state():
    engine = SessionProtocolEngine(doc_store=TeamMemoryDocumentStore())
    reset_protocol_engine(engine)

    try:
        app_instance = create_app(SynapseConfig())
        client = TestClient(app_instance)

        # Manually simulate un-ready state (e.g. during graceful drain)
        app_instance.state.ready = False

        res = client.get("/ready")
        assert res.status_code == 503
        data = res.json()
        assert data["status"] == "not_ready"
    finally:
        reset_protocol_engine(None)


# ---------------------------------------------------------------------------
# 4. Payload Size Limit Enforcement (HTTP 413) Tests
# ---------------------------------------------------------------------------

def test_payload_limit_via_content_length_header():
    # Configure tiny 2KB payload limit
    config = SynapseConfig(max_request_payload_bytes=2048)
    app_instance = create_app(config)
    client = TestClient(app_instance)

    # Send a request indicating 5KB via Content-Length
    res = client.post(
        "/api/v1/sessions",
        headers={"Content-Length": "5120"},
        content=b"x" * 10,
    )
    assert res.status_code in (413, 400)
    data = res.json()
    assert data["error"]["code"] == ErrorCode.PAYLOAD_TOO_LARGE.value
    assert "exceeds limit" in data["error"]["message"]


def test_payload_limit_via_actual_body_size():
    config = SynapseConfig(max_request_payload_bytes=2048)
    app_instance = create_app(config)
    client = TestClient(app_instance)

    # 4KB payload
    large_body = b"{" + b'"data": "' + (b"A" * 4000) + b'"}'
    res = client.post(
        "/api/v1/sessions",
        content=large_body,
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code in (413, 400)
    data = res.json()
    assert data["error"]["code"] == ErrorCode.PAYLOAD_TOO_LARGE.value


# ---------------------------------------------------------------------------
# 5. Request Timeout Middleware (HTTP 504) Tests
# ---------------------------------------------------------------------------

def test_request_timeout_middleware():
    # Configure 0.1s timeout
    config = SynapseConfig(request_timeout_seconds=0.1)
    app_instance = create_app(config)

    # Add a temporary slow route to test timeout
    @app_instance.get("/test-slow-operation")
    async def slow_route():
        await asyncio.sleep(0.3)
        return {"done": True}

    client = TestClient(app_instance)
    res = client.get("/test-slow-operation")
    assert res.status_code == 504
    data = res.json()
    assert data["error"]["code"] == ErrorCode.INTERNAL_ERROR.value
    assert "timed out" in data["error"]["message"]
    assert "X-Request-ID" in res.headers
    assert "X-Response-Time-Ms" in res.headers


# ---------------------------------------------------------------------------
# 6. Error Sanitization & Production Safety Tests
# ---------------------------------------------------------------------------

def test_development_mode_exposes_internal_error_details():
    config = SynapseConfig(environment="development")
    app_instance = create_app(config)

    @app_instance.get("/test-internal-failure")
    async def failing_route():
        raise ValueError("SensitiveInternalSqlError: column 'secret_hash' failed constraint")

    client = TestClient(app_instance, raise_server_exceptions=False)
    res = client.get("/test-internal-failure")
    assert res.status_code == 500
    data = res.json()
    assert "SensitiveInternalSqlError" in data["error"]["message"]


def test_production_mode_masks_internal_error_details():
    config = SynapseConfig(
        environment=EnvironmentMode.PRODUCTION.value,
        secret_key="p" * 32,
        cors_allowed_origins=["https://secure.internal"],
    )
    app_instance = create_app(config)

    @app_instance.get("/test-internal-failure-prod")
    async def failing_route():
        raise RuntimeError("CRITICAL: Root password /etc/shadow could not be decrypted")

    client = TestClient(app_instance, raise_server_exceptions=False)
    res = client.get("/test-internal-failure-prod")
    assert res.status_code == 500
    data = res.json()
    # Sensitive detail MUST be masked
    assert "CRITICAL: Root password" not in data["error"]["message"]
    assert "contact system administrator with your request ID" in data["error"]["message"]
    assert data["error"]["code"] == ErrorCode.INTERNAL_ERROR.value
    assert data["envelope"]["request_id"] is not None


# ---------------------------------------------------------------------------
# 7. Safe Diagnostics Endpoint Tests
# ---------------------------------------------------------------------------

def test_safe_config_endpoint():
    config = SynapseConfig(
        environment="staging",
        port=8080,
        secret_key="my-super-secret-staging-key-12345",
    )
    app_instance = create_app(config)
    client = TestClient(app_instance)

    res = client.get("/api/v1/config")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    cfg = data["config"]
    assert cfg["environment"] == "staging"
    assert cfg["port"] == 8080
    assert "secret_key" not in cfg
    assert cfg["secret_key_configured"] is True


# ---------------------------------------------------------------------------
# 8. Server CLI Argument Parsing
# ---------------------------------------------------------------------------

def test_server_runner_argument_resolution(monkeypatch):
    from backend import server

    recorded_args = {}

    def mock_run(app_str, **kwargs):
        recorded_args["app"] = app_str
        recorded_args.update(kwargs)

    monkeypatch.setattr("uvicorn.run", mock_run)
    monkeypatch.setattr("sys.argv", ["server.py", "--host", "0.0.0.0", "--port", "9999", "--reload"])

    server.main()
    assert recorded_args["app"] == "backend.api:app"
    assert recorded_args["host"] == "0.0.0.0"
    assert recorded_args["port"] == 9999
    assert recorded_args["reload"] is True
