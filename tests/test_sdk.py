"""Comprehensive Test Suite for Synapse Python SDK (Phase 11).

Tests:
1. ClientConfig initialization, bounds checking, and authentication header resolution.
2. Connection lifecycle and context manager cleanup.
3. Complete API operation success paths using MockTransport.
4. Structured exception mapping across all HTTP status codes (400, 401, 403, 404, 409, 413, 429, 500, 504).
5. Strict idempotency-aware safe retry policies (never retries non-idempotent writes; retries idempotent calls).
6. End-to-end integration tests using in-process FastAPI application transport.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest
from starlette.testclient import TestClient

from backend.api import create_app, reset_protocol_engine
from backend.config import SynapseConfig
from synapse_sdk import (
    AccessDeniedError,
    AuthenticationError,
    ClientConfig,
    ConflictError,
    ContextResult,
    DecisionRecord,
    DistillationResult,
    FactRecord,
    HealthStatus,
    IdempotencyConflictError,
    InvalidScopeError,
    NotFoundError,
    PayloadTooLargeError,
    RateLimitError,
    ReadinessStatus,
    ScopedQueryResult,
    ServerError,
    SessionInfo,
    SynapseApiError,
    SynapseClient,
    SynapseConnectionError,
    SynapseError,
    SynapseTimeoutError,
    ValidationError,
)


# ---------------------------------------------------------------------------
# 1. Configuration & Auth Header Resolution Tests
# ---------------------------------------------------------------------------

def test_client_config_defaults():
    config = ClientConfig()
    assert config.base_url == "http://localhost:8000"
    assert config.timeout == 30.0
    assert config.max_retries == 3
    assert config.backoff_factor == 0.5


def test_client_config_validation():
    with pytest.raises(ValueError):
        ClientConfig(timeout=0)

    with pytest.raises(ValueError):
        ClientConfig(max_retries=-1)

    with pytest.raises(ValueError):
        ClientConfig(backoff_factor=-0.1)


def test_auth_headers_bearer_token():
    config = ClientConfig(bearer_token="my-jwt-bearer-token")
    headers = config.get_auth_headers()
    assert headers["Authorization"] == "Bearer my-jwt-bearer-token"
    assert "X-API-Key" not in headers


def test_auth_headers_api_key():
    config = ClientConfig(api_key="synapse_sk_test1234567890")
    headers = config.get_auth_headers()
    assert headers["X-API-Key"] == "synapse_sk_test1234567890"
    assert "Authorization" not in headers


def test_auth_headers_dev_simulation():
    config = ClientConfig(
        default_principal_id="alice",
        default_tenant_id="tenant-acme",
        default_allowed_repositories=["repo-a", "repo-b"],
        default_roles=["maintainer"],
    )
    headers = config.get_auth_headers()
    assert headers["X-Principal-ID"] == "alice"
    assert headers["X-Tenant-ID"] == "tenant-acme"
    assert headers["X-Allowed-Repositories"] == "repo-a,repo-b"
    assert headers["X-Roles"] == "maintainer"


# ---------------------------------------------------------------------------
# 2. Client Lifecycle & Context Manager Tests
# ---------------------------------------------------------------------------

def test_client_context_manager():
    client = SynapseClient(base_url="http://localhost:8000")
    assert not client._is_closed

    with client as c:
        assert c is client
        assert not c._is_closed

    assert client._is_closed

    with pytest.raises(SynapseError) as exc:
        client.check_health()
    assert "is closed" in str(exc.value)


def test_client_double_close():
    client = SynapseClient()
    client.close()
    client.close()  # Safe idempotent close
    assert client._is_closed


# ---------------------------------------------------------------------------
# 3. Mock Transport Success Path Tests
# ---------------------------------------------------------------------------

def test_mock_check_health():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/health"
        content = {
            "status": "ok",
            "service": "synapse-session-context-api",
            "version": "1.0.0",
            "mode": "development-only",
            "timestamp": "2026-10-15T00:00:00Z",
        }
        return httpx.Response(200, json=content)

    transport = httpx.MockTransport(handler)
    client = SynapseClient(transport=transport)
    health = client.check_health()
    assert isinstance(health, HealthStatus)
    assert health.status == "ok"
    assert health.service == "synapse-session-context-api"
    assert health.version == "1.0.0"


def test_mock_check_readiness():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/ready"
        content = {
            "status": "ready",
            "service": "synapse-session-context-api",
            "checks": {"database": "ok (10 memories)", "orchestrator": "ok"},
            "timestamp": "2026-10-15T00:00:00Z",
        }
        return httpx.Response(200, json=content)

    transport = httpx.MockTransport(handler)
    client = SynapseClient(transport=transport)
    readiness = client.check_readiness()
    assert isinstance(readiness, ReadinessStatus)
    assert readiness.status == "ready"
    assert readiness.checks["database"] == "ok (10 memories)"


def test_mock_create_session():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/sessions"
        body = json.loads(request.content)
        assert body["repository"]["repository_id"] == "repo-core"
        assert body["title"] == "Refactor API"
        content = {
            "success": True,
            "session_id": "sess_12345",
            "repository": {"repository_id": "repo-core"},
            "status": "active",
            "created_at": "2026-10-15T00:00:00Z",
            "ttl_seconds": 86400,
            "title": "Refactor API",
        }
        return httpx.Response(201, json=content)

    transport = httpx.MockTransport(handler)
    client = SynapseClient(transport=transport)
    sess = client.create_session("repo-core", title="Refactor API")
    assert isinstance(sess, SessionInfo)
    assert sess.session_id == "sess_12345"
    assert sess.repository_id == "repo-core"
    assert sess.status == "active"


def test_mock_query_context():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/sessions/sess_123/context"
        content = {
            "success": True,
            "query": "authentication flow",
            "budget_allocated": 1500,
            "tokens_used": 340,
            "budget_utilization": 0.2267,
            "items": [
                {
                    "id": "mem_1",
                    "type": "decision",
                    "source": "memory",
                    "content": "Use RS256 JWT tokens",
                    "tokens": 40,
                    "score": 0.95,
                    "provenance": {"category": "architecture"},
                }
            ],
            "raw_context": "Use RS256 JWT tokens",
            "timing_ms": 12.5,
            "orchestration_mode": "deterministic",
        }
        return httpx.Response(200, json=content)

    transport = httpx.MockTransport(handler)
    client = SynapseClient(transport=transport)
    ctx = client.query_context("sess_123", "repo-core", "authentication flow")
    assert isinstance(ctx, ContextResult)
    assert ctx.query == "authentication flow"
    assert ctx.tokens_used == 340
    assert len(ctx.items) == 1
    assert ctx.items[0].content == "Use RS256 JWT tokens"


def test_mock_store_decision_and_fact():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/decisions" in request.url.path:
            return httpx.Response(
                201,
                json={"success": True, "decision_id": "dec_999", "status": "stored", "scope": "repository"},
            )
        elif "/facts" in request.url.path:
            return httpx.Response(
                201,
                json={"success": True, "memory_id": "mem_888", "status": "stored"},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = SynapseClient(transport=transport)

    dec = client.store_decision("sess_1", "repo-1", "Use SQLite", "Simple embedded storage")
    assert isinstance(dec, DecisionRecord)
    assert dec.decision_id == "dec_999"
    assert dec.title == "Use SQLite"

    fact = client.store_fact("sess_1", "repo-1", "DB WAL mode", "PRAGMA journal_mode=WAL")
    assert isinstance(fact, FactRecord)
    assert fact.memory_id == "mem_888"
    assert fact.fact_id == "mem_888"
    assert fact.title == "DB WAL mode"


def test_mock_distill_and_query_memories():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/distill" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "status": "recorded",
                    "distilled_memories_count": 2,
                    "memory_ids": ["mem_101", "mem_102"],
                },
            )
        elif "/memories/query" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "total_found": 1,
                    "scopes_included": ["repository", "team"],
                    "memories": [
                        {
                            "memory_id": "mem_101",
                            "title": "WAL Mode",
                            "content": "Enable WAL mode",
                            "scope": "repository",
                            "category": "implementation_summary",
                            "relevance_score": 0.88,
                        }
                    ],
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = SynapseClient(transport=transport)

    dist = client.distill_session("sess_1", "repo-1", "How to optimize SQLite?", "Enable WAL mode.")
    assert isinstance(dist, DistillationResult)
    assert dist.distilled_memories_count == 2
    assert "mem_101" in dist.memory_ids

    q_res = client.query_memories("repo-1", "SQLite optimize")
    assert isinstance(q_res, ScopedQueryResult)
    assert q_res.total_found == 1
    assert q_res.memories[0].title == "WAL Mode"


# ---------------------------------------------------------------------------
# 4. Exception Mapping Tests (Status Code Coverage)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status_code,err_code,expected_exc",
    [
        (400, "MALFORMED_INPUT", ValidationError),
        (401, "AUTHENTICATION_REQUIRED", AuthenticationError),
        (403, "ACCESS_DENIED", AccessDeniedError),
        (403, "INVALID_SCOPE", InvalidScopeError),
        (404, "SESSION_NOT_FOUND", NotFoundError),
        (409, "SESSION_CLOSED", ConflictError),
        (409, "IDEMPOTENCY_CONFLICT", IdempotencyConflictError),
        (413, "PAYLOAD_TOO_LARGE", PayloadTooLargeError),
        (422, "UNSUPPORTED_PROTOCOL_VERSION", ValidationError),
        (429, "RATE_LIMITED", RateLimitError),
        (500, "INTERNAL_ERROR", ServerError),
    ],
)
def test_exception_mapping_matrix(status_code, err_code, expected_exc):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={
                "envelope": {"request_id": "req_err_test"},
                "error": {"code": err_code, "message": f"Simulated {err_code} error", "retryable": False},
            },
        )

    transport = httpx.MockTransport(handler)
    client = SynapseClient(transport=transport)

    with pytest.raises(expected_exc) as exc_info:
        client.check_health()

    assert exc_info.value.status_code == status_code
    assert exc_info.value.error_code == err_code
    assert exc_info.value.request_id == "req_err_test"


# ---------------------------------------------------------------------------
# 5. Safe Retry Policy Tests
# ---------------------------------------------------------------------------

def test_idempotent_get_retries_on_503_and_succeeds():
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(
                503,
                json={"envelope": {"request_id": "req_retry"}, "error": {"code": "SERVICE_UNAVAILABLE", "retryable": True}},
            )
        return httpx.Response(200, json={"status": "ok", "service": "synapse"})

    transport = httpx.MockTransport(handler)
    client = SynapseClient(
        config=ClientConfig(max_retries=3, backoff_factor=0.01),
        transport=transport,
    )

    res = client.check_health()
    assert res.status == "ok"
    assert call_count == 3  # Succeeded on 3rd attempt after 2 retries


def test_non_idempotent_write_does_not_retry_on_failure():
    """CRITICAL: Mutating POST requests without idempotency_key must NEVER be blindly retried."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            503,
            json={"envelope": {"request_id": "req_no_retry"}, "error": {"code": "SERVICE_UNAVAILABLE", "retryable": True}},
        )

    transport = httpx.MockTransport(handler)
    client = SynapseClient(
        config=ClientConfig(max_retries=3, backoff_factor=0.01),
        transport=transport,
    )

    # Calling mutating create_session without an idempotency key
    with pytest.raises(ServerError):
        client.create_session("repo-1", title="Test Session")

    # MUST have executed exactly once without automatic retry
    assert call_count == 1


def test_idempotent_write_with_key_retries_on_failure():
    """When an idempotency_key is explicitly provided, retrying is safe and permitted."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            return httpx.Response(
                503,
                json={"envelope": {"request_id": "req_with_key"}, "error": {"code": "SERVICE_UNAVAILABLE", "retryable": True}},
            )
        return httpx.Response(
            201,
            json={"success": True, "session_id": "sess_safe", "status": "active"},
        )

    transport = httpx.MockTransport(handler)
    client = SynapseClient(
        config=ClientConfig(max_retries=3, backoff_factor=0.01),
        transport=transport,
    )

    sess = client.create_session("repo-1", title="Safe Retry Session", idempotency_key="key_12345")
    assert sess.session_id == "sess_safe"
    assert call_count == 2


# ---------------------------------------------------------------------------
# 6. End-to-End Integration Tests (Using in-process FastAPI application)
# ---------------------------------------------------------------------------

def test_sdk_e2e_against_fastapi_service():
    """Verify end-to-end integration across all operations using TestClient transport."""
    app_instance = create_app(SynapseConfig())
    tc = TestClient(app_instance)

    config = ClientConfig(
        base_url="http://testserver",
        default_principal_id="integration-tester",
        default_tenant_id="test-org",
        default_allowed_repositories=["repo-e2e"],
        default_roles=["developer", "admin"],
    )

    with SynapseClient(config=config, transport=tc._transport) as client:
        # 1. Probes
        health = client.check_health()
        assert health.status == "ok"

        readiness = client.check_readiness()
        assert readiness.status == "ready"

        # 2. Session creation
        session = client.create_session(
            repository_id="repo-e2e",
            title="SDK End to End Test",
            client_type="pytest",
        )
        assert session.session_id.startswith("sess_")
        assert session.status == "active"

        # 3. Store Decision
        dec = client.store_decision(
            session_id=session.session_id,
            repository_id="repo-e2e",
            title="Adopt Client SDK",
            rationale="Eliminates direct internal backend imports across clients.",
            category="architecture",
        )
        assert dec.decision_id.startswith("dec_")

        # 4. Store Fact
        fact = client.store_fact(
            session_id=session.session_id,
            repository_id="repo-e2e",
            title="SDK Retry Rule",
            content="Never retry non-idempotent writes without an idempotency key.",
        )
        assert fact.memory_id.startswith("mem_")

        # 5. Query Context
        ctx = client.query_context(
            session_id=session.session_id,
            repository_id="repo-e2e",
            query="Client SDK retry rule",
            max_tokens=800,
        )
        assert ctx.query == "Client SDK retry rule"
        assert ctx.budget_allocated == 800

        # 6. Distill
        dist = client.distill_session(
            session_id=session.session_id,
            repository_id="repo-e2e",
            prompt="How does the SDK handle retries?",
            response_summary="Idempotency-aware retries with backoff.",
            affected_files=["synapse_sdk/client.py"],
        )
        assert dist.success is True

        # 7. Query Memories
        mem_res = client.query_memories(
            repository_id="repo-e2e",
            query="SDK retry",
        )
        assert mem_res.success is True
        assert mem_res.total_found >= 1

        # 8. Close Session
        closed = client.close_session(session.session_id, summary="E2E test complete")
        assert closed.status == "closed"
