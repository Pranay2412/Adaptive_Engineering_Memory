"""Unit and contract tests for the Provider-Neutral Session Protocol (Phase 6.1)."""

import pytest
from datetime import datetime, timezone

from backend.session_protocol import (
    AccessDeniedError,
    AuthenticatedPrincipal,
    AuthenticationRequiredError,
    ClientIdentity,
    ClientType,
    ContextQueryRequest,
    ContextQueryResponse,
    CURRENT_PROTOCOL_VERSION,
    ErrorCode,
    IdempotencyConflictError,
    InvalidScopeError,
    MalformedInputError,
    MemoryScope,
    PayloadTooLargeError,
    ProtocolEnvelope,
    ProtocolErrorDetail,
    ProtocolErrorResponse,
    RepositoryIdentity,
    RepositoryMismatchError,
    SecretRedactor,
    SessionCloseRequest,
    SessionCloseResponse,
    SessionClosedError,
    SessionCreateRequest,
    SessionCreateResponse,
    SessionExpiredError,
    SessionNotFoundError,
    SessionProtocolEngine,
    SessionResumeRequest,
    SessionResumeResponse,
    SessionStatus,
    StoreDecisionRequest,
    StoreDecisionResponse,
    StoreFactRequest,
    StoreFactResponse,
    StoreSessionSummaryRequest,
    StoreSessionSummaryResponse,
    ScopedRetrievalRequest,
    ScopedRetrievalResponse,
    UnsupportedProtocolVersionError,
    utc_now_iso,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def test_principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        principal_id="user_alice_123",
        tenant_id="tenant_acme_corp",
        team_ids=["team_backend", "team_infra"],
        allowed_repositories=["repo-core-api", "repo-frontend"],
        roles=["senior_developer"],
        is_authenticated=True,
    )


@pytest.fixture
def unauthorized_principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        principal_id="user_mallory_999",
        tenant_id="tenant_other_corp",
        team_ids=["team_external"],
        allowed_repositories=["repo-external-only"],
        roles=["guest"],
        is_authenticated=True,
    )


@pytest.fixture
def test_client() -> ClientIdentity:
    return ClientIdentity(
        client_type=ClientType.VSCODE.value,
        client_version="1.85.0",
        assistant_name="antigravity",
        assistant_version="2.1.0",
        environment={"os": "windows", "node": "20.10.0"},
    )


@pytest.fixture
def test_repo() -> RepositoryIdentity:
    return RepositoryIdentity(
        repository_id="repo-core-api",
        name="Core API Microservice",
        branch="main",
        commit_sha="a1b2c3d4e5f6",
    )


@pytest.fixture
def engine() -> SessionProtocolEngine:
    return SessionProtocolEngine()


# ---------------------------------------------------------------------------
# 1. Protocol Envelope & Versioning Tests
# ---------------------------------------------------------------------------

def test_protocol_envelope_defaults():
    env = ProtocolEnvelope()
    assert env.protocol_version == CURRENT_PROTOCOL_VERSION
    assert env.request_id.startswith("req_")
    assert env.timestamp is not None
    assert env.idempotency_key is None
    env.validate()


def test_protocol_envelope_unsupported_version():
    env = ProtocolEnvelope(protocol_version="0.9.0")
    with pytest.raises(UnsupportedProtocolVersionError) as exc_info:
        env.validate()
    assert exc_info.value.code == ErrorCode.UNSUPPORTED_PROTOCOL_VERSION.value
    assert "0.9.0" in exc_info.value.message


def test_protocol_envelope_empty_request_id():
    env = ProtocolEnvelope(request_id="   ")
    with pytest.raises(MalformedInputError) as exc_info:
        env.validate()
    assert exc_info.value.field == "request_id"


# ---------------------------------------------------------------------------
# 2. Secret Redaction / Privacy Protection Tests
# ---------------------------------------------------------------------------

def test_secret_redactor_openai_key():
    raw = "Connecting to LLM using sk-proj1234567890abcdef1234567890 securely."
    redacted = SecretRedactor.redact(raw)
    assert "sk-" not in redacted
    assert "[REDACTED_API_KEY]" in redacted


def test_secret_redactor_anthropic_key():
    raw = "Claude API client initialized with sk-ant-api03-abcdef1234567890abcdef."
    redacted = SecretRedactor.redact(raw)
    assert "sk-ant-" not in redacted
    assert "[REDACTED_API_KEY]" in redacted


def test_secret_redactor_github_pat():
    raw = "Cloning repo with token ghp_1234567890abcdef1234567890abcdef1234."
    redacted = SecretRedactor.redact(raw)
    assert "ghp_" not in redacted
    assert "[REDACTED_GITHUB_TOKEN]" in redacted


def test_secret_redactor_aws_credentials():
    raw = "Deploying to S3 with AKIAIOSFODNN7EXAMPLE and aws_secret_access_key='wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'."
    redacted = SecretRedactor.redact(raw)
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted
    assert "[REDACTED_AWS_KEY]" in redacted
    assert "[REDACTED_AWS_SECRET]" in redacted


def test_secret_redactor_jwt():
    raw = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.doNotLeakThisSignature12345"
    redacted = SecretRedactor.redact(raw)
    assert "eyJhbG" not in redacted
    assert "[REDACTED_JWT_TOKEN]" in redacted


def test_secret_redactor_private_key():
    raw = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0Y...\n-----END RSA PRIVATE KEY-----"
    redacted = SecretRedactor.redact(raw)
    assert "-----BEGIN RSA PRIVATE KEY-----" not in redacted
    assert "[REDACTED_PRIVATE_KEY]" in redacted


def test_secret_redactor_nested_dict():
    nested = {
        "headers": {"Authorization": "Bearer sk-secret1234567890abcdef123456"},
        "config": ["clean_val", "ghp_1234567890abcdef1234567890abcdef1234"],
    }
    redacted = SecretRedactor.redact_dict(nested)
    assert "[REDACTED_API_KEY]" in redacted["headers"]["Authorization"]
    assert "[REDACTED_GITHUB_TOKEN]" in redacted["config"][1]


# ---------------------------------------------------------------------------
# 3. Session Lifecycle Tests (Create, Resume, Close)
# ---------------------------------------------------------------------------

def test_session_create_success(engine, test_principal, test_repo, test_client):
    req = SessionCreateRequest(
        envelope=ProtocolEnvelope(),
        repository=test_repo,
        client=test_client,
        title="Refactoring Auth Service",
        ttl_seconds=3600,
        metadata={"feature_flag": "auth_v2"},
    )
    res = engine.create_session(test_principal, req)

    assert res.success is True
    assert res.session_id.startswith("sess_")
    assert res.status == SessionStatus.ACTIVE.value
    assert res.repository_id == "repo-core-api"
    assert res.expires_at > res.created_at
    assert res.metadata.get("feature_flag") == "auth_v2"


def test_session_resume_success(engine, test_principal, test_repo, test_client):
    create_req = SessionCreateRequest(
        envelope=ProtocolEnvelope(),
        repository=test_repo,
        client=test_client,
        title="Session to Resume",
    )
    create_res = engine.create_session(test_principal, create_req)

    resume_req = SessionResumeRequest(
        envelope=ProtocolEnvelope(),
        session_id=create_res.session_id,
        client=test_client,
    )
    resume_res = engine.resume_session(test_principal, resume_req)

    assert resume_res.success is True
    assert resume_res.session_id == create_res.session_id
    assert resume_res.status == SessionStatus.RESUMED.value
    assert resume_res.repository_id == test_repo.repository_id


def test_session_resume_not_found(engine, test_principal, test_client):
    resume_req = SessionResumeRequest(
        envelope=ProtocolEnvelope(),
        session_id="sess_non_existent_999",
        client=test_client,
    )
    with pytest.raises(SessionNotFoundError) as exc_info:
        engine.resume_session(test_principal, resume_req)
    assert exc_info.value.code == ErrorCode.SESSION_NOT_FOUND.value


def test_session_close_success(engine, test_principal, test_repo, test_client):
    create_req = SessionCreateRequest(
        envelope=ProtocolEnvelope(),
        repository=test_repo,
        client=test_client,
        title="Session to Close",
    )
    create_res = engine.create_session(test_principal, create_req)

    close_req = SessionCloseRequest(
        envelope=ProtocolEnvelope(),
        session_id=create_res.session_id,
        summary="Finished migrating JWT to RS256.",
    )
    close_res = engine.close_session(test_principal, close_req)

    assert close_res.success is True
    assert close_res.session_id == create_res.session_id
    assert close_res.status == SessionStatus.CLOSED.value
    assert "RS256" in close_res.summary


def test_operation_on_closed_session_fails(engine, test_principal, test_repo, test_client):
    create_req = SessionCreateRequest(
        envelope=ProtocolEnvelope(),
        repository=test_repo,
        client=test_client,
    )
    create_res = engine.create_session(test_principal, create_req)

    engine.close_session(
        test_principal,
        SessionCloseRequest(envelope=ProtocolEnvelope(), session_id=create_res.session_id),
    )

    # Attempting to query context on closed session
    query_req = ContextQueryRequest(
        envelope=ProtocolEnvelope(),
        session_id=create_res.session_id,
        repository_id=test_repo.repository_id,
        query="How does auth work?",
    )
    with pytest.raises(SessionClosedError) as exc_info:
        engine.query_context(test_principal, query_req)
    assert exc_info.value.code == ErrorCode.SESSION_CLOSED.value


def test_operation_on_expired_session_fails(engine, test_principal, test_repo, test_client):
    create_req = SessionCreateRequest(
        envelope=ProtocolEnvelope(),
        repository=test_repo,
        client=test_client,
        ttl_seconds=1,  # 1 second TTL
    )
    create_res = engine.create_session(test_principal, create_req)

    # Manually backdate the session's expires_at to simulate expiration
    record = engine._sessions[create_res.session_id]
    record.expires_at = "2020-01-01T00:00:00+00:00"

    query_req = ContextQueryRequest(
        envelope=ProtocolEnvelope(),
        session_id=create_res.session_id,
        repository_id=test_repo.repository_id,
        query="How does auth work?",
    )
    with pytest.raises(SessionExpiredError) as exc_info:
        engine.query_context(test_principal, query_req)
    assert exc_info.value.code == ErrorCode.SESSION_EXPIRED.value


# ---------------------------------------------------------------------------
# 4. Scope and Repository Isolation Tests
# ---------------------------------------------------------------------------

def test_repository_mismatch_fails(engine, test_principal, test_repo, test_client):
    create_req = SessionCreateRequest(
        envelope=ProtocolEnvelope(),
        repository=test_repo,  # repo-core-api
        client=test_client,
    )
    create_res = engine.create_session(test_principal, create_req)

    # Request specifies a different repository than session was created for
    query_req = ContextQueryRequest(
        envelope=ProtocolEnvelope(),
        session_id=create_res.session_id,
        repository_id="repo-frontend",  # MISMATCH
        query="Render login component",
    )
    with pytest.raises(RepositoryMismatchError) as exc_info:
        engine.query_context(test_principal, query_req)
    assert exc_info.value.code == ErrorCode.REPOSITORY_MISMATCH.value
    assert "repo-frontend" in exc_info.value.message


def test_principal_unauthorized_repository_fails(engine, unauthorized_principal, test_repo, test_client):
    create_req = SessionCreateRequest(
        envelope=ProtocolEnvelope(),
        repository=test_repo,  # repo-core-api (not allowed for unauthorized_principal)
        client=test_client,
    )
    with pytest.raises(AccessDeniedError) as exc_info:
        engine.create_session(unauthorized_principal, create_req)
    assert exc_info.value.code == ErrorCode.ACCESS_DENIED.value


def test_cross_tenant_session_access_denied(engine, test_principal, unauthorized_principal, test_repo, test_client):
    create_req = SessionCreateRequest(
        envelope=ProtocolEnvelope(),
        repository=test_repo,
        client=test_client,
    )
    create_res = engine.create_session(test_principal, create_req)

    # Different principal from different tenant attempts to resume session
    resume_req = SessionResumeRequest(
        envelope=ProtocolEnvelope(),
        session_id=create_res.session_id,
        client=test_client,
    )
    with pytest.raises(AccessDeniedError) as exc_info:
        engine.resume_session(unauthorized_principal, resume_req)
    assert exc_info.value.code == ErrorCode.ACCESS_DENIED.value


def test_team_scope_permission_check(engine, test_principal, test_repo, test_client):
    create_req = SessionCreateRequest(
        envelope=ProtocolEnvelope(),
        repository=test_repo,
        client=test_client,
    )
    create_res = engine.create_session(test_principal, create_req)

    # Principal belongs to team_backend and team_infra, but NOT team_marketing
    store_req = StoreDecisionRequest(
        envelope=ProtocolEnvelope(),
        session_id=create_res.session_id,
        repository_id=test_repo.repository_id,
        title="Marketing branding choice",
        rationale="Adopted new theme colors",
        scope=MemoryScope.TEAM.value,
        team_id="team_marketing",  # Unauthorized team
    )
    with pytest.raises(InvalidScopeError) as exc_info:
        engine.store_decision(test_principal, store_req)
    assert exc_info.value.code == ErrorCode.INVALID_SCOPE.value


# ---------------------------------------------------------------------------
# 5. Idempotency & Duplicate Handling Tests
# ---------------------------------------------------------------------------

def test_idempotent_session_creation(engine, test_principal, test_repo, test_client):
    idempotency_key = "idemp_key_create_001"
    req = SessionCreateRequest(
        envelope=ProtocolEnvelope(idempotency_key=idempotency_key),
        repository=test_repo,
        client=test_client,
        title="Initial Setup",
    )

    res1 = engine.create_session(test_principal, req)
    res2 = engine.create_session(test_principal, req)

    # Both calls return identical session ID without creating a duplicate record
    assert res1.session_id == res2.session_id
    assert res1.created_at == res2.created_at
    assert len(engine._sessions) == 1


def test_idempotent_creation_conflict(engine, test_principal, test_repo, test_client):
    idempotency_key = "idemp_key_conflict_002"
    req1 = SessionCreateRequest(
        envelope=ProtocolEnvelope(idempotency_key=idempotency_key),
        repository=test_repo,
        client=test_client,
        title="Original Payload",
    )
    engine.create_session(test_principal, req1)

    # Reusing same idempotency key with conflicting title
    req2 = SessionCreateRequest(
        envelope=ProtocolEnvelope(idempotency_key=idempotency_key),
        repository=test_repo,
        client=test_client,
        title="Conflicting Changed Payload",
    )
    with pytest.raises(IdempotencyConflictError) as exc_info:
        engine.create_session(test_principal, req2)
    assert exc_info.value.code == ErrorCode.IDEMPOTENCY_CONFLICT.value


# ---------------------------------------------------------------------------
# 6. Payload Limits & Malformed Input Tests
# ---------------------------------------------------------------------------

def test_oversized_query_fails(engine, test_principal, test_repo, test_client):
    create_req = SessionCreateRequest(
        envelope=ProtocolEnvelope(),
        repository=test_repo,
        client=test_client,
    )
    create_res = engine.create_session(test_principal, create_req)

    oversized_query = "a" * 9000  # MAX_QUERY_LENGTH = 8192
    query_req = ContextQueryRequest(
        envelope=ProtocolEnvelope(),
        session_id=create_res.session_id,
        repository_id=test_repo.repository_id,
        query=oversized_query,
    )
    with pytest.raises(PayloadTooLargeError) as exc_info:
        engine.query_context(test_principal, query_req)
    assert exc_info.value.code == ErrorCode.PAYLOAD_TOO_LARGE.value
    assert exc_info.value.field == "query"


def test_negative_max_tokens_fails():
    req = ContextQueryRequest(
        envelope=ProtocolEnvelope(),
        session_id="sess_123",
        repository_id="repo-1",
        query="test query",
        max_tokens=-100,
    )
    with pytest.raises(MalformedInputError) as exc_info:
        req.validate()
    assert exc_info.value.field == "max_tokens"


# ---------------------------------------------------------------------------
# 7. Context Query & Memory Store Operations
# ---------------------------------------------------------------------------

def test_context_query_sanitizes_secrets(engine, test_principal, test_repo, test_client):
    create_req = SessionCreateRequest(
        envelope=ProtocolEnvelope(),
        repository=test_repo,
        client=test_client,
    )
    create_res = engine.create_session(test_principal, create_req)

    # Query contains raw OpenAI secret key
    query_req = ContextQueryRequest(
        envelope=ProtocolEnvelope(),
        session_id=create_res.session_id,
        repository_id=test_repo.repository_id,
        query="Verify auth credentials using key sk-live1234567890abcdef12345678",
    )
    res = engine.query_context(test_principal, query_req)

    assert res.success is True
    assert "sk-" not in res.formatted_context
    assert "[REDACTED_API_KEY]" in res.formatted_context
    assert res.tokens_used > 0
    assert res.budget_utilization_pct > 0.0


def test_store_decision_operation(engine, test_principal, test_repo, test_client):
    create_req = SessionCreateRequest(
        envelope=ProtocolEnvelope(),
        repository=test_repo,
        client=test_client,
    )
    create_res = engine.create_session(test_principal, create_req)

    store_req = StoreDecisionRequest(
        envelope=ProtocolEnvelope(),
        session_id=create_res.session_id,
        repository_id=test_repo.repository_id,
        title="Migrate to RS256 JWT tokens",
        rationale="RS256 enables asymmetric key verification without sharing private secrets.",
        scope=MemoryScope.REPOSITORY.value,
        category="architecture",
        impact_areas=["backend/auth.py", "backend/security.py"],
    )
    res = engine.store_decision(test_principal, store_req)

    assert res.success is True
    assert res.decision_id.startswith("dec_")
    assert res.status == "stored"
    assert res.scope == MemoryScope.REPOSITORY.value


def test_store_fact_operation(engine, test_principal, test_repo, test_client):
    create_req = SessionCreateRequest(
        envelope=ProtocolEnvelope(),
        repository=test_repo,
        client=test_client,
    )
    create_res = engine.create_session(test_principal, create_req)

    fact_req = StoreFactRequest(
        envelope=ProtocolEnvelope(),
        session_id=create_res.session_id,
        repository_id=test_repo.repository_id,
        title="Pytest command path requirement",
        content="Execute tests using python -m pytest to guarantee venv binary resolution.",
        category="recurring_solution",
        scope=MemoryScope.REPOSITORY.value,
        actionable_takeaways=["Always run python -m pytest in Windows automation."],
    )
    res = engine.store_fact(test_principal, fact_req)

    assert res.success is True
    assert res.memory_id.startswith("mem_")
    assert res.status == "stored"


def test_store_session_summary_operation(engine, test_principal, test_repo, test_client):
    create_req = SessionCreateRequest(
        envelope=ProtocolEnvelope(),
        repository=test_repo,
        client=test_client,
    )
    create_res = engine.create_session(test_principal, create_req)

    summary_req = StoreSessionSummaryRequest(
        envelope=ProtocolEnvelope(),
        session_id=create_res.session_id,
        repository_id=test_repo.repository_id,
        prompt="Why was RS256 chosen over HS256 for authentication?",
        response_summary="RS256 was chosen because asymmetric keys allow third-party verification without exposing signing credentials.",
        engineering_decisions=["Use RS256 with 2048-bit RSA keys."],
        affected_files=["backend/auth.py"],
        affected_symbols=["verify_jwt_token"],
    )
    res = engine.store_session_summary(test_principal, summary_req)

    assert res.success is True
    assert res.session_id == create_res.session_id
    assert res.distilled_decisions_count >= 1
    assert res.status == "recorded"
