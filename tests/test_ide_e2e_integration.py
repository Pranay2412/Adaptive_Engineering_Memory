"""End-to-End Cross-IDE / Client Integration Tests (Phase 6.4).

Demonstrates a realistic multi-session developer workflow:
1. Developer opens IDE, starts Session 1 on a repository.
2. Queries context and records an approved architectural decision (ADR).
3. Closes Session 1.
4. Later, developer opens IDE and starts Session 2 on the same repository.
5. Queries context: Synapse retrieves the persisted decision from Session 1!
6. Validates edge cases: repository switching, empty results, duplicate calls,
   and invalid scopes.
"""

import pytest
from starlette.testclient import TestClient

from backend.api import app, reset_protocol_engine
from backend.ide_client import SynapseIDEClient
from backend.session_protocol import (
    ErrorCode,
    ProtocolException,
    SessionProtocolEngine,
)
from backend.team_memory_store import TeamMemoryDocumentStore
from backend.session_memory import AISessionMemoryService
from backend.orchestrator import AdaptiveContextOrchestrator


@pytest.fixture
def integrated_client():
    """Build an integrated IDE client backed by real in-memory SQLite stores."""
    doc_store = TeamMemoryDocumentStore()
    session_svc = AISessionMemoryService(doc_store=doc_store)
    orchestrator = AdaptiveContextOrchestrator(session_memory_service=session_svc)
    engine = SessionProtocolEngine(
        doc_store=doc_store,
        session_service=session_svc,
        orchestrator=orchestrator,
    )
    reset_protocol_engine(engine)

    test_http = TestClient(app)
    ide_client = SynapseIDEClient(
        principal_id="engineer_bob",
        tenant_id="engineering_corp",
        allowed_repositories=["repo-auth-service", "repo-billing-service"],
        app_client=test_http,
    )

    yield ide_client
    reset_protocol_engine(None)


# ---------------------------------------------------------------------------
# 1. Primary Acceptance Test: Cross-Session Durable Memory Retention
# ---------------------------------------------------------------------------

def test_cross_session_memory_retention(integrated_client):
    repo_id = "repo-auth-service"

    # --- SESSION 1: Morning Investigation & Decision ---
    sess_1_id = integrated_client.create_session(
        repository_id=repo_id,
        title="Morning session: JWT verification architecture",
        client_type="vscode",
        assistant_name="synapse-extension",
    )
    assert sess_1_id.startswith("sess_")

    # Developer records an architectural decision in Session 1
    dec_id = integrated_client.record_decision(
        session_id=sess_1_id,
        repository_id=repo_id,
        title="ADR-002: Enforce RS256 token verification with public RSA key ring",
        rationale="RS256 asymmetric keys allow microservices to verify authenticity without knowing the private signing key.",
        scope="repository",
        category="architecture",
        impact_areas=["backend/auth.py", "backend/security.py"],
        affected_symbols=["verify_jwt_token"],
    )
    assert dec_id.startswith("dec_")

    # Also distill a session summary
    distill_res = integrated_client.distill_session(
        session_id=sess_1_id,
        repository_id=repo_id,
        prompt="Why should we migrate from HS256 to RS256?",
        response_summary="Migrated from symmetric HS256 to asymmetric RS256 to eliminate shared secret distribution vulnerabilities.",
        decisions=["Enforce RS256 token verification."],
        affected_files=["backend/auth.py"],
    )
    assert distill_res["status"] == "recorded"

    # Close Session 1
    close_res = integrated_client.close_session(
        session_id=sess_1_id,
        summary="Documented and established RS256 token verification standard.",
    )
    assert close_res["status"] == "closed"

    # --- SESSION 2: Afternoon Session (Later in time) ---
    sess_2_id = integrated_client.create_session(
        repository_id=repo_id,
        title="Afternoon session: Implementing token validation filter",
        client_type="cursor",
        assistant_name="synapse-cursor-agent",
    )
    assert sess_2_id.startswith("sess_")
    assert sess_2_id != sess_1_id

    # In Session 2, query context: Does Synapse recall what was decided in Session 1?
    query_res = integrated_client.query_context(
        session_id=sess_2_id,
        repository_id=repo_id,
        query="What algorithm is required for JWT verification in this repository?",
        max_tokens=1500,
    )

    assert query_res["success"] is True
    assert query_res["tokens_used"] > 0
    formatted_ctx = query_res["formatted_context"]

    # CRITICAL CHECK: Context in Session 2 must contain the decision from Session 1!
    assert "RS256" in formatted_ctx
    assert "Architecture Decision" in formatted_ctx or "ADR" in formatted_ctx

    # Close Session 2
    integrated_client.close_session(sess_2_id, "Completed filter using RS256.")


# ---------------------------------------------------------------------------
# 2. Scope & Repository Switching Isolation Test
# ---------------------------------------------------------------------------

def test_repository_switching_isolation(integrated_client):
    auth_repo = "repo-auth-service"
    billing_repo = "repo-billing-service"

    # Session in Auth repo stores sensitive auth decision
    sess_auth = integrated_client.create_session(repository_id=auth_repo, title="Auth Session")
    integrated_client.record_decision(
        session_id=sess_auth,
        repository_id=auth_repo,
        title="ADR-AUTH-099: Internal secret rotation policy",
        rationale="Rotate token secrets every 30 days.",
        scope="repository",
    )
    integrated_client.close_session(sess_auth, "Auth done")

    # Developer switches to Billing repo
    sess_billing = integrated_client.create_session(repository_id=billing_repo, title="Billing Session")

    # Context query in Billing repo must NOT leak Auth repo's private decision
    ctx_res = integrated_client.query_context(
        session_id=sess_billing,
        repository_id=billing_repo,
        query="What is the internal secret rotation policy?",
    )
    billing_ctx = ctx_res["formatted_context"]
    assert "ADR-AUTH-099" not in billing_ctx


# ---------------------------------------------------------------------------
# 3. Edge Case: Empty / Unrelated Result Handling
# ---------------------------------------------------------------------------

def test_empty_or_unrelated_query(integrated_client):
    repo_id = "repo-auth-service"
    sess_id = integrated_client.create_session(repository_id=repo_id, title="Unrelated Query Test")

    # Querying completely unrelated gibberish terms
    res = integrated_client.query_context(
        session_id=sess_id,
        repository_id=repo_id,
        query="quantum_warp_hyperdrive_chroniton_particles_9999",
    )
    assert res["success"] is True
    assert res["tokens_used"] >= 0
    assert res["budget_utilization_pct"] <= 100.0


# ---------------------------------------------------------------------------
# 4. Edge Case: Idempotent & Duplicate Decision Submission
# ---------------------------------------------------------------------------

def test_duplicate_decision_submission_idempotency(integrated_client):
    repo_id = "repo-auth-service"
    sess_id = integrated_client.create_session(repository_id=repo_id, title="Idempotency Test")
    idemp_key = "idemp_adr_001_unique"

    # Call 1
    dec_id_1 = integrated_client.record_decision(
        session_id=sess_id,
        repository_id=repo_id,
        title="ADR-001: Consistent Database Logging",
        rationale="Log all transactions with trace IDs.",
        idempotency_key=idemp_key,
    )

    # Call 2 (Duplicate replay)
    dec_id_2 = integrated_client.record_decision(
        session_id=sess_id,
        repository_id=repo_id,
        title="ADR-001: Consistent Database Logging",
        rationale="Log all transactions with trace IDs.",
        idempotency_key=idemp_key,
    )

    # Must return identical decision ID without duplicate record creation
    assert dec_id_1 == dec_id_2


# ---------------------------------------------------------------------------
# 5. Edge Case: Closed Session Rejection
# ---------------------------------------------------------------------------

def test_closed_session_rejection(integrated_client):
    repo_id = "repo-auth-service"
    sess_id = integrated_client.create_session(repository_id=repo_id, title="Closing Test")
    integrated_client.close_session(sess_id, "All done.")

    with pytest.raises(ProtocolException) as exc_info:
        integrated_client.query_context(
            session_id=sess_id,
            repository_id=repo_id,
            query="test query after close",
        )
    assert exc_info.value.code == ErrorCode.SESSION_CLOSED.value


# ---------------------------------------------------------------------------
# 6. Edge Case: Invalid Scope Rejection
# ---------------------------------------------------------------------------

def test_invalid_team_scope_rejection(integrated_client):
    repo_id = "repo-auth-service"
    sess_id = integrated_client.create_session(repository_id=repo_id, title="Team Scope Test")

    # Developer attempts to write decision to unknown team
    with pytest.raises(ProtocolException) as exc_info:
        integrated_client.record_decision(
            session_id=sess_id,
            repository_id=repo_id,
            title="Team Marketing decision",
            rationale="Adopt new colors",
            scope="team",
            team_id="team_marketing_unauthorized",
        )
    assert exc_info.value.code in (ErrorCode.INVALID_SCOPE.value, ErrorCode.ACCESS_DENIED.value)
