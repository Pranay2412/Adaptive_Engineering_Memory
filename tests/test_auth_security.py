"""Comprehensive Authentication, Authorization, RBAC & Security Audit Tests (Phase 9).

Tests:
1. RBAC Matrix & Role Permissions (Admin, Maintainer, Developer, Viewer, System Service)
2. Server-side Permission Enforcement in SessionProtocolEngine (Allow & Deny paths)
3. Multi-tenant and Cross-Repository Isolation (Deny unauthorized repo/tenant access)
4. Security Audit Event Logging & Secret Sanitization (No sensitive credentials in audit trails)
5. High-Entropy API Key Management (SHA-256 hash storage, expiration, revocation)
6. Cryptographic Token Service (HMAC-SHA256 signature verification, tampering, expiration)
7. REST API Authentication Boundary (Bearer tokens, API keys, 401/403 status codes, audit endpoints)
"""

from __future__ import annotations

import os
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.api import app, reset_protocol_engine
from backend.security import (
    ApiKeyRecord,
    ApiKeyService,
    Permission,
    Role,
    ROLE_PERMISSIONS,
    SecurityAuditEvent,
    SecurityAuditLogger,
    TokenService,
    compute_effective_permissions,
    principal_from_api_key,
)
from backend.session_protocol import (
    AccessDeniedError,
    AuthenticatedPrincipal,
    AuthenticationRequiredError,
    ClientIdentity,
    ContextQueryRequest,
    InvalidScopeError,
    MemoryScope,
    ProtocolEnvelope,
    RepositoryIdentity,
    ScopedRetrievalRequest,
    SessionCreateRequest,
    SessionProtocolEngine,
    StoreDecisionRequest,
    StoreFactRequest,
    utc_now_iso,
)
from backend.team_memory_store import TeamMemoryDocumentStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_doc_store():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    store = TeamMemoryDocumentStore(db_path=db_path)
    yield store
    del store
    try:
        os.remove(db_path)
    except OSError:
        pass


@pytest.fixture
def protocol_engine(temp_doc_store):
    return SessionProtocolEngine(doc_store=temp_doc_store)


@pytest.fixture
def api_client(temp_doc_store):
    engine = SessionProtocolEngine(doc_store=temp_doc_store)
    reset_protocol_engine(engine)
    client = TestClient(app)
    yield client
    reset_protocol_engine(None)


# ---------------------------------------------------------------------------
# Test Group 1: RBAC Matrix & Role Permissions
# ---------------------------------------------------------------------------

def test_rbac_matrix_coverage():
    """Verify all recognized roles have defined permission mappings."""
    for role in Role:
        assert role.value in ROLE_PERMISSIONS
        perms = ROLE_PERMISSIONS[role.value]
        assert isinstance(perms, set)
        assert len(perms) > 0


def test_rbac_permission_inheritance():
    """Test effective permissions computation and role hierarchies."""
    admin_perms = compute_effective_permissions([Role.ADMIN.value])
    assert Permission.ADMIN_ALL.value in admin_perms

    viewer_perms = compute_effective_permissions([Role.VIEWER.value])
    assert Permission.MEMORY_READ.value in viewer_perms
    assert Permission.CONTEXT_QUERY.value in viewer_perms
    assert Permission.MEMORY_WRITE.value not in viewer_perms
    assert Permission.DECISION_WRITE.value not in viewer_perms
    assert Permission.SESSION_MANAGE.value not in viewer_perms

    dev_perms = compute_effective_permissions([Role.DEVELOPER.value])
    assert Permission.MEMORY_READ.value in dev_perms
    assert Permission.MEMORY_WRITE.value in dev_perms
    assert Permission.CONTEXT_QUERY.value in dev_perms
    assert Permission.SESSION_MANAGE.value in dev_perms
    assert Permission.DECISION_WRITE.value in dev_perms
    assert Permission.GIT_INGEST.value not in dev_perms
    assert Permission.AUDIT_READ.value not in dev_perms


def test_principal_role_and_permission_checks():
    """Test AuthenticatedPrincipal helper methods."""
    viewer = AuthenticatedPrincipal(
        principal_id="viewer-1",
        tenant_id="tenant-1",
        roles=[Role.VIEWER.value],
        allowed_repositories=["repo-a"],
    )
    assert viewer.has_role(Role.VIEWER.value)
    assert not viewer.has_role(Role.ADMIN.value)
    assert viewer.has_permission(Permission.MEMORY_READ.value)
    assert not viewer.has_permission(Permission.DECISION_WRITE.value)
    assert viewer.can_perform(Permission.MEMORY_READ.value, repository_id="repo-a")
    assert not viewer.can_perform(Permission.MEMORY_READ.value, repository_id="repo-b")
    assert not viewer.can_perform(Permission.DECISION_WRITE.value, repository_id="repo-a")

    admin = AuthenticatedPrincipal(
        principal_id="admin-1",
        tenant_id="tenant-1",
        roles=[Role.ADMIN.value],
        allowed_repositories=["*"],
    )
    assert admin.has_role(Role.ADMIN.value)
    assert admin.has_role(Role.DEVELOPER.value)  # Admin satisfies any role
    assert admin.has_permission(Permission.DECISION_WRITE.value)
    assert admin.has_permission("any:arbitrary:perm")
    assert admin.can_perform("any:action", repository_id="any-repo")


# ---------------------------------------------------------------------------
# Test Group 2: Server-Side Permission Enforcement in SessionProtocolEngine
# ---------------------------------------------------------------------------

def test_viewer_denied_session_creation(protocol_engine):
    """Viewers lack session:manage and cannot create sessions."""
    viewer = AuthenticatedPrincipal(
        principal_id="viewer-alice",
        tenant_id="tenant-1",
        roles=[Role.VIEWER.value],
        allowed_repositories=["repo-alpha"],
    )
    req = SessionCreateRequest(
        envelope=ProtocolEnvelope(),
        repository=RepositoryIdentity(repository_id="repo-alpha"),
        client=ClientIdentity(),
    )
    with pytest.raises(AccessDeniedError) as exc_info:
        protocol_engine.create_session(viewer, req)
    assert "session:manage" in str(exc_info.value)


def test_developer_allowed_session_creation(protocol_engine):
    """Developers possess session:manage and can create sessions in authorized repos."""
    dev = AuthenticatedPrincipal(
        principal_id="dev-bob",
        tenant_id="tenant-1",
        roles=[Role.DEVELOPER.value],
        allowed_repositories=["repo-alpha"],
    )
    req = SessionCreateRequest(
        envelope=ProtocolEnvelope(),
        repository=RepositoryIdentity(repository_id="repo-alpha"),
        client=ClientIdentity(),
    )
    resp = protocol_engine.create_session(dev, req)
    assert resp.session_id.startswith("sess_")
    assert resp.repository_id == "repo-alpha"


def test_viewer_denied_decision_and_fact_writes(protocol_engine):
    """Viewers cannot store decisions or facts even if active session exists."""
    dev = AuthenticatedPrincipal(
        principal_id="dev-bob",
        tenant_id="tenant-1",
        roles=[Role.DEVELOPER.value],
        allowed_repositories=["repo-alpha"],
    )
    create_req = SessionCreateRequest(
        envelope=ProtocolEnvelope(),
        repository=RepositoryIdentity(repository_id="repo-alpha"),
        client=ClientIdentity(),
    )
    sess_resp = protocol_engine.create_session(dev, create_req)
    session_id = sess_resp.session_id

    # Now viewer in the same tenant tries to mutate session
    viewer = AuthenticatedPrincipal(
        principal_id="viewer-alice",
        tenant_id="tenant-1",
        roles=[Role.VIEWER.value],
        allowed_repositories=["repo-alpha"],
    )

    dec_req = StoreDecisionRequest(
        envelope=ProtocolEnvelope(),
        session_id=session_id,
        repository_id="repo-alpha",
        title="Unauthorized Decision",
        rationale="Should fail",
    )
    with pytest.raises(AccessDeniedError) as exc_info:
        protocol_engine.store_decision(viewer, dec_req)
    assert "decision:write" in str(exc_info.value)

    fact_req = StoreFactRequest(
        envelope=ProtocolEnvelope(),
        session_id=session_id,
        repository_id="repo-alpha",
        title="Unauthorized Fact",
        content="Should fail",
    )
    with pytest.raises(AccessDeniedError) as exc_info:
        protocol_engine.store_fact(viewer, fact_req)
    assert "memory:write" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test Group 3: Multi-Tenant & Cross-Repository Isolation
# ---------------------------------------------------------------------------

def test_unauthorized_repository_rejection(protocol_engine):
    """Principals cannot access repositories outside their allowed list."""
    dev = AuthenticatedPrincipal(
        principal_id="dev-carol",
        tenant_id="tenant-1",
        roles=[Role.DEVELOPER.value],
        allowed_repositories=["repo-alpha"],
    )
    req = SessionCreateRequest(
        envelope=ProtocolEnvelope(),
        repository=RepositoryIdentity(repository_id="repo-secret-beta"),
        client=ClientIdentity(),
    )
    with pytest.raises(AccessDeniedError) as exc_info:
        protocol_engine.create_session(dev, req)
    assert "repo-secret-beta" in str(exc_info.value)


def test_cross_tenant_session_isolation(protocol_engine):
    """Sessions created under Tenant A cannot be read or resumed by Tenant B."""
    tenant_a_dev = AuthenticatedPrincipal(
        principal_id="user-a",
        tenant_id="tenant-alpha",
        roles=[Role.DEVELOPER.value],
        allowed_repositories=["*"],
    )
    sess = protocol_engine.create_session(
        tenant_a_dev,
        SessionCreateRequest(
            envelope=ProtocolEnvelope(),
            repository=RepositoryIdentity(repository_id="shared-repo"),
            client=ClientIdentity(),
        ),
    )

    tenant_b_dev = AuthenticatedPrincipal(
        principal_id="user-b",
        tenant_id="tenant-beta",
        roles=[Role.DEVELOPER.value],
        allowed_repositories=["*"],
    )

    with pytest.raises(AccessDeniedError) as exc_info:
        protocol_engine.query_context(
            tenant_b_dev,
            ContextQueryRequest(
                envelope=ProtocolEnvelope(),
                session_id=sess.session_id,
                repository_id="shared-repo",
                query="find secrets",
            ),
        )
    assert "does not own session" in str(exc_info.value)


def test_cross_team_scope_leakage_prevented(protocol_engine):
    """Principals cannot write to team scope for teams they do not belong to."""
    dev = AuthenticatedPrincipal(
        principal_id="dev-dave",
        tenant_id="tenant-1",
        team_ids=["team-marketing"],
        roles=[Role.DEVELOPER.value],
        allowed_repositories=["repo-core"],
    )
    sess = protocol_engine.create_session(
        dev,
        SessionCreateRequest(
            envelope=ProtocolEnvelope(),
            repository=RepositoryIdentity(repository_id="repo-core"),
            client=ClientIdentity(),
        ),
    )

    dec_req = StoreDecisionRequest(
        envelope=ProtocolEnvelope(),
        session_id=sess.session_id,
        repository_id="repo-core",
        title="Cross team attempt",
        rationale="Trying to write to engineering team memory",
        scope=MemoryScope.TEAM.value,
        team_id="team-engineering-forbidden",
    )
    with pytest.raises(InvalidScopeError) as exc_info:
        protocol_engine.store_decision(dev, dec_req)
    assert "does not belong to team 'team-engineering-forbidden'" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test Group 4: Security Audit Event Logging & Secret Sanitization
# ---------------------------------------------------------------------------

def test_audit_event_persistence_and_query(temp_doc_store):
    """Verify security audit events are persisted and retrievable."""
    logger = SecurityAuditLogger(doc_store=temp_doc_store)

    event = SecurityAuditEvent(
        id="audit_test_1",
        event_type="test_event",
        principal_id="user-123",
        tenant_id="tenant-xyz",
        action="read",
        resource="secret_file.py",
        status="allow",
        timestamp=utc_now_iso(),
    )
    logger.log(event)

    events = temp_doc_store.list_audit_events(tenant_id="tenant-xyz")
    assert len(events) == 1
    assert events[0].id == "audit_test_1"
    assert events[0].principal_id == "user-123"
    assert events[0].action == "read"


def test_audit_event_secret_redaction(temp_doc_store):
    """Audit logs must strictly redact private credentials and keys from metadata."""
    logger = SecurityAuditLogger(doc_store=temp_doc_store)

    dirty_meta = {
        "user_query": "Use key sk-123456789012345678901234 to authenticate",
        "nested": {"token": "ghp_123456789012345678901234567890123456"},
    }
    event = SecurityAuditEvent(
        id="audit_redact_1",
        event_type="auth_check",
        principal_id="user-456",
        tenant_id="tenant-1",
        action="query",
        status="allow",
        metadata=dirty_meta,
    )
    logger.log(event)

    events = temp_doc_store.list_audit_events(principal_id="user-456")
    assert len(events) == 1
    stored_meta = events[0].metadata
    assert "[REDACTED_API_KEY]" in stored_meta["user_query"]
    assert "sk-1234567890" not in stored_meta["user_query"]
    assert stored_meta["nested"]["token"] == "[REDACTED_GITHUB_TOKEN]"


def test_protocol_engine_generates_audit_trail(protocol_engine, temp_doc_store):
    """Protocol operations must generate audit records for both allowed and denied actions."""
    dev = AuthenticatedPrincipal(
        principal_id="audit-dev",
        tenant_id="tenant-audit",
        roles=[Role.DEVELOPER.value],
        allowed_repositories=["repo-ok"],
    )

    # Allowed creation
    protocol_engine.create_session(
        dev,
        SessionCreateRequest(
            envelope=ProtocolEnvelope(),
            repository=RepositoryIdentity(repository_id="repo-ok"),
            client=ClientIdentity(),
        ),
    )

    # Denied creation (repo not allowed)
    with pytest.raises(AccessDeniedError):
        protocol_engine.create_session(
            dev,
            SessionCreateRequest(
                envelope=ProtocolEnvelope(),
                repository=RepositoryIdentity(repository_id="repo-forbidden"),
                client=ClientIdentity(),
            ),
        )

    events = temp_doc_store.list_audit_events(principal_id="audit-dev")
    assert len(events) >= 2
    statuses = [e.status for e in events]
    assert "allow" in statuses
    assert "deny" in statuses


# ---------------------------------------------------------------------------
# Test Group 5: High-Entropy API Key Management
# ---------------------------------------------------------------------------

def test_api_key_create_verify_and_revoke(temp_doc_store):
    """Test full API key lifecycle: creation, hashing, verification, revocation."""
    svc = ApiKeyService(doc_store=temp_doc_store)

    raw_key, record = svc.create_api_key(
        name="CI Service Key",
        tenant_id="tenant-ci",
        principal_id="ci-bot",
        roles=[Role.MAINTAINER.value],
        allowed_repositories=["repo-a", "repo-b"],
    )

    assert raw_key.startswith("syn_")
    assert record.key_hash != raw_key  # Plaintext key is NOT the hash
    assert len(record.key_hash) == 64  # SHA-256 hex digest

    # Verify key
    verified = svc.verify_api_key(raw_key)
    assert verified is not None
    assert verified.key_id == record.key_id
    assert verified.principal_id == "ci-bot"
    assert Role.MAINTAINER.value in verified.roles

    # Principal conversion
    principal = principal_from_api_key(verified)
    assert principal.principal_id == "ci-bot"
    assert principal.has_role(Role.MAINTAINER.value)
    assert principal.can_access_repository("repo-a")
    assert not principal.can_access_repository("repo-unauthorized")

    # Revocation
    revoked = svc.revoke_api_key(record.key_id)
    assert revoked is True

    # After revocation, verification fails
    assert svc.verify_api_key(raw_key) is None


def test_api_key_expiration(temp_doc_store):
    """Expired API keys must be rejected during verification."""
    svc = ApiKeyService(doc_store=temp_doc_store)
    past_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    raw_key, record = svc.create_api_key(
        name="Expired Key",
        tenant_id="tenant-1",
        principal_id="user-old",
        expires_at=past_iso,
    )
    assert svc.verify_api_key(raw_key) is None


# ---------------------------------------------------------------------------
# Test Group 6: Cryptographic Token Service (HMAC-SHA256)
# ---------------------------------------------------------------------------

def test_token_service_issue_and_verify():
    """Verify cryptographically signed bearer tokens."""
    token_svc = TokenService(secret_key="test-secret-key-32chars-minimum-length-synapse")

    principal = AuthenticatedPrincipal(
        principal_id="user-jwt",
        tenant_id="tenant-jwt",
        roles=[Role.DEVELOPER.value],
        allowed_repositories=["repo-main"],
        team_ids=["team-backend"],
    )

    token = token_svc.create_token(principal, ttl_seconds=300)
    assert isinstance(token, str)
    assert token.count(".") == 2

    verified_principal = token_svc.verify_token(token)
    assert verified_principal.principal_id == "user-jwt"
    assert verified_principal.tenant_id == "tenant-jwt"
    assert verified_principal.has_role(Role.DEVELOPER.value)
    assert verified_principal.can_access_repository("repo-main")
    assert not verified_principal.can_access_repository("repo-other")
    assert verified_principal.can_access_team("team-backend")


def test_token_tampering_rejected():
    """Tampered token signatures or payloads must raise ValueError."""
    token_svc = TokenService(secret_key="test-secret-key-32chars-minimum-length-synapse")

    principal = AuthenticatedPrincipal(
        principal_id="user-victim",
        tenant_id="tenant-jwt",
        roles=[Role.VIEWER.value],
    )
    token = token_svc.create_token(principal, ttl_seconds=300)

    # Tamper with the signature
    parts = token.split(".")
    tampered_token = f"{parts[0]}.{parts[1]}.invalidsignature123"

    with pytest.raises(ValueError) as exc:
        token_svc.verify_token(tampered_token)
    assert "signature" in str(exc.value).lower()


def test_token_expiration_rejected():
    """Expired tokens must fail validation."""
    token_svc = TokenService(secret_key="test-secret-key-32chars-minimum-length-synapse")

    principal = AuthenticatedPrincipal(principal_id="user-fast", tenant_id="tenant-jwt")
    # Expire immediately (-1 second ttl)
    token = token_svc.create_token(principal, ttl_seconds=-1)

    with pytest.raises(ValueError) as exc:
        token_svc.verify_token(token)
    assert "expired" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Test Group 7: REST API Authentication Boundary
# ---------------------------------------------------------------------------

def test_api_authenticated_bearer_token(api_client):
    """API accepts valid Bearer tokens and authorizes session creation."""
    token_svc = TokenService()
    principal = AuthenticatedPrincipal(
        principal_id="api-bearer-dev",
        tenant_id="tenant-api",
        roles=[Role.DEVELOPER.value],
        allowed_repositories=["repo-api"],
    )
    token = token_svc.create_token(principal)

    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "envelope": {"protocol_version": "1.0.0"},
        "repository": {"repository_id": "repo-api"},
        "client": {"client_type": "vscode"},
    }
    resp = api_client.post("/api/v1/sessions", json=payload, headers=headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "active"
    assert data["repository_id"] == "repo-api"


def test_api_invalid_bearer_token_returns_401(api_client):
    """Invalid bearer tokens return 401 Unauthorized."""
    headers = {"Authorization": "Bearer invalid.tampered.token"}
    payload = {
        "envelope": {"protocol_version": "1.0.0"},
        "repository": {"repository_id": "repo-api"},
        "client": {"client_type": "vscode"},
    }
    resp = api_client.post("/api/v1/sessions", json=payload, headers=headers)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_api_authenticated_api_key(api_client, temp_doc_store):
    """API accepts valid API keys and executes requests."""
    svc = ApiKeyService(doc_store=temp_doc_store)
    raw_key, record = svc.create_api_key(
        name="Key for API",
        tenant_id="tenant-api",
        principal_id="key-dev",
        roles=[Role.DEVELOPER.value],
        allowed_repositories=["repo-api-key"],
    )

    headers = {"X-API-Key": raw_key}
    payload = {
        "envelope": {"protocol_version": "1.0.0"},
        "repository": {"repository_id": "repo-api-key"},
        "client": {"client_type": "cli"},
    }
    resp = api_client.post("/api/v1/sessions", json=payload, headers=headers)
    assert resp.status_code == 201
    assert resp.json()["repository_id"] == "repo-api-key"


def test_api_revoked_api_key_returns_401(api_client, temp_doc_store):
    """Revoked API key returns 401 Unauthorized."""
    svc = ApiKeyService(doc_store=temp_doc_store)
    raw_key, record = svc.create_api_key(
        name="Revoked Key",
        tenant_id="tenant-api",
        principal_id="key-dev",
    )
    svc.revoke_api_key(record.key_id)

    headers = {"X-API-Key": raw_key}
    payload = {
        "envelope": {"protocol_version": "1.0.0"},
        "repository": {"repository_id": "repo-api"},
        "client": {"client_type": "cli"},
    }
    resp = api_client.post("/api/v1/sessions", json=payload, headers=headers)
    assert resp.status_code == 401


def test_api_viewer_forbidden_from_session_creation(api_client):
    """Viewer token receives 403 Forbidden on create session endpoint."""
    token_svc = TokenService()
    viewer = AuthenticatedPrincipal(
        principal_id="viewer-eve",
        tenant_id="tenant-api",
        roles=[Role.VIEWER.value],
        allowed_repositories=["repo-api"],
    )
    token = token_svc.create_token(viewer)

    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "envelope": {"protocol_version": "1.0.0"},
        "repository": {"repository_id": "repo-api"},
        "client": {"client_type": "vscode"},
    }
    resp = api_client.post("/api/v1/sessions", json=payload, headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ACCESS_DENIED"


def test_api_auth_me_endpoint(api_client):
    """GET /api/v1/auth/me returns the verified principal details and effective permissions."""
    token_svc = TokenService()
    dev = AuthenticatedPrincipal(
        principal_id="dev-profile",
        tenant_id="tenant-xyz",
        roles=[Role.DEVELOPER.value],
        allowed_repositories=["repo-1", "repo-2"],
        team_ids=["team-dev"],
    )
    token = token_svc.create_token(dev)

    headers = {"Authorization": f"Bearer {token}"}
    resp = api_client.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["principal_id"] == "dev-profile"
    assert data["roles"] == ["developer"]
    assert Permission.SESSION_MANAGE.value in data["permissions"]
    assert Permission.MEMORY_WRITE.value in data["permissions"]


def test_api_security_audit_access_control(api_client):
    """Developer cannot inspect audit events; Admin can inspect audit events."""
    token_svc = TokenService()
    dev = AuthenticatedPrincipal(
        principal_id="dev-normal",
        tenant_id="tenant-sec",
        roles=[Role.DEVELOPER.value],
    )
    admin = AuthenticatedPrincipal(
        principal_id="admin-sec",
        tenant_id="tenant-sec",
        roles=[Role.ADMIN.value],
    )

    dev_token = token_svc.create_token(dev)
    admin_token = token_svc.create_token(admin)

    # Dev attempt -> 403 Forbidden
    resp_dev = api_client.get("/api/v1/security/audit", headers={"Authorization": f"Bearer {dev_token}"})
    assert resp_dev.status_code == 403

    # Admin attempt -> 200 OK
    resp_admin = api_client.get("/api/v1/security/audit", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp_admin.status_code == 200
    assert isinstance(resp_admin.json(), list)


def test_api_key_management_endpoints(api_client):
    """Admin can create and revoke API keys via REST API endpoints."""
    token_svc = TokenService()
    admin = AuthenticatedPrincipal(
        principal_id="admin-root",
        tenant_id="tenant-corp",
        roles=[Role.ADMIN.value],
    )
    admin_token = token_svc.create_token(admin)
    headers = {"Authorization": f"Bearer {admin_token}"}

    # Create API Key
    create_body = {
        "name": "Integration Test Key",
        "roles": ["maintainer"],
        "allowed_repositories": ["repo-corp"],
        "allowed_teams": ["team-corp"],
    }
    create_resp = api_client.post("/api/v1/auth/api-keys", json=create_body, headers=headers)
    assert create_resp.status_code == 201
    create_data = create_resp.json()
    assert "api_key" in create_data
    assert create_data["api_key"].startswith("syn_")
    key_id = create_data["key_record"]["key_id"]

    # Revoke API Key
    del_resp = api_client.delete(f"/api/v1/auth/api-keys/{key_id}", headers=headers)
    assert del_resp.status_code == 200
    assert del_resp.json()["revoked"] is True
