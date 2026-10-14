"""Synapse Session & Context REST API (Phase 6.2).

Exposes provider-neutral session and context operations over HTTP using
Phase 6.1 protocol schemas, Adaptive Context Orchestrator (ACO),
and the SQLite/Neo4j engineering memory stores.

NOTE: This service is currently in DEVELOPMENT-ONLY mode. Secure authentication
and token verification will be implemented in Phase 9. Body-supplied identity
attributes are never treated as secure credentials.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .orchestrator import AdaptiveContextOrchestrator
from .security import ApiKeyService, Permission, TokenService, principal_from_api_key
from .session_memory import AISessionMemoryService
from .session_protocol import (
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
    ProtocolException,
    RepositoryIdentity,
    RepositoryMismatchError,
    ScopedRetrievalRequest,
    ScopedRetrievalResponse,
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
    SUPPORTED_PROTOCOL_VERSIONS,
    UnsupportedProtocolVersionError,
    utc_now_iso,
)
from .team_memory_store import TeamMemoryDocumentStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global Application & Service Factory
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Synapse Adaptive Engineering Memory API",
    version=CURRENT_PROTOCOL_VERSION,
    description="Provider-neutral REST API for cross-IDE session and context management.",
    docs_url="/docs",
    openapi_url="/openapi.json",
)

# Enable CORS for local cross-IDE extensions (VS Code, JetBrains, Browser clients)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_ENGINE: SessionProtocolEngine | None = None


def get_protocol_engine() -> SessionProtocolEngine:
    """Retrieve or lazily initialize the singleton SessionProtocolEngine."""
    global _ENGINE
    if _ENGINE is None:
        doc_store = TeamMemoryDocumentStore()
        session_svc = AISessionMemoryService(doc_store=doc_store)
        orchestrator = AdaptiveContextOrchestrator(session_memory_service=session_svc)
        _ENGINE = SessionProtocolEngine(
            doc_store=doc_store,
            session_service=session_svc,
            orchestrator=orchestrator,
        )
    return _ENGINE


def reset_protocol_engine(engine: SessionProtocolEngine | None = None) -> None:
    """Reset the singleton engine for clean test isolation."""
    global _ENGINE
    _ENGINE = engine


# ---------------------------------------------------------------------------
# Middleware: Request ID, Timing & Security Disclaimers
# ---------------------------------------------------------------------------

@app.middleware("http")
async def protocol_envelope_middleware(request: Request, call_next):
    start_time = time.perf_counter()

    # Extract or generate Request ID
    req_id = request.headers.get("X-Request-ID") or f"req_{utc_now_iso()[:10]}_{int(start_time * 1000) % 1000000}"
    request.state.request_id = req_id

    response: Response = await call_next(request)

    duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
    response.headers["X-Request-ID"] = req_id
    response.headers["X-Response-Time-Ms"] = str(duration_ms)
    response.headers["X-Synapse-Mode"] = "development-only"
    response.headers["X-Synapse-Protocol-Version"] = CURRENT_PROTOCOL_VERSION
    response.headers["X-Synapse-Security-Warning"] = (
        "Development mode: principal identity is simulated or header-derived; production auth is enforced in Phase 9."
    )
    return response


# ---------------------------------------------------------------------------
# Exception Handlers
# ---------------------------------------------------------------------------

@app.exception_handler(ProtocolException)
async def protocol_exception_handler(request: Request, exc: ProtocolException):
    status_map = {
        ErrorCode.MALFORMED_INPUT.value: status.HTTP_400_BAD_REQUEST,
        ErrorCode.PAYLOAD_TOO_LARGE.value: status.HTTP_400_BAD_REQUEST,
        ErrorCode.UNSUPPORTED_PROTOCOL_VERSION.value: status.HTTP_422_UNPROCESSABLE_ENTITY,
        ErrorCode.AUTHENTICATION_REQUIRED.value: status.HTTP_401_UNAUTHORIZED,
        ErrorCode.ACCESS_DENIED.value: status.HTTP_403_FORBIDDEN,
        ErrorCode.INVALID_SCOPE.value: status.HTTP_403_FORBIDDEN,
        ErrorCode.SESSION_NOT_FOUND.value: status.HTTP_404_NOT_FOUND,
        ErrorCode.SESSION_CLOSED.value: status.HTTP_409_CONFLICT,
        ErrorCode.SESSION_EXPIRED.value: status.HTTP_409_CONFLICT,
        ErrorCode.REPOSITORY_MISMATCH.value: status.HTTP_409_CONFLICT,
        ErrorCode.IDEMPOTENCY_CONFLICT.value: status.HTTP_409_CONFLICT,
        ErrorCode.RATE_LIMITED.value: status.HTTP_429_TOO_MANY_REQUESTS,
    }
    http_code = status_map.get(exc.code, status.HTTP_500_INTERNAL_SERVER_ERROR)

    envelope = ProtocolEnvelope(
        protocol_version=CURRENT_PROTOCOL_VERSION,
        request_id=getattr(request.state, "request_id", "req_unknown"),
        timestamp=utc_now_iso(),
    )
    error_resp = ProtocolErrorResponse(envelope=envelope, error=exc.to_error_detail())
    return JSONResponse(status_code=http_code, content=error_resp.to_dict())


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled server exception during request %s: %s", getattr(request.state, "request_id", "req_unknown"), exc)
    envelope = ProtocolEnvelope(
        protocol_version=CURRENT_PROTOCOL_VERSION,
        request_id=getattr(request.state, "request_id", "req_unknown"),
        timestamp=utc_now_iso(),
    )
    detail = ProtocolErrorDetail(
        code=ErrorCode.INTERNAL_ERROR.value,
        message=f"An unexpected internal error occurred: {str(exc)}",
        retryable=True,
    )
    error_resp = ProtocolErrorResponse(envelope=envelope, error=detail)
    return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content=error_resp.to_dict())


# ---------------------------------------------------------------------------
# Principal Resolution Dependency (Development Authentication Boundary)
# ---------------------------------------------------------------------------

def get_current_principal(
    authorization: str | None = Header(None, alias="Authorization"),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    x_principal_id: str | None = Header(None, alias="X-Principal-ID"),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-ID"),
    x_team_ids: str | None = Header(None, alias="X-Team-IDs"),
    x_allowed_repositories: str | None = Header(None, alias="X-Allowed-Repositories"),
    x_roles: str | None = Header(None, alias="X-Roles"),
) -> AuthenticatedPrincipal:
    """Resolve authenticated principal from Bearer tokens, API keys, or dev fallback.

    Authentication Precedence:
    1. Cryptographic Bearer Token: Authorization: Bearer <token> -> validated cryptographically.
    2. API Key: Authorization: ApiKey <key> OR X-API-Key: <key> -> validated via SHA-256 hash lookup.
    3. Development Fallback: In local dev, derives principal from simulation headers.
       If environment variable SYNAPSE_REQUIRE_AUTH is set to "1"/"true", fallback is disabled.
    """
    # 1. Bearer Token Authentication
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        try:
            token_svc = TokenService()
            return token_svc.verify_token(token)
        except Exception as e:
            raise AuthenticationRequiredError(f"Invalid or expired bearer token: {e}")

    # 2. API Key Authentication
    raw_api_key = None
    if authorization and authorization.lower().startswith("apikey "):
        raw_api_key = authorization.split(" ", 1)[1].strip()
    elif x_api_key:
        raw_api_key = x_api_key.strip()

    if raw_api_key:
        engine = get_protocol_engine()
        api_key_svc = ApiKeyService(doc_store=engine.doc_store)
        record = api_key_svc.verify_api_key(raw_api_key)
        if not record:
            raise AuthenticationRequiredError("Invalid, expired, or revoked API key.")
        return principal_from_api_key(record)

    # 3. Development Fallback
    require_auth = os.environ.get("SYNAPSE_REQUIRE_AUTH", "").lower() in ("1", "true", "yes")
    if require_auth:
        raise AuthenticationRequiredError("Authentication required: missing Bearer token or API key.")

    pid = x_principal_id.strip() if x_principal_id else "dev-user"
    tid = x_tenant_id.strip() if x_tenant_id else "dev-tenant"
    teams = [t.strip() for t in x_team_ids.split(",") if t.strip()] if x_team_ids else ["dev-team"]
    repos = [r.strip() for r in x_allowed_repositories.split(",") if r.strip()] if x_allowed_repositories else ["*"]
    roles = [r.strip() for r in x_roles.split(",") if r.strip()] if x_roles else ["developer"]

    return AuthenticatedPrincipal(
        principal_id=pid,
        tenant_id=tid,
        team_ids=teams,
        allowed_repositories=repos,
        roles=roles,
        is_authenticated=True,
    )


# ---------------------------------------------------------------------------
# Pydantic Schemas for API Requests
# ---------------------------------------------------------------------------

class EnvelopeSchema(BaseModel):
    protocol_version: str = CURRENT_PROTOCOL_VERSION
    request_id: str | None = None
    timestamp: str | None = None
    idempotency_key: str | None = None


class ClientIdentitySchema(BaseModel):
    client_type: str = ClientType.CUSTOM.value
    client_version: str = "1.0.0"
    assistant_name: str = "generic-assistant"
    assistant_version: str | None = None
    environment: dict[str, str] = Field(default_factory=dict)


class RepositoryIdentitySchema(BaseModel):
    repository_id: str
    name: str | None = None
    root_path: str | None = None
    branch: str | None = None
    commit_sha: str | None = None
    remote_url: str | None = None


class CreateSessionBody(BaseModel):
    repository: RepositoryIdentitySchema
    client: ClientIdentitySchema = Field(default_factory=ClientIdentitySchema)
    title: str = ""
    ttl_seconds: int = 86400
    metadata: dict[str, Any] = Field(default_factory=dict)
    envelope: EnvelopeSchema = Field(default_factory=EnvelopeSchema)


class ResumeSessionBody(BaseModel):
    client: ClientIdentitySchema = Field(default_factory=ClientIdentitySchema)
    envelope: EnvelopeSchema = Field(default_factory=EnvelopeSchema)


class CloseSessionBody(BaseModel):
    reason: str = "completed"
    summary: str = ""
    envelope: EnvelopeSchema = Field(default_factory=EnvelopeSchema)


class ContextQueryBody(BaseModel):
    repository_id: str
    query: str
    max_tokens: int = 1500
    optimization_mode: str = "deterministic"
    active_file: str | None = None
    cursor_position: int | None = None
    include_scratchpad: bool = False
    envelope: EnvelopeSchema = Field(default_factory=EnvelopeSchema)


class StoreDecisionBody(BaseModel):
    repository_id: str
    title: str
    rationale: str
    scope: str = MemoryScope.REPOSITORY.value
    category: str = "architecture"
    status: str = "accepted"
    impact_areas: list[str] = Field(default_factory=list)
    affected_symbols: list[str] = Field(default_factory=list)
    team_id: str | None = None
    envelope: EnvelopeSchema = Field(default_factory=EnvelopeSchema)


class StoreFactBody(BaseModel):
    repository_id: str
    title: str
    content: str
    category: str = "implementation_summary"
    scope: str = MemoryScope.REPOSITORY.value
    actionable_takeaways: list[str] = Field(default_factory=list)
    impact_areas: list[str] = Field(default_factory=list)
    team_id: str | None = None
    envelope: EnvelopeSchema = Field(default_factory=EnvelopeSchema)


class StoreSessionSummaryBody(BaseModel):
    repository_id: str
    prompt: str
    response_summary: str
    affected_files: list[str] = Field(default_factory=list)
    affected_symbols: list[str] = Field(default_factory=list)
    engineering_decisions: list[str] = Field(default_factory=list)
    visibility: str = MemoryScope.REPOSITORY.value
    team_id: str | None = None
    store_raw_transcript: bool = False
    raw_messages: list[dict[str, Any]] | None = None
    envelope: EnvelopeSchema = Field(default_factory=EnvelopeSchema)


class ScopedRetrievalBody(BaseModel):
    repository_id: str
    query: str
    allowed_scopes: list[str] = Field(default_factory=lambda: [MemoryScope.REPOSITORY.value, MemoryScope.TEAM.value])
    category_filter: list[str] | None = None
    limit: int = 10
    envelope: EnvelopeSchema = Field(default_factory=EnvelopeSchema)


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.get("/health", tags=["System"])
@app.get("/api/v1/health", tags=["System"])
async def health_check():
    """Service health and capability status."""
    return {
        "status": "ok",
        "service": "synapse-session-context-api",
        "version": CURRENT_PROTOCOL_VERSION,
        "mode": "development-only",
        "timestamp": utc_now_iso(),
    }


@app.get("/api/v1/status", tags=["System"])
async def service_status(
    engine: SessionProtocolEngine = Depends(get_protocol_engine),
):
    """Detailed runtime status including active sessions and supported protocol versions."""
    return {
        "status": "ok",
        "active_sessions_count": len(engine._sessions),
        "protocol_version": CURRENT_PROTOCOL_VERSION,
        "supported_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
        "database_connected": engine.doc_store is not None,
        "orchestrator_connected": engine.orchestrator is not None,
        "timestamp": utc_now_iso(),
    }


# --- Session Lifecycle ---

@app.post("/api/v1/sessions", tags=["Sessions"], status_code=status.HTTP_201_CREATED)
async def create_session(
    body: CreateSessionBody,
    request: Request,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    engine: SessionProtocolEngine = Depends(get_protocol_engine),
):
    """Create a new engineering session bound to repository and client."""
    envelope = ProtocolEnvelope(
        protocol_version=body.envelope.protocol_version,
        request_id=body.envelope.request_id or getattr(request.state, "request_id", "req_new"),
        timestamp=body.envelope.timestamp or utc_now_iso(),
        idempotency_key=body.envelope.idempotency_key,
    )
    repo_id = RepositoryIdentity(
        repository_id=body.repository.repository_id,
        name=body.repository.name or body.repository.repository_id,
        root_path=body.repository.root_path,
        branch=body.repository.branch,
        commit_sha=body.repository.commit_sha,
        remote_url=body.repository.remote_url,
    )
    client_id = ClientIdentity(
        client_type=body.client.client_type,
        client_version=body.client.client_version,
        assistant_name=body.client.assistant_name,
        assistant_version=body.client.assistant_version,
        environment=body.client.environment,
    )
    proto_req = SessionCreateRequest(
        envelope=envelope,
        repository=repo_id,
        client=client_id,
        title=body.title,
        ttl_seconds=body.ttl_seconds,
        metadata=body.metadata,
    )
    res = engine.create_session(principal, proto_req)
    return res.to_dict()


@app.get("/api/v1/sessions/{session_id}", tags=["Sessions"])
@app.post("/api/v1/sessions/{session_id}/resume", tags=["Sessions"])
async def resume_session(
    session_id: str,
    request: Request,
    body: ResumeSessionBody | None = None,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    engine: SessionProtocolEngine = Depends(get_protocol_engine),
):
    """Resume or inspect an active session."""
    body_data = body or ResumeSessionBody()
    envelope = ProtocolEnvelope(
        protocol_version=body_data.envelope.protocol_version,
        request_id=body_data.envelope.request_id or getattr(request.state, "request_id", "req_new"),
        timestamp=body_data.envelope.timestamp or utc_now_iso(),
    )
    client_id = ClientIdentity(
        client_type=body_data.client.client_type,
        client_version=body_data.client.client_version,
        assistant_name=body_data.client.assistant_name,
        assistant_version=body_data.client.assistant_version,
        environment=body_data.client.environment,
    )
    proto_req = SessionResumeRequest(
        envelope=envelope,
        session_id=session_id,
        client=client_id,
    )
    res = engine.resume_session(principal, proto_req)
    return res.to_dict()


@app.post("/api/v1/sessions/{session_id}/close", tags=["Sessions"])
async def close_session(
    session_id: str,
    body: CloseSessionBody,
    request: Request,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    engine: SessionProtocolEngine = Depends(get_protocol_engine),
):
    """Close an active session, finalizing its summary and preventing further mutations."""
    envelope = ProtocolEnvelope(
        protocol_version=body.envelope.protocol_version,
        request_id=body.envelope.request_id or getattr(request.state, "request_id", "req_new"),
        timestamp=body.envelope.timestamp or utc_now_iso(),
    )
    proto_req = SessionCloseRequest(
        envelope=envelope,
        session_id=session_id,
        reason=body.reason,
        summary=body.summary,
    )
    res = engine.close_session(principal, proto_req)
    return res.to_dict()


# --- Context Query ---

@app.post("/api/v1/sessions/{session_id}/context", tags=["Context"])
async def query_context(
    session_id: str,
    body: ContextQueryBody,
    request: Request,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    engine: SessionProtocolEngine = Depends(get_protocol_engine),
):
    """Retrieve budgeted, orchestrated context for the active session via ACO."""
    envelope = ProtocolEnvelope(
        protocol_version=body.envelope.protocol_version,
        request_id=body.envelope.request_id or getattr(request.state, "request_id", "req_new"),
        timestamp=body.envelope.timestamp or utc_now_iso(),
    )
    proto_req = ContextQueryRequest(
        envelope=envelope,
        session_id=session_id,
        repository_id=body.repository_id,
        query=body.query,
        max_tokens=body.max_tokens,
        optimization_mode=body.optimization_mode,
        active_file=body.active_file,
        cursor_position=body.cursor_position,
        include_scratchpad=body.include_scratchpad,
    )
    res = engine.query_context(principal, proto_req)
    return res.to_dict()


# --- Memory & Decision Writes ---

@app.post("/api/v1/sessions/{session_id}/decisions", tags=["Memory"], status_code=status.HTTP_201_CREATED)
async def store_decision(
    session_id: str,
    body: StoreDecisionBody,
    request: Request,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    engine: SessionProtocolEngine = Depends(get_protocol_engine),
):
    """Persist an architectural or engineering decision (ADR) under repository or team scope."""
    envelope = ProtocolEnvelope(
        protocol_version=body.envelope.protocol_version,
        request_id=body.envelope.request_id or getattr(request.state, "request_id", "req_new"),
        timestamp=body.envelope.timestamp or utc_now_iso(),
        idempotency_key=body.envelope.idempotency_key,
    )
    proto_req = StoreDecisionRequest(
        envelope=envelope,
        session_id=session_id,
        repository_id=body.repository_id,
        title=body.title,
        rationale=body.rationale,
        scope=body.scope,
        category=body.category,
        status=body.status,
        impact_areas=body.impact_areas,
        affected_symbols=body.affected_symbols,
        team_id=body.team_id,
    )
    res = engine.store_decision(principal, proto_req)
    return res.to_dict()


@app.post("/api/v1/sessions/{session_id}/facts", tags=["Memory"], status_code=status.HTTP_201_CREATED)
async def store_fact(
    session_id: str,
    body: StoreFactBody,
    request: Request,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    engine: SessionProtocolEngine = Depends(get_protocol_engine),
):
    """Persist a technical recipe, recurring solution pattern, or bug root cause."""
    envelope = ProtocolEnvelope(
        protocol_version=body.envelope.protocol_version,
        request_id=body.envelope.request_id or getattr(request.state, "request_id", "req_new"),
        timestamp=body.envelope.timestamp or utc_now_iso(),
        idempotency_key=body.envelope.idempotency_key,
    )
    proto_req = StoreFactRequest(
        envelope=envelope,
        session_id=session_id,
        repository_id=body.repository_id,
        title=body.title,
        content=body.content,
        category=body.category,
        scope=body.scope,
        actionable_takeaways=body.actionable_takeaways,
        impact_areas=body.impact_areas,
        team_id=body.team_id,
    )
    res = engine.store_fact(principal, proto_req)
    return res.to_dict()


@app.post("/api/v1/sessions/{session_id}/distill", tags=["Memory"])
async def distill_session_interaction(
    session_id: str,
    body: StoreSessionSummaryBody,
    request: Request,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    engine: SessionProtocolEngine = Depends(get_protocol_engine),
):
    """Distill session interaction into reusable engineering memory cards."""
    envelope = ProtocolEnvelope(
        protocol_version=body.envelope.protocol_version,
        request_id=body.envelope.request_id or getattr(request.state, "request_id", "req_new"),
        timestamp=body.envelope.timestamp or utc_now_iso(),
    )
    proto_req = StoreSessionSummaryRequest(
        envelope=envelope,
        session_id=session_id,
        repository_id=body.repository_id,
        prompt=body.prompt,
        response_summary=body.response_summary,
        affected_files=body.affected_files,
        affected_symbols=body.affected_symbols,
        engineering_decisions=body.engineering_decisions,
        store_raw_transcript=body.store_raw_transcript,
        raw_messages=body.raw_messages,
        visibility=body.visibility,
        team_id=body.team_id,
    )
    res = engine.store_session_summary(principal, proto_req)
    return res.to_dict()


# --- Scoped Memory Retrieval ---

@app.post("/api/v1/memories/query", tags=["Memory"])
async def query_scoped_memories(
    body: ScopedRetrievalBody,
    request: Request,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    engine: SessionProtocolEngine = Depends(get_protocol_engine),
):
    """Query stored engineering memories within permitted scopes and categories."""
    envelope = ProtocolEnvelope(
        protocol_version=body.envelope.protocol_version,
        request_id=body.envelope.request_id or getattr(request.state, "request_id", "req_new"),
        timestamp=body.envelope.timestamp or utc_now_iso(),
    )
    proto_req = ScopedRetrievalRequest(
        envelope=envelope,
        repository_id=body.repository_id,
        query=body.query,
        allowed_scopes=body.allowed_scopes,
        category_filter=body.category_filter,
        limit=body.limit,
    )
    res = engine.scoped_retrieval(principal, proto_req)
    return res.to_dict()


# ---------------------------------------------------------------------------
# Auth & Security Management Endpoints (Phase 9)
# ---------------------------------------------------------------------------

class CreateApiKeyBody(BaseModel):
    name: str = Field(..., max_length=128)
    roles: list[str] = Field(default_factory=lambda: ["developer"])
    allowed_repositories: list[str] = Field(default_factory=lambda: ["*"])
    allowed_teams: list[str] = Field(default_factory=list)
    expires_at: str | None = None


@app.get("/api/v1/auth/me", tags=["Authentication"])
async def get_my_principal(
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
):
    """Retrieve verified identity, active roles, and granted permissions."""
    return principal.to_dict()


@app.get("/api/v1/security/audit", tags=["Security"])
async def list_security_audit_events(
    tenant_id: str | None = None,
    principal_id: str | None = None,
    event_type: str | None = None,
    limit: int = 50,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    engine: SessionProtocolEngine = Depends(get_protocol_engine),
):
    """List security audit events (restricted to admin or maintainer roles)."""
    if not principal.has_permission(Permission.AUDIT_READ.value):
        raise AccessDeniedError(
            f"Principal '{principal.principal_id}' lacks permission '{Permission.AUDIT_READ.value}' to inspect security audit events.",
            details={"principal_id": principal.principal_id, "required_permission": Permission.AUDIT_READ.value},
        )
    events = engine.audit_logger.list_events(
        tenant_id=tenant_id or principal.tenant_id,
        principal_id=principal_id,
        event_type=event_type,
        limit=limit,
    )
    return [e.to_dict() if hasattr(e, "to_dict") else e for e in events]


@app.post("/api/v1/auth/api-keys", tags=["Authentication"], status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: CreateApiKeyBody,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    engine: SessionProtocolEngine = Depends(get_protocol_engine),
):
    """Generate high-entropy API key (restricted to admin or maintainer)."""
    if not principal.has_role("admin") and not principal.has_role("maintainer"):
        raise AccessDeniedError(
            f"Principal '{principal.principal_id}' is not authorized to generate API keys.",
            details={"principal_id": principal.principal_id},
        )
    api_key_svc = ApiKeyService(doc_store=engine.doc_store)
    raw_key, record = api_key_svc.create_api_key(
        name=body.name,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        roles=body.roles,
        allowed_repositories=body.allowed_repositories,
        allowed_teams=body.allowed_teams,
        expires_at=body.expires_at,
    )
    return {
        "api_key": raw_key,
        "key_record": record.to_dict(),
        "warning": "Store this API key securely. It will NEVER be displayed or retrievable again.",
    }


@app.delete("/api/v1/auth/api-keys/{key_id}", tags=["Authentication"])
async def revoke_api_key(
    key_id: str,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    engine: SessionProtocolEngine = Depends(get_protocol_engine),
):
    """Revoke an active API key (restricted to admin or maintainer)."""
    if not principal.has_role("admin") and not principal.has_role("maintainer"):
        raise AccessDeniedError(
            f"Principal '{principal.principal_id}' is not authorized to revoke API keys.",
            details={"principal_id": principal.principal_id},
        )
    api_key_svc = ApiKeyService(doc_store=engine.doc_store)
    revoked = api_key_svc.revoke_api_key(key_id)
    if not revoked:
        raise ProtocolException(
            code=ErrorCode.MALFORMED_INPUT,
            message=f"API key '{key_id}' not found or already revoked.",
        )
    return {"key_id": key_id, "revoked": True}

