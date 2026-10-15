"""Provider-neutral, versioned session protocol for Adaptive Engineering Memory.

Defines schemas, identity scopes, validation, secret redaction, idempotency,
and lifecycle operations across heterogeneous IDEs and AI assistants
(VS Code, JetBrains, Cursor, Claude Code, Antigravity, CLI).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field as dc_field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Protocol Version & Constants
# ---------------------------------------------------------------------------

CURRENT_PROTOCOL_VERSION: str = "1.0.0"
SUPPORTED_PROTOCOL_VERSIONS: set[str] = {"1.0.0"}

MAX_QUERY_LENGTH: int = 8192
MAX_PROMPT_LENGTH: int = 16384
MAX_CONTENT_LENGTH: int = 65536
MAX_TITLE_LENGTH: int = 256
MAX_BATCH_SIZE: int = 100
MAX_METADATA_BYTES: int = 65536
DEFAULT_SESSION_TTL_SECONDS: int = 86400  # 24 hours


def utc_now_iso() -> str:
    """Return ISO 8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SessionStatus(str, Enum):
    """Lifecycle status of an engineering session."""
    ACTIVE = "active"
    RESUMED = "resumed"
    CLOSED = "closed"
    EXPIRED = "expired"


class ClientType(str, Enum):
    """Recognized client environments."""
    VSCODE = "vscode"
    JETBRAINS = "jetbrains"
    CURSOR = "cursor"
    CLAUDE_CODE = "claude_code"
    ANTIGRAVITY = "antigravity"
    CLI = "cli"
    CUSTOM = "custom"


class MemoryScope(str, Enum):
    """Visibility and permission boundary for memories and decisions."""
    PERSONAL = "personal"
    TEAM = "team"
    REPOSITORY = "repository"


class ErrorCode(str, Enum):
    """Standardized error codes for protocol responses."""
    MALFORMED_INPUT = "MALFORMED_INPUT"
    UNSUPPORTED_PROTOCOL_VERSION = "UNSUPPORTED_PROTOCOL_VERSION"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    ACCESS_DENIED = "ACCESS_DENIED"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    SESSION_CLOSED = "SESSION_CLOSED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    REPOSITORY_MISMATCH = "REPOSITORY_MISMATCH"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    INVALID_SCOPE = "INVALID_SCOPE"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# Protocol Exceptions
# ---------------------------------------------------------------------------

class ProtocolException(Exception):
    """Base class for session protocol errors."""

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        field: str | None = None,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = str(code.value if isinstance(code, ErrorCode) else code)
        self.message = message
        self.field = field
        self.details = details or {}
        self.retryable = retryable

    def to_error_detail(self) -> ProtocolErrorDetail:
        """Convert exception to structured error detail."""
        return ProtocolErrorDetail(
            code=self.code,
            message=self.message,
            field=self.field,
            details=self.details,
            retryable=self.retryable,
        )


class MalformedInputError(ProtocolException):
    """Raised when an input payload fails schema or length validation."""
    def __init__(self, message: str, field: str | None = None, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.MALFORMED_INPUT, message, field=field, details=details, retryable=False)


class UnsupportedProtocolVersionError(ProtocolException):
    """Raised when the client requests an unsupported protocol version."""
    def __init__(self, requested_version: str) -> None:
        super().__init__(
            ErrorCode.UNSUPPORTED_PROTOCOL_VERSION,
            f"Requested protocol version '{requested_version}' is not supported. Supported: {sorted(SUPPORTED_PROTOCOL_VERSIONS)}",
            field="protocol_version",
            details={"requested_version": requested_version, "supported_versions": list(SUPPORTED_PROTOCOL_VERSIONS)},
            retryable=False,
        )


class AuthenticationRequiredError(ProtocolException):
    """Raised when an operation requires an authenticated principal."""
    def __init__(self, message: str = "Authenticated principal is required.") -> None:
        super().__init__(ErrorCode.AUTHENTICATION_REQUIRED, message, retryable=False)


class AccessDeniedError(ProtocolException):
    """Raised when a principal is not authorized for the requested repository or scope."""
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.ACCESS_DENIED, message, details=details, retryable=False)


class SessionNotFoundError(ProtocolException):
    """Raised when the specified session ID does not exist."""
    def __init__(self, session_id: str) -> None:
        super().__init__(
            ErrorCode.SESSION_NOT_FOUND,
            f"Session '{session_id}' not found.",
            field="session_id",
            details={"session_id": session_id},
            retryable=False,
        )


class SessionClosedError(ProtocolException):
    """Raised when attempting an active operation on a closed session."""
    def __init__(self, session_id: str, closed_at: str | None = None) -> None:
        super().__init__(
            ErrorCode.SESSION_CLOSED,
            f"Session '{session_id}' is closed and cannot accept new operations.",
            field="session_id",
            details={"session_id": session_id, "closed_at": closed_at},
            retryable=False,
        )


class SessionExpiredError(ProtocolException):
    """Raised when a session TTL has elapsed."""
    def __init__(self, session_id: str, expires_at: str | None = None) -> None:
        super().__init__(
            ErrorCode.SESSION_EXPIRED,
            f"Session '{session_id}' has expired.",
            field="session_id",
            details={"session_id": session_id, "expires_at": expires_at},
            retryable=False,
        )


class RepositoryMismatchError(ProtocolException):
    """Raised when an operation repository does not match the bound session repository."""
    def __init__(self, session_id: str, expected_repo: str, provided_repo: str) -> None:
        super().__init__(
            ErrorCode.REPOSITORY_MISMATCH,
            f"Repository '{provided_repo}' does not match session '{session_id}' repository '{expected_repo}'.",
            field="repository_id",
            details={"session_id": session_id, "expected_repository_id": expected_repo, "provided_repository_id": provided_repo},
            retryable=False,
        )


class IdempotencyConflictError(ProtocolException):
    """Raised when an idempotency key is reused with a conflicting payload."""
    def __init__(self, idempotency_key: str) -> None:
        super().__init__(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            f"Idempotency key '{idempotency_key}' was previously used with a different payload.",
            field="idempotency_key",
            details={"idempotency_key": idempotency_key},
            retryable=False,
        )


class PayloadTooLargeError(ProtocolException):
    """Raised when input text exceeds allowed limits."""
    def __init__(self, field_name: str, actual_len: int, max_len: int) -> None:
        super().__init__(
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"Field '{field_name}' length ({actual_len}) exceeds maximum allowed ({max_len}).",
            field=field_name,
            details={"field": field_name, "actual_length": actual_len, "max_allowed": max_len},
            retryable=False,
        )


class InvalidScopeError(ProtocolException):
    """Raised when a memory or decision scope is invalid for the requesting principal."""
    def __init__(self, scope: str, reason: str) -> None:
        super().__init__(
            ErrorCode.INVALID_SCOPE,
            f"Scope '{scope}' is invalid: {reason}",
            field="scope",
            details={"scope": scope, "reason": reason},
            retryable=False,
        )


# ---------------------------------------------------------------------------
# Secret Redaction / Privacy Protection
# ---------------------------------------------------------------------------

class SecretRedactor:
    """Detects and redacts sensitive credentials, tokens, and keys from technical text."""

    PATTERNS: list[tuple[re.Pattern, str]] = [
        # OpenAI / generic sk keys
        (re.compile(r"\b(sk-[a-zA-Z0-9_\-]{20,})\b"), "[REDACTED_API_KEY]"),
        # Anthropic keys
        (re.compile(r"\b(sk-ant-[a-zA-Z0-9_\-]{20,})\b"), "[REDACTED_API_KEY]"),
        # GitHub Personal Access Tokens
        (re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{36,})\b"), "[REDACTED_GITHUB_TOKEN]"),
        (re.compile(r"\b(github_pat_[A-Za-z0-9_]{22}_[A-Za-z0-9_]{59})\b"), "[REDACTED_GITHUB_TOKEN]"),
        # AWS Access Keys
        (re.compile(r"\b(AKIA[0-9A-Z]{16})\b"), "[REDACTED_AWS_KEY]"),
        # AWS Secret Access Keys
        (re.compile(r"(?i)(?:aws_secret_access_key|secret_key)\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?"), "[REDACTED_AWS_SECRET]"),
        # JWT tokens (eyJ...)
        (re.compile(r"\b(eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,})\b"), "[REDACTED_JWT_TOKEN]"),
        # Private Keys
        (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
        # Password in URLs
        (re.compile(r"(https?://[^:\s]+):([^@\s/]+)@"), r"\1:[REDACTED_PASSWORD]@"),
    ]

    @classmethod
    def redact(cls, text: str | None) -> str:
        """Scan text and replace all secret occurrences with safe placeholders."""
        if not text:
            return ""
        result = text
        for pattern, replacement in cls.PATTERNS:
            result = pattern.sub(replacement, result)
        return result

    @classmethod
    def redact_dict(cls, data: Any) -> Any:
        """Recursively redact secrets in nested dictionaries and lists."""
        if isinstance(data, str):
            return cls.redact(data)
        if isinstance(data, dict):
            return {k: cls.redact_dict(v) for k, v in data.items()}
        if isinstance(data, list):
            return [cls.redact_dict(item) for item in data]
        return data


# ---------------------------------------------------------------------------
# Identity & Scope Models (Trust Boundaries)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """Verified identity asserted by server auth middleware (NOT client-supplied)."""

    principal_id: str
    tenant_id: str
    team_ids: list[str] = dc_field(default_factory=list)
    allowed_repositories: list[str] = dc_field(default_factory=list)
    roles: list[str] = dc_field(default_factory=lambda: ["developer"])
    is_authenticated: bool = True

    def can_access_repository(self, repository_id: str) -> bool:
        """Check whether the principal is authorized to access a repository."""
        if not self.is_authenticated:
            return False
        if "*" in self.allowed_repositories:
            return True
        return repository_id in self.allowed_repositories

    def can_access_team(self, team_id: str) -> bool:
        """Check whether the principal belongs to the specified team."""
        if not self.is_authenticated:
            return False
        return team_id in self.team_ids

    def has_role(self, role: str) -> bool:
        """Check whether principal possesses the specified role (or admin)."""
        if not self.is_authenticated:
            return False
        return role in self.roles or "admin" in self.roles

    def has_permission(self, permission: str) -> bool:
        """Check whether principal's roles grant the requested permission."""
        if not self.is_authenticated:
            return False
        from .security import compute_effective_permissions, Permission
        effective = compute_effective_permissions(self.roles)
        if Permission.ADMIN_ALL.value in effective or "*" in effective:
            return True
        return permission in effective

    def can_perform(
        self,
        permission: str,
        repository_id: str | None = None,
        team_id: str | None = None,
    ) -> bool:
        """Evaluate permission, repository access, and team boundaries in one call."""
        if not self.is_authenticated:
            return False
        if not self.has_permission(permission):
            return False
        if repository_id is not None and not self.can_access_repository(repository_id):
            return False
        if team_id is not None and not self.can_access_team(team_id):
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        """Serialize principal to dictionary."""
        from .security import compute_effective_permissions
        return {
            "principal_id": self.principal_id,
            "tenant_id": self.tenant_id,
            "team_ids": list(self.team_ids),
            "allowed_repositories": list(self.allowed_repositories),
            "roles": list(self.roles),
            "permissions": sorted(list(compute_effective_permissions(self.roles))),
            "is_authenticated": self.is_authenticated,
        }


@dataclass(frozen=True)
class ClientIdentity:
    """Client / IDE / Assistant telemetry provided by the caller."""

    client_type: str = ClientType.CUSTOM.value
    client_version: str = "1.0.0"
    assistant_name: str = "generic-assistant"
    assistant_version: str | None = None
    environment: dict[str, str] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize client identity."""
        return {
            "client_type": self.client_type,
            "client_version": self.client_version,
            "assistant_name": self.assistant_name,
            "assistant_version": self.assistant_version,
            "environment": dict(self.environment),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClientIdentity:
        """Construct from dictionary."""
        return cls(
            client_type=str(data.get("client_type", ClientType.CUSTOM.value)),
            client_version=str(data.get("client_version", "1.0.0")),
            assistant_name=str(data.get("assistant_name", "generic-assistant")),
            assistant_version=data.get("assistant_version"),
            environment=dict(data.get("environment", {})),
        )


@dataclass(frozen=True)
class RepositoryIdentity:
    """Identifies the target repository and worktree."""

    repository_id: str
    name: str = ""
    root_path: str | None = None
    branch: str | None = None
    commit_sha: str | None = None
    remote_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize repository identity."""
        return {
            "repository_id": self.repository_id,
            "name": self.name or self.repository_id,
            "root_path": self.root_path,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "remote_url": self.remote_url,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RepositoryIdentity:
        """Construct from dictionary."""
        repo_id = str(data.get("repository_id") or data.get("id") or "")
        return cls(
            repository_id=repo_id,
            name=str(data.get("name", repo_id)),
            root_path=data.get("root_path"),
            branch=data.get("branch"),
            commit_sha=data.get("commit_sha"),
            remote_url=data.get("remote_url"),
        )


@dataclass
class ProtocolEnvelope:
    """Envelope standard metadata included on all requests and responses."""

    protocol_version: str = CURRENT_PROTOCOL_VERSION
    request_id: str = dc_field(default_factory=lambda: f"req_{uuid.uuid4().hex[:12]}")
    timestamp: str = dc_field(default_factory=utc_now_iso)
    idempotency_key: str | None = None

    def validate(self) -> None:
        """Validate envelope integrity."""
        if not self.protocol_version or self.protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise UnsupportedProtocolVersionError(self.protocol_version or "missing")
        if not self.request_id or not self.request_id.strip():
            raise MalformedInputError("request_id cannot be empty.", field="request_id")

    def to_dict(self) -> dict[str, Any]:
        """Serialize envelope."""
        return {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "timestamp": self.timestamp,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProtocolEnvelope:
        """Construct from dictionary."""
        return cls(
            protocol_version=str(data.get("protocol_version", CURRENT_PROTOCOL_VERSION)),
            request_id=str(data.get("request_id") or f"req_{uuid.uuid4().hex[:12]}"),
            timestamp=str(data.get("timestamp") or utc_now_iso()),
            idempotency_key=data.get("idempotency_key"),
        )


# ---------------------------------------------------------------------------
# Error Detail & Response
# ---------------------------------------------------------------------------

@dataclass
class ProtocolErrorDetail:
    """Structured machine-readable and human-readable error payload."""

    code: str
    message: str
    field: str | None = None
    details: dict[str, Any] = dc_field(default_factory=dict)
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize error detail."""
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "details": dict(self.details),
            "retryable": self.retryable,
        }


@dataclass
class ProtocolErrorResponse:
    """Standard error envelope returned on any protocol violation."""

    envelope: ProtocolEnvelope
    error: ProtocolErrorDetail
    success: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize error response."""
        return {
            "success": False,
            "envelope": self.envelope.to_dict(),
            "error": self.error.to_dict(),
        }


# ---------------------------------------------------------------------------
# Session Protocol Operation Models
# ---------------------------------------------------------------------------

@dataclass
class SessionCreateRequest:
    """Request to create a new engineering session bound to repository and client."""

    envelope: ProtocolEnvelope
    repository: RepositoryIdentity
    client: ClientIdentity = dc_field(default_factory=ClientIdentity)
    title: str = ""
    ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
    metadata: dict[str, Any] = dc_field(default_factory=dict)

    def validate(self) -> None:
        """Validate create request fields."""
        self.envelope.validate()
        if not self.repository.repository_id or not self.repository.repository_id.strip():
            raise MalformedInputError("repository.repository_id is required.", field="repository.repository_id")
        if len(self.title) > MAX_TITLE_LENGTH:
            raise PayloadTooLargeError("title", len(self.title), MAX_TITLE_LENGTH)
        if self.ttl_seconds <= 0:
            raise MalformedInputError("ttl_seconds must be positive.", field="ttl_seconds")


@dataclass
class SessionCreateResponse:
    """Response returned upon successful session creation."""

    envelope: ProtocolEnvelope
    session_id: str
    status: str
    repository_id: str
    created_at: str
    expires_at: str
    metadata: dict[str, Any] = dc_field(default_factory=dict)
    success: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize response."""
        return {
            "success": self.success,
            "envelope": self.envelope.to_dict(),
            "session_id": self.session_id,
            "status": self.status,
            "repository_id": self.repository_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "metadata": dict(self.metadata),
        }


@dataclass
class SessionResumeRequest:
    """Request to resume or inspect an existing session."""

    envelope: ProtocolEnvelope
    session_id: str
    client: ClientIdentity = dc_field(default_factory=ClientIdentity)

    def validate(self) -> None:
        """Validate resume request."""
        self.envelope.validate()
        if not self.session_id or not self.session_id.strip():
            raise MalformedInputError("session_id is required.", field="session_id")


@dataclass
class SessionResumeResponse:
    """Response returned upon successfully resuming a session."""

    envelope: ProtocolEnvelope
    session_id: str
    status: str
    repository_id: str
    started_at: str
    expires_at: str
    query_count: int
    title: str = ""
    summary: str = ""
    metadata: dict[str, Any] = dc_field(default_factory=dict)
    success: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize response."""
        return {
            "success": self.success,
            "envelope": self.envelope.to_dict(),
            "session_id": self.session_id,
            "status": self.status,
            "repository_id": self.repository_id,
            "started_at": self.started_at,
            "expires_at": self.expires_at,
            "query_count": self.query_count,
            "title": self.title,
            "summary": self.summary,
            "metadata": dict(self.metadata),
        }


@dataclass
class SessionCloseRequest:
    """Request to close an active session and finalize its summary."""

    envelope: ProtocolEnvelope
    session_id: str
    reason: str = "completed"
    summary: str = ""

    def validate(self) -> None:
        """Validate close request."""
        self.envelope.validate()
        if not self.session_id or not self.session_id.strip():
            raise MalformedInputError("session_id is required.", field="session_id")
        if len(self.summary) > MAX_CONTENT_LENGTH:
            raise PayloadTooLargeError("summary", len(self.summary), MAX_CONTENT_LENGTH)


@dataclass
class SessionCloseResponse:
    """Response returned upon closing a session."""

    envelope: ProtocolEnvelope
    session_id: str
    status: str
    closed_at: str
    summary: str
    success: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize response."""
        return {
            "success": self.success,
            "envelope": self.envelope.to_dict(),
            "session_id": self.session_id,
            "status": self.status,
            "closed_at": self.closed_at,
            "summary": self.summary,
        }


# ---------------------------------------------------------------------------
# Context Query Models
# ---------------------------------------------------------------------------

@dataclass
class ContextQueryRequest:
    """Request to retrieve budgeted, orchestrated engineering context for an LLM."""

    envelope: ProtocolEnvelope
    session_id: str
    repository_id: str
    query: str
    max_tokens: int = 1500
    optimization_mode: str = "deterministic"
    intent_override: str | None = None
    active_file: str | None = None
    cursor_position: int | None = None
    include_scratchpad: bool = False

    def validate(self) -> None:
        """Validate context query parameters."""
        self.envelope.validate()
        if not self.session_id or not self.session_id.strip():
            raise MalformedInputError("session_id is required.", field="session_id")
        if not self.repository_id or not self.repository_id.strip():
            raise MalformedInputError("repository_id is required.", field="repository_id")
        if not self.query or not self.query.strip():
            raise MalformedInputError("query cannot be empty.", field="query")
        if len(self.query) > MAX_QUERY_LENGTH:
            raise PayloadTooLargeError("query", len(self.query), MAX_QUERY_LENGTH)
        if self.max_tokens <= 0:
            raise MalformedInputError("max_tokens must be positive.", field="max_tokens")
        if self.active_file:
            from .security_hardening import SafePathValidator
            SafePathValidator.validate_safe_relative_path(self.active_file)


@dataclass
class ContextQueryResponse:
    """Response containing orchestrated context and token metrics."""

    envelope: ProtocolEnvelope
    session_id: str
    repository_id: str
    intent: str
    formatted_context: str
    token_budget: int
    tokens_used: int
    budget_utilization_pct: float
    items_packed: int
    items_retained_by_category: dict[str, int]
    provenance: list[dict[str, Any]]
    latency_ms: float
    success: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize context query response."""
        return {
            "success": self.success,
            "envelope": self.envelope.to_dict(),
            "session_id": self.session_id,
            "repository_id": self.repository_id,
            "intent": self.intent,
            "formatted_context": self.formatted_context,
            "token_budget": self.token_budget,
            "tokens_used": self.tokens_used,
            "budget_utilization_pct": self.budget_utilization_pct,
            "items_packed": self.items_packed,
            "items_retained_by_category": dict(self.items_retained_by_category),
            "provenance": list(self.provenance),
            "latency_ms": self.latency_ms,
        }


# ---------------------------------------------------------------------------
# Memory / Decision Store Models
# ---------------------------------------------------------------------------

@dataclass
class StoreDecisionRequest:
    """Request to persist an accepted architectural or engineering decision."""

    envelope: ProtocolEnvelope
    session_id: str
    repository_id: str
    title: str
    rationale: str
    scope: str = MemoryScope.REPOSITORY.value
    category: str = "architecture"
    status: str = "accepted"
    impact_areas: list[str] = dc_field(default_factory=list)
    affected_symbols: list[str] = dc_field(default_factory=list)
    team_id: str | None = None

    def validate(self) -> None:
        """Validate decision store request."""
        self.envelope.validate()
        if not self.session_id or not self.session_id.strip():
            raise MalformedInputError("session_id is required.", field="session_id")
        if not self.repository_id or not self.repository_id.strip():
            raise MalformedInputError("repository_id is required.", field="repository_id")
        if not self.title or not self.title.strip():
            raise MalformedInputError("title cannot be empty.", field="title")
        if len(self.title) > MAX_TITLE_LENGTH:
            raise PayloadTooLargeError("title", len(self.title), MAX_TITLE_LENGTH)
        if not self.rationale or not self.rationale.strip():
            raise MalformedInputError("rationale cannot be empty.", field="rationale")
        if len(self.rationale) > MAX_CONTENT_LENGTH:
            raise PayloadTooLargeError("rationale", len(self.rationale), MAX_CONTENT_LENGTH)
        valid_scopes = {s.value for s in MemoryScope}
        if self.scope not in valid_scopes:
            raise InvalidScopeError(self.scope, f"Must be one of {sorted(valid_scopes)}")


@dataclass
class StoreDecisionResponse:
    """Response returned upon storing an engineering decision."""

    envelope: ProtocolEnvelope
    decision_id: str
    status: str = "stored"
    scope: str = MemoryScope.REPOSITORY.value
    success: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize response."""
        return {
            "success": self.success,
            "envelope": self.envelope.to_dict(),
            "decision_id": self.decision_id,
            "status": self.status,
            "scope": self.scope,
        }


@dataclass
class StoreFactRequest:
    """Request to persist a technical fact, recipe, or bug root cause."""

    envelope: ProtocolEnvelope
    session_id: str
    repository_id: str
    title: str
    content: str
    category: str = "implementation_summary"
    scope: str = MemoryScope.REPOSITORY.value
    actionable_takeaways: list[str] = dc_field(default_factory=list)
    impact_areas: list[str] = dc_field(default_factory=list)
    team_id: str | None = None

    def validate(self) -> None:
        """Validate fact store request."""
        self.envelope.validate()
        if not self.session_id or not self.session_id.strip():
            raise MalformedInputError("session_id is required.", field="session_id")
        if not self.repository_id or not self.repository_id.strip():
            raise MalformedInputError("repository_id is required.", field="repository_id")
        if not self.title or not self.title.strip():
            raise MalformedInputError("title cannot be empty.", field="title")
        if len(self.title) > MAX_TITLE_LENGTH:
            raise PayloadTooLargeError("title", len(self.title), MAX_TITLE_LENGTH)
        if not self.content or not self.content.strip():
            raise MalformedInputError("content cannot be empty.", field="content")
        if len(self.content) > MAX_CONTENT_LENGTH:
            raise PayloadTooLargeError("content", len(self.content), MAX_CONTENT_LENGTH)
        valid_scopes = {s.value for s in MemoryScope}
        if self.scope not in valid_scopes:
            raise InvalidScopeError(self.scope, f"Must be one of {sorted(valid_scopes)}")


@dataclass
class StoreFactResponse:
    """Response returned upon storing a fact or recurring solution."""

    envelope: ProtocolEnvelope
    memory_id: str
    status: str = "stored"
    success: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize response."""
        return {
            "success": self.success,
            "envelope": self.envelope.to_dict(),
            "memory_id": self.memory_id,
            "status": self.status,
        }


@dataclass
class StoreSessionSummaryRequest:
    """Request to distill an entire session interaction into reusable engineering memory."""

    envelope: ProtocolEnvelope
    session_id: str
    repository_id: str
    prompt: str
    response_summary: str
    affected_files: list[str] = dc_field(default_factory=list)
    affected_symbols: list[str] = dc_field(default_factory=list)
    engineering_decisions: list[str] = dc_field(default_factory=list)
    store_raw_transcript: bool = False
    raw_messages: list[dict[str, Any]] | None = None
    visibility: str = MemoryScope.REPOSITORY.value
    team_id: str | None = None

    def validate(self) -> None:
        """Validate session summary distillation request."""
        self.envelope.validate()
        if not self.session_id or not self.session_id.strip():
            raise MalformedInputError("session_id is required.", field="session_id")
        if not self.repository_id or not self.repository_id.strip():
            raise MalformedInputError("repository_id is required.", field="repository_id")
        if not self.prompt or not self.prompt.strip():
            raise MalformedInputError("prompt cannot be empty.", field="prompt")
        if len(self.prompt) > MAX_PROMPT_LENGTH:
            raise PayloadTooLargeError("prompt", len(self.prompt), MAX_PROMPT_LENGTH)
        if not self.response_summary or not self.response_summary.strip():
            raise MalformedInputError("response_summary cannot be empty.", field="response_summary")
        if len(self.response_summary) > MAX_CONTENT_LENGTH:
            raise PayloadTooLargeError("response_summary", len(self.response_summary), MAX_CONTENT_LENGTH)


@dataclass
class StoreSessionSummaryResponse:
    """Response returned upon distilling a session."""

    envelope: ProtocolEnvelope
    session_id: str
    distilled_memories_count: int
    distilled_decisions_count: int
    status: str = "recorded"
    success: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize response."""
        return {
            "success": self.success,
            "envelope": self.envelope.to_dict(),
            "session_id": self.session_id,
            "distilled_memories_count": self.distilled_memories_count,
            "distilled_decisions_count": self.distilled_decisions_count,
            "status": self.status,
        }


# ---------------------------------------------------------------------------
# Scoped Retrieval Models
# ---------------------------------------------------------------------------

@dataclass
class ScopedRetrievalRequest:
    """Request to query memories strictly within specified scopes and permissions."""

    envelope: ProtocolEnvelope
    repository_id: str
    query: str
    allowed_scopes: list[str] = dc_field(default_factory=lambda: [MemoryScope.REPOSITORY.value, MemoryScope.TEAM.value])
    category_filter: list[str] | None = None
    limit: int = 10

    def validate(self) -> None:
        """Validate scoped retrieval parameters."""
        self.envelope.validate()
        if not self.repository_id or not self.repository_id.strip():
            raise MalformedInputError("repository_id is required.", field="repository_id")
        if not self.query or not self.query.strip():
            raise MalformedInputError("query cannot be empty.", field="query")
        if len(self.query) > MAX_QUERY_LENGTH:
            raise PayloadTooLargeError("query", len(self.query), MAX_QUERY_LENGTH)
        if self.limit <= 0 or self.limit > MAX_BATCH_SIZE:
            raise MalformedInputError(f"limit must be between 1 and {MAX_BATCH_SIZE}.", field="limit")


@dataclass
class ScopedRetrievalResponse:
    """Response containing filtered memories with provenance."""

    envelope: ProtocolEnvelope
    repository_id: str
    query: str
    total_found: int
    memories: list[dict[str, Any]]
    scopes_included: list[str]
    success: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize scoped retrieval response."""
        return {
            "success": self.success,
            "envelope": self.envelope.to_dict(),
            "repository_id": self.repository_id,
            "query": self.query,
            "total_found": self.total_found,
            "memories": list(self.memories),
            "scopes_included": list(self.scopes_included),
        }


# ---------------------------------------------------------------------------
# Internal State Record
# ---------------------------------------------------------------------------

@dataclass
class SessionStateRecord:
    """Internal session tracking state stored in session engine."""

    session_id: str
    principal_id: str
    tenant_id: str
    repository_id: str
    client: ClientIdentity
    status: SessionStatus = SessionStatus.ACTIVE
    title: str = ""
    created_at: str = dc_field(default_factory=utc_now_iso)
    updated_at: str = dc_field(default_factory=utc_now_iso)
    expires_at: str = ""
    closed_at: str | None = None
    query_count: int = 0
    summary: str = ""
    metadata: dict[str, Any] = dc_field(default_factory=dict)

    def is_expired(self) -> bool:
        """Check whether the session has passed its expiration timestamp."""
        if not self.expires_at:
            return False
        try:
            exp_dt = datetime.fromisoformat(self.expires_at)
            return datetime.now(timezone.utc) > exp_dt
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Protocol Engine (Reference Implementation & Contract Verifier)
# ---------------------------------------------------------------------------

class SessionProtocolEngine:
    """Central engine executing and validating provider-neutral session operations.

    Enforces:
    1. Protocol version validation
    2. Principal authentication & repository authorization
    3. Secret redaction on all incoming/outgoing text
    4. Exact session-repository scope binding (cannot query/write to unassigned repo)
    5. Session lifecycle checks (closed/expired session rejection)
    6. Idempotency management (safe duplicate replays, conflict detection)
    7. Integration with ACO and Document/Session stores
    """

    def __init__(
        self,
        doc_store: Any | None = None,
        session_service: Any | None = None,
        orchestrator: Any | None = None,
        audit_logger: Any | None = None,
    ) -> None:
        self.doc_store = doc_store
        self.session_service = session_service
        self.orchestrator = orchestrator

        from .security import SecurityAuditLogger
        self.audit_logger = audit_logger or SecurityAuditLogger(doc_store=doc_store)

        # In-memory tracking for active sessions & idempotency records
        self._sessions: dict[str, SessionStateRecord] = {}
        self._idempotency_cache: dict[str, tuple[str, dict[str, Any]]] = {}  # key -> (payload_hash, response_dict)

    # --- Session Lifecycle ---

    def create_session(
        self,
        principal: AuthenticatedPrincipal,
        request: SessionCreateRequest,
    ) -> SessionCreateResponse:
        """Create a new session bound to repository and client."""
        request.validate()

        from .security import Permission

        # Role & Permission Check
        if not principal.has_permission(Permission.SESSION_MANAGE.value):
            self.audit_logger.log_access(
                principal=principal,
                action="create_session",
                resource=request.repository.repository_id,
                allowed=False,
                reason=f"Roles {principal.roles} lack required permission '{Permission.SESSION_MANAGE.value}'",
                repository_id=request.repository.repository_id,
            )
            raise AccessDeniedError(
                f"Principal '{principal.principal_id}' lacks permission '{Permission.SESSION_MANAGE.value}'.",
                details={"principal_id": principal.principal_id, "required_permission": Permission.SESSION_MANAGE.value},
            )

        # Authorization: Principal must be authorized for requested repository
        if not principal.can_access_repository(request.repository.repository_id):
            self.audit_logger.log_access(
                principal=principal,
                action="create_session",
                resource=request.repository.repository_id,
                allowed=False,
                reason=f"Repository '{request.repository.repository_id}' not authorized for principal",
                repository_id=request.repository.repository_id,
            )
            raise AccessDeniedError(
                f"Principal '{principal.principal_id}' is not authorized for repository '{request.repository.repository_id}'.",
                details={"principal_id": principal.principal_id, "repository_id": request.repository.repository_id},
            )

        self.audit_logger.log_access(
            principal=principal,
            action="create_session",
            resource=request.repository.repository_id,
            allowed=True,
            repository_id=request.repository.repository_id,
        )

        # Idempotency check
        if request.envelope.idempotency_key:
            cached = self._check_idempotency(request.envelope.idempotency_key, request)
            if cached is not None:
                return SessionCreateResponse(
                    envelope=ProtocolEnvelope(
                        protocol_version=request.envelope.protocol_version,
                        request_id=request.envelope.request_id,
                        timestamp=utc_now_iso(),
                        idempotency_key=request.envelope.idempotency_key,
                    ),
                    session_id=cached["session_id"],
                    status=cached["status"],
                    repository_id=cached["repository_id"],
                    created_at=cached["created_at"],
                    expires_at=cached["expires_at"],
                    metadata=cached.get("metadata", {}),
                )

        now = utc_now_iso()
        now_dt = datetime.now(timezone.utc)
        exp_dt = datetime.fromtimestamp(now_dt.timestamp() + request.ttl_seconds, tz=timezone.utc)
        expires_at = exp_dt.isoformat()

        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        clean_title = SecretRedactor.redact(request.title or f"Session for {request.repository.repository_id}")
        clean_metadata = SecretRedactor.redact_dict(request.metadata)

        record = SessionStateRecord(
            session_id=session_id,
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            repository_id=request.repository.repository_id,
            client=request.client,
            status=SessionStatus.ACTIVE,
            title=clean_title,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
            metadata=clean_metadata,
        )
        self._sessions[session_id] = record

        response = SessionCreateResponse(
            envelope=ProtocolEnvelope(
                protocol_version=request.envelope.protocol_version,
                request_id=request.envelope.request_id,
                timestamp=now,
                idempotency_key=request.envelope.idempotency_key,
            ),
            session_id=session_id,
            status=record.status.value,
            repository_id=record.repository_id,
            created_at=now,
            expires_at=expires_at,
            metadata=clean_metadata,
        )

        if request.envelope.idempotency_key:
            self._save_idempotency(request.envelope.idempotency_key, request, response.to_dict())

        return response

    def resume_session(
        self,
        principal: AuthenticatedPrincipal,
        request: SessionResumeRequest,
    ) -> SessionResumeResponse:
        """Inspect and resume an existing session."""
        request.validate()

        from .security import Permission

        if not principal.has_permission(Permission.SESSION_MANAGE.value):
            self.audit_logger.log_access(
                principal=principal,
                action="resume_session",
                resource=request.session_id,
                allowed=False,
                reason=f"Roles {principal.roles} lack required permission '{Permission.SESSION_MANAGE.value}'",
            )
            raise AccessDeniedError(
                f"Principal '{principal.principal_id}' lacks permission '{Permission.SESSION_MANAGE.value}'.",
                details={"principal_id": principal.principal_id, "required_permission": Permission.SESSION_MANAGE.value},
            )

        session = self._get_active_session(request.session_id, principal)

        now = utc_now_iso()
        session.status = SessionStatus.RESUMED
        session.updated_at = now

        self.audit_logger.log_access(
            principal=principal,
            action="resume_session",
            resource=request.session_id,
            allowed=True,
            repository_id=session.repository_id,
        )

        return SessionResumeResponse(
            envelope=ProtocolEnvelope(
                protocol_version=request.envelope.protocol_version,
                request_id=request.envelope.request_id,
                timestamp=now,
            ),
            session_id=session.session_id,
            status=session.status.value,
            repository_id=session.repository_id,
            started_at=session.created_at,
            expires_at=session.expires_at,
            query_count=session.query_count,
            title=session.title,
            summary=session.summary,
            metadata=session.metadata,
        )

    def close_session(
        self,
        principal: AuthenticatedPrincipal,
        request: SessionCloseRequest,
    ) -> SessionCloseResponse:
        """Close an existing session, preventing further mutations."""
        request.validate()

        from .security import Permission

        if not principal.has_permission(Permission.SESSION_MANAGE.value):
            self.audit_logger.log_access(
                principal=principal,
                action="close_session",
                resource=request.session_id,
                allowed=False,
                reason=f"Roles {principal.roles} lack required permission '{Permission.SESSION_MANAGE.value}'",
            )
            raise AccessDeniedError(
                f"Principal '{principal.principal_id}' lacks permission '{Permission.SESSION_MANAGE.value}'.",
                details={"principal_id": principal.principal_id, "required_permission": Permission.SESSION_MANAGE.value},
            )

        session = self._get_active_session(request.session_id, principal)

        now = utc_now_iso()
        clean_summary = SecretRedactor.redact(request.summary or session.summary)

        session.status = SessionStatus.CLOSED
        session.closed_at = now
        session.updated_at = now
        session.summary = clean_summary

        self.audit_logger.log_access(
            principal=principal,
            action="close_session",
            resource=request.session_id,
            allowed=True,
            repository_id=session.repository_id,
        )

        return SessionCloseResponse(
            envelope=ProtocolEnvelope(
                protocol_version=request.envelope.protocol_version,
                request_id=request.envelope.request_id,
                timestamp=now,
            ),
            session_id=session.session_id,
            status=SessionStatus.CLOSED.value,
            closed_at=now,
            summary=clean_summary,
        )

    # --- Context Query Operation ---

    def query_context(
        self,
        principal: AuthenticatedPrincipal,
        request: ContextQueryRequest,
    ) -> ContextQueryResponse:
        """Query orchestrated context for the active session and repository."""
        request.validate()

        from .security import Permission

        if not principal.has_permission(Permission.CONTEXT_QUERY.value):
            self.audit_logger.log_access(
                principal=principal,
                action="query_context",
                resource=request.session_id,
                allowed=False,
                reason=f"Roles {principal.roles} lack required permission '{Permission.CONTEXT_QUERY.value}'",
                repository_id=request.repository_id,
            )
            raise AccessDeniedError(
                f"Principal '{principal.principal_id}' lacks permission '{Permission.CONTEXT_QUERY.value}'.",
                details={"principal_id": principal.principal_id, "required_permission": Permission.CONTEXT_QUERY.value},
            )

        session = self._get_active_session(request.session_id, principal)

        # Repository Scope Invariant: Request repository must strictly match session repository
        if request.repository_id != session.repository_id:
            self.audit_logger.log_access(
                principal=principal,
                action="query_context",
                resource=request.session_id,
                allowed=False,
                reason=f"Repository mismatch: session={session.repository_id}, request={request.repository_id}",
                repository_id=request.repository_id,
            )
            raise RepositoryMismatchError(session.session_id, session.repository_id, request.repository_id)

        clean_query = SecretRedactor.redact(request.query)
        session.query_count += 1
        session.updated_at = utc_now_iso()

        self.audit_logger.log_access(
            principal=principal,
            action="query_context",
            resource=request.session_id,
            allowed=True,
            repository_id=session.repository_id,
        )

        # Always delegate to real AdaptiveContextOrchestrator
        if self.orchestrator is None:
            from .orchestrator import AdaptiveContextOrchestrator
            self.orchestrator = AdaptiveContextOrchestrator(
                session_memory_service=self.session_service,
            )

        orch_ctx = self.orchestrator.orchestrate(
            query=clean_query,
            user_id=principal.principal_id,
            repository_id=session.repository_id,
            max_tokens=request.max_tokens,
            optimization_mode=request.optimization_mode,
        )
        intent = (
            orch_ctx.intent.primary_intent.value
            if hasattr(orch_ctx.intent, "primary_intent")
            else str(orch_ctx.intent)
        )
        formatted = SecretRedactor.redact(
            getattr(orch_ctx, "context_text", getattr(orch_ctx, "formatted_context", ""))
        )
        used_tokens = getattr(orch_ctx, "estimated_tokens", getattr(orch_ctx, "tokens_used", 0))
        breakdown: dict[str, int] = {}
        for r in getattr(orch_ctx, "retrieved_results", []):
            cat = getattr(r, "type", "code")
            breakdown[cat] = breakdown.get(cat, 0) + 1
        prov = [
            {
                "source": getattr(r, "source", "unknown"),
                "repository": getattr(r, "repository", session.repository_id),
                "confidence": getattr(r, "confidence", 1.0),
            }
            for r in getattr(orch_ctx, "retrieved_results", [])
        ]
        meta = getattr(orch_ctx, "retrieval_metadata", {})
        latency = float(meta.get("latency_ms", 1.0) if isinstance(meta, dict) else 1.0)

        utilization = round((used_tokens / request.max_tokens) * 100.0, 2)

        return ContextQueryResponse(
            envelope=ProtocolEnvelope(
                protocol_version=request.envelope.protocol_version,
                request_id=request.envelope.request_id,
                timestamp=utc_now_iso(),
            ),
            session_id=session.session_id,
            repository_id=session.repository_id,
            intent=intent,
            formatted_context=formatted,
            token_budget=request.max_tokens,
            tokens_used=used_tokens,
            budget_utilization_pct=utilization,
            items_packed=sum(breakdown.values()),
            items_retained_by_category=breakdown,
            provenance=prov,
            latency_ms=latency,
        )

    # --- Store Operations ---

    def store_decision(
        self,
        principal: AuthenticatedPrincipal,
        request: StoreDecisionRequest,
    ) -> StoreDecisionResponse:
        """Store an accepted engineering decision."""
        request.validate()

        from .security import Permission

        if not principal.has_permission(Permission.DECISION_WRITE.value):
            self.audit_logger.log_access(
                principal=principal,
                action="store_decision",
                resource=request.session_id,
                allowed=False,
                reason=f"Roles {principal.roles} lack required permission '{Permission.DECISION_WRITE.value}'",
                repository_id=request.repository_id,
            )
            raise AccessDeniedError(
                f"Principal '{principal.principal_id}' lacks permission '{Permission.DECISION_WRITE.value}'.",
                details={"principal_id": principal.principal_id, "required_permission": Permission.DECISION_WRITE.value},
            )

        session = self._get_active_session(request.session_id, principal)

        if request.repository_id != session.repository_id:
            raise RepositoryMismatchError(session.session_id, session.repository_id, request.repository_id)

        # Team Scope Authorization: If scope is team, principal must belong to that team
        if request.scope == MemoryScope.TEAM.value:
            target_team = request.team_id or (principal.team_ids[0] if principal.team_ids else None)
            if not target_team or not principal.can_access_team(target_team):
                self.audit_logger.log_access(
                    principal=principal,
                    action="store_decision",
                    resource=request.session_id,
                    allowed=False,
                    reason=f"Principal '{principal.principal_id}' does not belong to team '{target_team}'.",
                    repository_id=request.repository_id,
                    team_id=target_team,
                )
                raise InvalidScopeError(
                    request.scope,
                    f"Principal '{principal.principal_id}' does not belong to team '{target_team}'.",
                )

        # Idempotency check
        if request.envelope.idempotency_key:
            cached = self._check_idempotency(request.envelope.idempotency_key, request)
            if cached is not None:
                return StoreDecisionResponse(
                    envelope=ProtocolEnvelope(
                        protocol_version=request.envelope.protocol_version,
                        request_id=request.envelope.request_id,
                        timestamp=utc_now_iso(),
                        idempotency_key=request.envelope.idempotency_key,
                    ),
                    decision_id=cached["decision_id"],
                    status=cached["status"],
                    scope=cached["scope"],
                )

        decision_id = f"dec_{uuid.uuid4().hex[:10]}"
        clean_title = SecretRedactor.redact(request.title)
        clean_rationale = SecretRedactor.redact(request.rationale)

        # If document store is present, persist the decision
        if self.doc_store and hasattr(self.doc_store, "save_decision"):
            from .models import EngineeringDecision, Memory, MemoryProvenance
            dec_obj = EngineeringDecision(
                id=decision_id,
                title=clean_title,
                rationale=clean_rationale,
                status=request.status,
                category=request.category,
                scope=request.scope,
                author_id=principal.principal_id,
                team_id=request.team_id,
                repository_id=request.repository_id,
                created_at=utc_now_iso(),
            )
            self.doc_store.save_decision(dec_obj)

            if hasattr(self.doc_store, "save_memory"):
                prov = MemoryProvenance(
                    source_type="engineering_decision",
                    source_id=decision_id,
                    file_paths=request.impact_areas,
                    symbol_names=request.affected_symbols,
                    confidence=0.95,
                    created_at=utc_now_iso(),
                )
                mem_card = Memory(
                    id=f"mem_{decision_id}",
                    title=f"Architecture Decision: {clean_title}",
                    content=f"Decision: {clean_title}\n\nRationale:\n{clean_rationale}",
                    category="architecture_decision",
                    scope=request.scope,
                    author_id=principal.principal_id,
                    team_id=request.team_id,
                    repository_id=request.repository_id,
                    session_id=session.session_id,
                    decision_id=decision_id,
                    impact_areas=request.impact_areas,
                    provenance=prov,
                    confidence=0.95,
                    created_at=utc_now_iso(),
                    updated_at=utc_now_iso(),
                )
                self.doc_store.save_memory(mem_card, changed_by=principal.principal_id, change_reason="StoreDecision")

        session.updated_at = utc_now_iso()

        self.audit_logger.log_access(
            principal=principal,
            action="store_decision",
            resource=request.session_id,
            allowed=True,
            repository_id=request.repository_id,
            team_id=request.team_id,
        )

        response = StoreDecisionResponse(
            envelope=ProtocolEnvelope(
                protocol_version=request.envelope.protocol_version,
                request_id=request.envelope.request_id,
                timestamp=utc_now_iso(),
                idempotency_key=request.envelope.idempotency_key,
            ),
            decision_id=decision_id,
            status="stored",
            scope=request.scope,
        )

        if request.envelope.idempotency_key:
            self._save_idempotency(request.envelope.idempotency_key, request, response.to_dict())

        return response

    def store_fact(
        self,
        principal: AuthenticatedPrincipal,
        request: StoreFactRequest,
    ) -> StoreFactResponse:
        """Store an engineering fact or solution pattern."""
        request.validate()

        from .security import Permission

        if not principal.has_permission(Permission.MEMORY_WRITE.value):
            self.audit_logger.log_access(
                principal=principal,
                action="store_fact",
                resource=request.session_id,
                allowed=False,
                reason=f"Roles {principal.roles} lack required permission '{Permission.MEMORY_WRITE.value}'",
                repository_id=request.repository_id,
            )
            raise AccessDeniedError(
                f"Principal '{principal.principal_id}' lacks permission '{Permission.MEMORY_WRITE.value}'.",
                details={"principal_id": principal.principal_id, "required_permission": Permission.MEMORY_WRITE.value},
            )

        session = self._get_active_session(request.session_id, principal)

        if request.repository_id != session.repository_id:
            raise RepositoryMismatchError(session.session_id, session.repository_id, request.repository_id)

        # Team Scope Authorization
        if request.scope == MemoryScope.TEAM.value:
            target_team = request.team_id or (principal.team_ids[0] if principal.team_ids else None)
            if not target_team or not principal.can_access_team(target_team):
                self.audit_logger.log_access(
                    principal=principal,
                    action="store_fact",
                    resource=request.session_id,
                    allowed=False,
                    reason=f"Principal '{principal.principal_id}' does not belong to team '{target_team}'.",
                    repository_id=request.repository_id,
                    team_id=target_team,
                )
                raise InvalidScopeError(
                    request.scope,
                    f"Principal '{principal.principal_id}' does not belong to team '{target_team}'.",
                )

        # Idempotency check
        if request.envelope.idempotency_key:
            cached = self._check_idempotency(request.envelope.idempotency_key, request)
            if cached is not None:
                return StoreFactResponse(
                    envelope=ProtocolEnvelope(
                        protocol_version=request.envelope.protocol_version,
                        request_id=request.envelope.request_id,
                        timestamp=utc_now_iso(),
                        idempotency_key=request.envelope.idempotency_key,
                    ),
                    memory_id=cached["memory_id"],
                    status=cached["status"],
                )

        memory_id = f"mem_{uuid.uuid4().hex[:10]}"
        clean_title = SecretRedactor.redact(request.title)
        clean_content = SecretRedactor.redact(request.content)
        clean_takeaways = [SecretRedactor.redact(t) for t in request.actionable_takeaways]

        if self.doc_store and hasattr(self.doc_store, "save_memory"):
            from .models import Memory, MemoryProvenance
            prov = MemoryProvenance(
                source_type="session_protocol",
                source_id=session.session_id,
                file_paths=request.impact_areas,
                confidence=0.95,
                created_at=utc_now_iso(),
            )
            mem = Memory(
                id=memory_id,
                title=clean_title,
                content=clean_content,
                category=request.category,
                scope=request.scope,
                author_id=principal.principal_id,
                team_id=request.team_id,
                repository_id=request.repository_id,
                session_id=session.session_id,
                impact_areas=request.impact_areas,
                actionable_takeaways=clean_takeaways,
                provenance=prov,
                created_at=utc_now_iso(),
                updated_at=utc_now_iso(),
            )
            self.doc_store.save_memory(mem, changed_by=principal.principal_id, change_reason="Protocol StoreFact")

        session.updated_at = utc_now_iso()

        self.audit_logger.log_access(
            principal=principal,
            action="store_fact",
            resource=request.session_id,
            allowed=True,
            repository_id=request.repository_id,
            team_id=request.team_id,
        )

        response = StoreFactResponse(
            envelope=ProtocolEnvelope(
                protocol_version=request.envelope.protocol_version,
                request_id=request.envelope.request_id,
                timestamp=utc_now_iso(),
                idempotency_key=request.envelope.idempotency_key,
            ),
            memory_id=memory_id,
            status="stored",
        )

        if request.envelope.idempotency_key:
            self._save_idempotency(request.envelope.idempotency_key, request, response.to_dict())

        return response

    def store_session_summary(
        self,
        principal: AuthenticatedPrincipal,
        request: StoreSessionSummaryRequest,
    ) -> StoreSessionSummaryResponse:
        """Distill an entire session interaction into reusable engineering memory."""
        request.validate()

        from .security import Permission

        if not principal.has_permission(Permission.MEMORY_WRITE.value):
            self.audit_logger.log_access(
                principal=principal,
                action="store_session_summary",
                resource=request.session_id,
                allowed=False,
                reason=f"Roles {principal.roles} lack required permission '{Permission.MEMORY_WRITE.value}'",
                repository_id=request.repository_id,
            )
            raise AccessDeniedError(
                f"Principal '{principal.principal_id}' lacks permission '{Permission.MEMORY_WRITE.value}'.",
                details={"principal_id": principal.principal_id, "required_permission": Permission.MEMORY_WRITE.value},
            )

        session = self._get_active_session(request.session_id, principal)

        if request.repository_id != session.repository_id:
            raise RepositoryMismatchError(session.session_id, session.repository_id, request.repository_id)

        clean_prompt = SecretRedactor.redact(request.prompt)
        clean_resp = SecretRedactor.redact(request.response_summary)
        clean_decisions = [SecretRedactor.redact(d) for d in request.engineering_decisions]

        mem_count = 1
        dec_count = len(clean_decisions)

        # If live session service is available, record and distill
        if self.session_service:
            res = self.session_service.record_session(
                session_id=session.session_id,
                user_id=principal.principal_id,
                repository_id=session.repository_id,
                prompt=clean_prompt,
                response_summary=clean_resp,
                affected_files=request.affected_files,
                affected_symbols=request.affected_symbols,
                engineering_decisions=clean_decisions,
                visibility=request.visibility,
                team_id=request.team_id,
                store_raw_transcript=request.store_raw_transcript,
                raw_messages=request.raw_messages,
            )
            mem_count = len(res.distilled_memories)
            dec_count = len(res.decisions)

        session.summary = clean_resp
        session.updated_at = utc_now_iso()

        self.audit_logger.log_access(
            principal=principal,
            action="store_session_summary",
            resource=session.session_id,
            allowed=True,
            repository_id=session.repository_id,
        )

        return StoreSessionSummaryResponse(
            envelope=ProtocolEnvelope(
                protocol_version=request.envelope.protocol_version,
                request_id=request.envelope.request_id,
                timestamp=utc_now_iso(),
            ),
            session_id=session.session_id,
            distilled_memories_count=mem_count,
            distilled_decisions_count=dec_count,
            status="recorded",
        )

    # --- Scoped Memory Retrieval Operation ---

    def scoped_retrieval(
        self,
        principal: AuthenticatedPrincipal,
        request: ScopedRetrievalRequest,
    ) -> ScopedRetrievalResponse:
        """Query stored memories enforcing strict repository and scope isolation."""
        request.validate()

        from .security import Permission

        if not principal.has_permission(Permission.MEMORY_READ.value):
            self.audit_logger.log_access(
                principal=principal,
                action="scoped_retrieval",
                resource=request.repository_id,
                allowed=False,
                reason=f"Roles {principal.roles} lack required permission '{Permission.MEMORY_READ.value}'",
                repository_id=request.repository_id,
            )
            raise AccessDeniedError(
                f"Principal '{principal.principal_id}' lacks permission '{Permission.MEMORY_READ.value}'.",
                details={"principal_id": principal.principal_id, "required_permission": Permission.MEMORY_READ.value},
            )

        if not principal.can_access_repository(request.repository_id):
            self.audit_logger.log_access(
                principal=principal,
                action="scoped_retrieval",
                resource=request.repository_id,
                allowed=False,
                reason=f"Principal '{principal.principal_id}' is not authorized for repository '{request.repository_id}'.",
                repository_id=request.repository_id,
            )
            raise AccessDeniedError(
                f"Principal '{principal.principal_id}' is not authorized for repository '{request.repository_id}'.",
                details={"principal_id": principal.principal_id, "repository_id": request.repository_id},
            )

        clean_query = SecretRedactor.redact(request.query)

        memories: list[dict[str, Any]] = []

        if self.doc_store and hasattr(self.doc_store, "query_memories"):
            raw_mems = self.doc_store.query_memories(
                user_id=principal.principal_id,
                team_id=principal.team_ids[0] if principal.team_ids else None,
                user_team_ids=principal.team_ids,
                repository_id=request.repository_id,
                query=clean_query,
                limit=request.limit,
            )
            for m in raw_mems:
                if m.scope in request.allowed_scopes:
                    if request.category_filter and m.category not in request.category_filter:
                        continue
                    memories.append(SecretRedactor.redact_dict(m.to_dict()))

        self.audit_logger.log_access(
            principal=principal,
            action="scoped_retrieval",
            resource=request.repository_id,
            allowed=True,
            repository_id=request.repository_id,
        )

        return ScopedRetrievalResponse(
            envelope=ProtocolEnvelope(
                protocol_version=request.envelope.protocol_version,
                request_id=request.envelope.request_id,
                timestamp=utc_now_iso(),
            ),
            repository_id=request.repository_id,
            query=clean_query,
            total_found=len(memories),
            memories=memories,
            scopes_included=list(request.allowed_scopes),
        )

    # --- Internal Helpers ---

    def _get_active_session(
        self,
        session_id: str,
        principal: AuthenticatedPrincipal,
    ) -> SessionStateRecord:
        """Retrieve and validate an active session belonging to principal."""
        session = self._sessions.get(session_id)
        if not session:
            raise SessionNotFoundError(session_id)

        # Cross-Tenant & Principal isolation: session must match principal or tenant
        if session.principal_id != principal.principal_id and session.tenant_id != principal.tenant_id:
            self.audit_logger.log_access(
                principal=principal,
                action="access_session",
                resource=session_id,
                allowed=False,
                reason=f"Principal '{principal.principal_id}' does not own session '{session_id}'.",
                repository_id=session.repository_id,
            )
            raise AccessDeniedError(
                f"Principal '{principal.principal_id}' does not own session '{session_id}'.",
                details={"principal_id": principal.principal_id, "session_id": session_id},
            )

        # Expiration check
        if session.is_expired():
            session.status = SessionStatus.EXPIRED
            raise SessionExpiredError(session_id, session.expires_at)

        # Closed check
        if session.status == SessionStatus.CLOSED:
            raise SessionClosedError(session_id, session.closed_at)

        return session

    def _compute_payload_hash(self, obj: Any) -> str:
        """Compute stable SHA-256 hash of a request payload."""
        try:
            if hasattr(obj, "__dict__"):
                d = asdict(obj)
            elif isinstance(obj, dict):
                d = dict(obj)
            else:
                d = {"value": str(obj)}

            # Exclude transient envelope fields from hash
            if "envelope" in d and isinstance(d["envelope"], dict):
                d_env = dict(d["envelope"])
                d_env.pop("request_id", None)
                d_env.pop("timestamp", None)
                d["envelope"] = d_env

            serialized = json.dumps(d, sort_keys=True)
            return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        except Exception:
            return hashlib.sha256(str(obj).encode("utf-8")).hexdigest()

    def _check_idempotency(self, idempotency_key: str, request: Any) -> dict[str, Any] | None:
        """Verify idempotency key, returning previous response or raising conflict."""
        if idempotency_key not in self._idempotency_cache:
            return None

        cached_hash, cached_response = self._idempotency_cache[idempotency_key]
        current_hash = self._compute_payload_hash(request)

        if cached_hash != current_hash:
            raise IdempotencyConflictError(idempotency_key)

        return cached_response

    def _save_idempotency(self, idempotency_key: str, request: Any, response_dict: dict[str, Any]) -> None:
        """Record idempotency cache entry."""
        h = self._compute_payload_hash(request)
        self._idempotency_cache[idempotency_key] = (h, response_dict)
