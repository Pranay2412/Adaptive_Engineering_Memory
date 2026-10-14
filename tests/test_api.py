"""Integration and contract tests for the Synapse Session & Context REST API (Phase 6.2)."""

import pytest
from starlette.testclient import TestClient

from backend.api import app, get_protocol_engine, reset_protocol_engine
from backend.session_protocol import (
    AuthenticatedPrincipal,
    ErrorCode,
    SessionProtocolEngine,
)


@pytest.fixture(autouse=True)
def clean_engine():
    """Ensure a clean, isolated engine for each test."""
    test_engine = SessionProtocolEngine()
    reset_protocol_engine(test_engine)
    yield test_engine
    reset_protocol_engine(None)


@pytest.fixture
def client():
    """Starlette TestClient for exercising the FastAPI application."""
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. Health and Status Checks
# ---------------------------------------------------------------------------

def test_health_check(client):
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["service"] == "synapse-session-context-api"
    assert data["mode"] == "development-only"
    assert res.headers["X-Synapse-Mode"] == "development-only"
    assert "X-Request-ID" in res.headers
    assert "X-Response-Time-Ms" in res.headers


def test_api_v1_health_and_status(client):
    res_health = client.get("/api/v1/health")
    assert res_health.status_code == 200

    res_status = client.get("/api/v1/status")
    assert res_status.status_code == 200
    data = res_status.json()
    assert data["status"] == "ok"
    assert data["active_sessions_count"] == 0
    assert data["protocol_version"] == "1.0.0"
    assert "1.0.0" in data["supported_versions"]


# ---------------------------------------------------------------------------
# 2. Session Lifecycle Tests (Create, Resume, Close)
# ---------------------------------------------------------------------------

def test_create_session(client):
    payload = {
        "repository": {
            "repository_id": "repo-core-api",
            "name": "Core Authentication API",
            "branch": "main",
        },
        "client": {
            "client_type": "vscode",
            "client_version": "1.85.0",
            "assistant_name": "antigravity",
        },
        "title": "Investigating Auth Service",
        "ttl_seconds": 3600,
        "metadata": {"task_id": "TASK-101"},
    }
    res = client.post("/api/v1/sessions", json=payload)
    assert res.status_code == 201
    data = res.json()

    assert data["success"] is True
    assert data["session_id"].startswith("sess_")
    assert data["status"] == "active"
    assert data["repository_id"] == "repo-core-api"
    assert "expires_at" in data
    assert data["metadata"]["task_id"] == "TASK-101"


def test_resume_session(client):
    create_res = client.post(
        "/api/v1/sessions",
        json={"repository": {"repository_id": "repo-core-api"}, "title": "Resume Test"},
    )
    session_id = create_res.json()["session_id"]

    # GET /api/v1/sessions/{id}
    res_get = client.get(f"/api/v1/sessions/{session_id}")
    assert res_get.status_code == 200
    data_get = res_get.json()
    assert data_get["session_id"] == session_id
    assert data_get["status"] == "resumed"

    # POST /api/v1/sessions/{id}/resume
    res_post = client.post(f"/api/v1/sessions/{session_id}/resume", json={})
    assert res_post.status_code == 200
    assert res_post.json()["status"] == "resumed"


def test_close_session(client):
    create_res = client.post(
        "/api/v1/sessions",
        json={"repository": {"repository_id": "repo-core-api"}, "title": "Close Test"},
    )
    session_id = create_res.json()["session_id"]

    res_close = client.post(
        f"/api/v1/sessions/{session_id}/close",
        json={"summary": "Completed auth refactoring."},
    )
    assert res_close.status_code == 200
    data_close = res_close.json()
    assert data_close["status"] == "closed"
    assert "Completed auth refactoring." in data_close["summary"]

    # Attempting to query context on closed session returns 409 Conflict
    res_ctx = client.post(
        f"/api/v1/sessions/{session_id}/context",
        json={"repository_id": "repo-core-api", "query": "auth flow"},
    )
    assert res_ctx.status_code == 409
    err = res_ctx.json()
    assert err["error"]["code"] == ErrorCode.SESSION_CLOSED.value


def test_session_not_found(client):
    res = client.get("/api/v1/sessions/sess_does_not_exist_999")
    assert res.status_code == 404
    err = res.json()
    assert err["error"]["code"] == ErrorCode.SESSION_NOT_FOUND.value


# ---------------------------------------------------------------------------
# 3. Context Query Operations
# ---------------------------------------------------------------------------

def test_context_query_success(client):
    create_res = client.post(
        "/api/v1/sessions",
        json={"repository": {"repository_id": "repo-core-api"}},
    )
    session_id = create_res.json()["session_id"]

    query_payload = {
        "repository_id": "repo-core-api",
        "query": "Where could JWT verification fail and what code is involved?",
        "max_tokens": 1500,
        "optimization_mode": "deterministic",
    }
    res = client.post(f"/api/v1/sessions/{session_id}/context", json=query_payload)
    assert res.status_code == 200
    data = res.json()

    assert data["success"] is True
    assert data["session_id"] == session_id
    assert data["token_budget"] == 1500
    assert data["tokens_used"] > 0
    assert data["budget_utilization_pct"] <= 100.0
    assert "formatted_context" in data
    assert "provenance" in data
    assert data["latency_ms"] >= 0.0


def test_context_query_repository_mismatch(client):
    create_res = client.post(
        "/api/v1/sessions",
        json={"repository": {"repository_id": "repo-core-api"}},
    )
    session_id = create_res.json()["session_id"]

    # Request specifying mismatched repository
    query_payload = {
        "repository_id": "repo-frontend-mismatch",
        "query": "Render component",
    }
    res = client.post(f"/api/v1/sessions/{session_id}/context", json=query_payload)
    assert res.status_code == 409
    err = res.json()
    assert err["error"]["code"] == ErrorCode.REPOSITORY_MISMATCH.value


# ---------------------------------------------------------------------------
# 4. Scope and Header Authentication Authorization Tests
# ---------------------------------------------------------------------------

def test_unauthorized_repository_access_denied(client):
    # Principal only allowed for "repo-allowed"
    headers = {
        "X-Principal-ID": "user-restricted",
        "X-Allowed-Repositories": "repo-allowed",
    }
    payload = {
        "repository": {"repository_id": "repo-forbidden"},
        "title": "Unauthorized attempt",
    }
    res = client.post("/api/v1/sessions", json=payload, headers=headers)
    assert res.status_code == 403
    err = res.json()
    assert err["error"]["code"] == ErrorCode.ACCESS_DENIED.value


def test_team_scope_access_denied(client):
    headers = {
        "X-Principal-ID": "user-alice",
        "X-Team-IDs": "team-backend",
        "X-Allowed-Repositories": "repo-core-api",
    }
    create_res = client.post(
        "/api/v1/sessions",
        json={"repository": {"repository_id": "repo-core-api"}},
        headers=headers,
    )
    session_id = create_res.json()["session_id"]

    # Writing decision specifying unauthorized team
    dec_payload = {
        "repository_id": "repo-core-api",
        "title": "Infra design choice",
        "rationale": "Adopted k8s",
        "scope": "team",
        "team_id": "team-infra-unauthorized",
    }
    res = client.post(f"/api/v1/sessions/{session_id}/decisions", json=dec_payload, headers=headers)
    assert res.status_code == 403
    err = res.json()
    assert err["error"]["code"] == ErrorCode.INVALID_SCOPE.value


# ---------------------------------------------------------------------------
# 5. Idempotency & Conflict Tests via HTTP
# ---------------------------------------------------------------------------

def test_idempotent_session_creation_api(client):
    idemp_key = "idemp_test_http_001"
    payload = {
        "repository": {"repository_id": "repo-core-api"},
        "title": "Idempotent Creation",
        "envelope": {"idempotency_key": idemp_key},
    }

    res1 = client.post("/api/v1/sessions", json=payload)
    assert res1.status_code == 201

    res2 = client.post("/api/v1/sessions", json=payload)
    assert res2.status_code == 201

    # Returned session IDs must match exactly
    assert res1.json()["session_id"] == res2.json()["session_id"]


def test_idempotency_conflict_api(client):
    idemp_key = "idemp_conflict_http_002"
    payload1 = {
        "repository": {"repository_id": "repo-core-api"},
        "title": "First Version",
        "envelope": {"idempotency_key": idemp_key},
    }
    res1 = client.post("/api/v1/sessions", json=payload1)
    assert res1.status_code == 201

    payload2 = {
        "repository": {"repository_id": "repo-core-api"},
        "title": "Conflicting Changed Title",
        "envelope": {"idempotency_key": idemp_key},
    }
    res2 = client.post("/api/v1/sessions", json=payload2)
    assert res2.status_code == 409
    err = res2.json()
    assert err["error"]["code"] == ErrorCode.IDEMPOTENCY_CONFLICT.value


# ---------------------------------------------------------------------------
# 6. Memory Writes & Scoped Retrieval via HTTP
# ---------------------------------------------------------------------------

def test_store_decision_api(client):
    create_res = client.post(
        "/api/v1/sessions",
        json={"repository": {"repository_id": "repo-core-api"}},
    )
    session_id = create_res.json()["session_id"]

    payload = {
        "repository_id": "repo-core-api",
        "title": "RS256 JWT Token Choice",
        "rationale": "RS256 enables asymmetric token verification without sharing private secrets.",
        "scope": "repository",
        "category": "architecture",
        "impact_areas": ["backend/auth.py"],
    }
    res = client.post(f"/api/v1/sessions/{session_id}/decisions", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["success"] is True
    assert data["decision_id"].startswith("dec_")
    assert data["status"] == "stored"


def test_store_fact_api(client):
    create_res = client.post(
        "/api/v1/sessions",
        json={"repository": {"repository_id": "repo-core-api"}},
    )
    session_id = create_res.json()["session_id"]

    payload = {
        "repository_id": "repo-core-api",
        "title": "Pytest Command Path Rule",
        "content": "Run python -m pytest to avoid binary resolution conflicts.",
        "category": "recurring_solution",
        "actionable_takeaways": ["Run with python -m pytest."],
    }
    res = client.post(f"/api/v1/sessions/{session_id}/facts", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["success"] is True
    assert data["memory_id"].startswith("mem_")
    assert data["status"] == "stored"


def test_distill_session_api(client):
    create_res = client.post(
        "/api/v1/sessions",
        json={"repository": {"repository_id": "repo-core-api"}},
    )
    session_id = create_res.json()["session_id"]

    payload = {
        "repository_id": "repo-core-api",
        "prompt": "How does authentication work with JWT?",
        "response_summary": "Tokens are verified via public RSA keys using RS256 algorithm.",
        "engineering_decisions": ["Always verify JWT exp claim."],
        "affected_files": ["backend/auth.py"],
    }
    res = client.post(f"/api/v1/sessions/{session_id}/distill", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["status"] == "recorded"
    assert data["distilled_memories_count"] >= 1


def test_scoped_memories_query_api(client):
    payload = {
        "repository_id": "repo-core-api",
        "query": "authentication JWT",
        "allowed_scopes": ["repository", "team"],
        "limit": 5,
    }
    res = client.post("/api/v1/memories/query", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert "total_found" in data
    assert "memories" in data
    assert "scopes_included" in data


# ---------------------------------------------------------------------------
# 7. Secret Redaction via HTTP
# ---------------------------------------------------------------------------

def test_secret_redaction_in_api_request(client):
    payload = {
        "repository": {"repository_id": "repo-core-api"},
        "title": "Setup with sk-live1234567890abcdef12345678 key",
    }
    res = client.post("/api/v1/sessions", json=payload)
    assert res.status_code == 201
    data = res.json()

    # Retrieve created session to inspect stored title
    sess_res = client.get(f"/api/v1/sessions/{data['session_id']}")
    assert "sk-" not in sess_res.text
    assert "[REDACTED_API_KEY]" in sess_res.text
