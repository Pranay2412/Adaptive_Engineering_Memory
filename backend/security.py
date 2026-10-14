"""Authentication, Authorization, RBAC, and Security Auditing for Synapse (Phase 9).

Provides:
- Role & Permission models (RBAC matrix: admin, maintainer, developer, viewer, system_service)
- Immutable Security Audit Logger (tracking access, policy violations, cross-repo/tenant leakage)
- Secure API Key Management (SHA-256 hash storage, prefixing, expiry, revocation)
- Cryptographic Token Service (HMAC-SHA256 signed bearer tokens with claims verification)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import uuid
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Default development secret key; in production this must be overridden via environment variable
DEFAULT_DEV_SECRET = "synapse-insecure-dev-secret-key-32chars-min-length-12345"


def utc_now_iso() -> str:
    """Return ISO 8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Roles & Permissions Enums & RBAC Matrix
# ---------------------------------------------------------------------------

class Role(str, Enum):
    """Recognized principal roles in Synapse."""
    ADMIN = "admin"
    MAINTAINER = "maintainer"
    DEVELOPER = "developer"
    VIEWER = "viewer"
    SYSTEM_SERVICE = "system_service"


class Permission(str, Enum):
    """Discrete system permissions for engineering memory operations."""
    MEMORY_READ = "memory:read"
    MEMORY_WRITE = "memory:write"
    CONTEXT_QUERY = "context:query"
    SESSION_MANAGE = "session:manage"
    DECISION_WRITE = "decision:write"
    GIT_INGEST = "git:ingest"
    AUDIT_READ = "audit:read"
    ADMIN_ALL = "*"


ROLE_PERMISSIONS: dict[str, set[str]] = {
    Role.ADMIN.value: {
        Permission.ADMIN_ALL.value,
    },
    Role.MAINTAINER.value: {
        Permission.MEMORY_READ.value,
        Permission.MEMORY_WRITE.value,
        Permission.CONTEXT_QUERY.value,
        Permission.SESSION_MANAGE.value,
        Permission.DECISION_WRITE.value,
        Permission.GIT_INGEST.value,
        Permission.AUDIT_READ.value,
    },
    Role.DEVELOPER.value: {
        Permission.MEMORY_READ.value,
        Permission.MEMORY_WRITE.value,
        Permission.CONTEXT_QUERY.value,
        Permission.SESSION_MANAGE.value,
        Permission.DECISION_WRITE.value,
    },
    Role.VIEWER.value: {
        Permission.MEMORY_READ.value,
        Permission.CONTEXT_QUERY.value,
    },
    Role.SYSTEM_SERVICE.value: {
        Permission.MEMORY_READ.value,
        Permission.MEMORY_WRITE.value,
        Permission.GIT_INGEST.value,
        Permission.SESSION_MANAGE.value,
    },
    # Common engineering role aliases
    "senior_developer": {
        Permission.MEMORY_READ.value,
        Permission.MEMORY_WRITE.value,
        Permission.CONTEXT_QUERY.value,
        Permission.SESSION_MANAGE.value,
        Permission.DECISION_WRITE.value,
    },
    "guest": {
        Permission.MEMORY_READ.value,
        Permission.CONTEXT_QUERY.value,
    },
}


def compute_effective_permissions(roles: Iterable[str]) -> set[str]:
    """Compute union of permissions granted to a set of roles with alias resolution."""
    perms: set[str] = set()
    for r in roles:
        if r in ROLE_PERMISSIONS:
            perms.update(ROLE_PERMISSIONS[r])
        else:
            r_lower = r.lower()
            if "admin" in r_lower:
                perms.update(ROLE_PERMISSIONS[Role.ADMIN.value])
            elif "maintain" in r_lower:
                perms.update(ROLE_PERMISSIONS[Role.MAINTAINER.value])
            elif "dev" in r_lower or "engineer" in r_lower:
                perms.update(ROLE_PERMISSIONS[Role.DEVELOPER.value])
            elif "view" in r_lower or "guest" in r_lower or "read" in r_lower:
                perms.update(ROLE_PERMISSIONS[Role.VIEWER.value])
    return perms


# ---------------------------------------------------------------------------
# Security Audit Event Models
# ---------------------------------------------------------------------------

@dataclass
class SecurityAuditEvent:
    """Immutable audit event recording security-relevant actions."""

    id: str
    event_type: str
    principal_id: str
    tenant_id: str
    repository_id: str | None = None
    team_id: str | None = None
    action: str = ""
    resource: str = ""
    status: str = "allow"  # "allow", "deny", "revoked", "expired"
    reason: str | None = None
    ip_address: str | None = None
    timestamp: str = dc_field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_type": self.event_type,
            "principal_id": self.principal_id,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "team_id": self.team_id,
            "action": self.action,
            "resource": self.resource,
            "status": self.status,
            "reason": self.reason,
            "ip_address": self.ip_address,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SecurityAuditEvent:
        return cls(
            id=data["id"],
            event_type=data["event_type"],
            principal_id=data["principal_id"],
            tenant_id=data["tenant_id"],
            repository_id=data.get("repository_id"),
            team_id=data.get("team_id"),
            action=data.get("action", ""),
            resource=data.get("resource", ""),
            status=data.get("status", "allow"),
            reason=data.get("reason"),
            ip_address=data.get("ip_address"),
            timestamp=data.get("timestamp", utc_now_iso()),
            metadata=data.get("metadata", {}),
        )


class SecurityAuditLogger:
    """Persists and monitors security audit events without leaking private data."""

    def __init__(self, doc_store: Any | None = None) -> None:
        self.doc_store = doc_store
        self._in_memory_events: list[SecurityAuditEvent] = []

    def log(self, event: SecurityAuditEvent) -> SecurityAuditEvent:
        """Persist a security audit trail event."""
        # Sanitize metadata to prevent secret leakage
        from .session_protocol import SecretRedactor

        if event.metadata:
            event.metadata = SecretRedactor.redact_dict(event.metadata)

        if self.doc_store and hasattr(self.doc_store, "log_audit_event"):
            try:
                self.doc_store.log_audit_event(event)
            except Exception as e:
                logger.warning("Failed to persist security audit event to document store: %s", e)
                self._in_memory_events.append(event)
        else:
            self._in_memory_events.append(event)

        if event.status == "deny":
            logger.warning(
                "SECURITY AUDIT DENY: principal=%s action=%s resource=%s reason=%s repo=%s tenant=%s",
                event.principal_id,
                event.action,
                event.resource,
                event.reason,
                event.repository_id,
                event.tenant_id,
            )
        return event

    def log_access(
        self,
        principal: Any,
        action: str,
        resource: str,
        allowed: bool = True,
        reason: str | None = None,
        repository_id: str | None = None,
        team_id: str | None = None,
        ip_address: str | None = None,
        event_type: str = "authorization_check",
        metadata: dict[str, Any] | None = None,
    ) -> SecurityAuditEvent:
        """Helper to create and log an authorization audit entry."""
        pid = getattr(principal, "principal_id", str(principal))
        tid = getattr(principal, "tenant_id", "unknown-tenant")
        event = SecurityAuditEvent(
            id=f"audit_{uuid.uuid4().hex[:12]}",
            event_type=event_type,
            principal_id=pid,
            tenant_id=tid,
            repository_id=repository_id,
            team_id=team_id,
            action=action,
            resource=resource,
            status="allow" if allowed else "deny",
            reason=reason,
            ip_address=ip_address,
            timestamp=utc_now_iso(),
            metadata=metadata or {},
        )
        return self.log(event)

    def list_events(
        self,
        tenant_id: str | None = None,
        principal_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
    ) -> list[SecurityAuditEvent]:
        """Query security audit events."""
        if self.doc_store and hasattr(self.doc_store, "list_audit_events"):
            return self.doc_store.list_audit_events(
                tenant_id=tenant_id,
                principal_id=principal_id,
                event_type=event_type,
                limit=limit,
            )

        events = self._in_memory_events
        if tenant_id:
            events = [e for e in events if e.tenant_id == tenant_id]
        if principal_id:
            events = [e for e in events if e.principal_id == principal_id]
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        return events[:limit]


# ---------------------------------------------------------------------------
# API Key Record & Service
# ---------------------------------------------------------------------------

@dataclass
class ApiKeyRecord:
    """Stored representation of an API key (hash only, never plaintext secret)."""

    key_id: str
    name: str
    key_hash: str
    prefix: str
    tenant_id: str
    principal_id: str
    roles: list[str] = dc_field(default_factory=list)
    allowed_repositories: list[str] = dc_field(default_factory=list)
    allowed_teams: list[str] = dc_field(default_factory=list)
    expires_at: str | None = None
    revoked: bool = False
    created_at: str = dc_field(default_factory=utc_now_iso)

    def is_expired(self) -> bool:
        """Check if API key expiration timestamp has passed."""
        if not self.expires_at:
            return False
        try:
            exp = datetime.fromisoformat(self.expires_at)
            return datetime.now(timezone.utc) > exp
        except Exception:
            return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key_id": self.key_id,
            "name": self.name,
            "prefix": self.prefix,
            "tenant_id": self.tenant_id,
            "principal_id": self.principal_id,
            "roles": list(self.roles),
            "allowed_repositories": list(self.allowed_repositories),
            "allowed_teams": list(self.allowed_teams),
            "expires_at": self.expires_at,
            "revoked": self.revoked,
            "created_at": self.created_at,
        }


class ApiKeyService:
    """Manages high-entropy API keys with secure SHA-256 hashing and lifecycle revocation."""

    KEY_PREFIX = "syn_"

    def __init__(self, doc_store: Any | None = None) -> None:
        self.doc_store = doc_store
        self._in_memory_keys: dict[str, ApiKeyRecord] = {}  # key_hash -> ApiKeyRecord

    @staticmethod
    def hash_key(raw_key: str) -> str:
        """Compute cryptographic SHA-256 digest of plaintext key."""
        return hashlib.sha256(raw_key.strip().encode("utf-8")).hexdigest()

    def create_api_key(
        self,
        name: str,
        tenant_id: str,
        principal_id: str,
        roles: list[str] | None = None,
        allowed_repositories: list[str] | None = None,
        allowed_teams: list[str] | None = None,
        expires_at: str | None = None,
    ) -> tuple[str, ApiKeyRecord]:
        """Generate high-entropy API key, hash and persist it.

        Returns (plaintext_secret_key, ApiKeyRecord).
        The plaintext key is ONLY returned once and never stored anywhere!
        """
        key_id = f"key_{uuid.uuid4().hex[:12]}"
        random_secret = secrets.token_hex(24)
        prefix = f"{self.KEY_PREFIX}{random_secret[:6]}"
        full_key = f"{prefix}_{random_secret[6:]}"
        key_hash = self.hash_key(full_key)

        record = ApiKeyRecord(
            key_id=key_id,
            name=name,
            key_hash=key_hash,
            prefix=prefix,
            tenant_id=tenant_id,
            principal_id=principal_id,
            roles=roles or [Role.DEVELOPER.value],
            allowed_repositories=allowed_repositories or ["*"],
            allowed_teams=allowed_teams or [],
            expires_at=expires_at,
            revoked=False,
            created_at=utc_now_iso(),
        )

        if self.doc_store and hasattr(self.doc_store, "save_api_key"):
            self.doc_store.save_api_key(record)
        else:
            self._in_memory_keys[key_hash] = record

        return full_key, record

    def verify_api_key(self, raw_key: str) -> ApiKeyRecord | None:
        """Verify API key from plaintext string, returning active record or None."""
        if not raw_key or not isinstance(raw_key, str) or not raw_key.startswith(self.KEY_PREFIX):
            return None

        key_hash = self.hash_key(raw_key)

        record: ApiKeyRecord | None = None
        if self.doc_store and hasattr(self.doc_store, "get_api_key_by_hash"):
            record = self.doc_store.get_api_key_by_hash(key_hash)
        else:
            record = self._in_memory_keys.get(key_hash)

        if not record:
            return None

        if record.revoked:
            logger.info("Rejected revoked API key '%s'", record.key_id)
            return None

        if record.is_expired():
            logger.info("Rejected expired API key '%s'", record.key_id)
            return None

        return record

    def revoke_api_key(self, key_id: str) -> bool:
        """Revoke an API key by key_id."""
        if self.doc_store and hasattr(self.doc_store, "revoke_api_key"):
            return self.doc_store.revoke_api_key(key_id)

        for record in self._in_memory_keys.values():
            if record.key_id == key_id:
                record.revoked = True
                return True
        return False


# ---------------------------------------------------------------------------
# Cryptographic Token Service (HMAC-SHA256 Signed Bearer Tokens)
# ---------------------------------------------------------------------------

class TokenService:
    """Issues and cryptographically validates HMAC-SHA256 bearer tokens."""

    def __init__(self, secret_key: str | None = None) -> None:
        self.secret_key = (
            secret_key
            or os.environ.get("SYNAPSE_SECRET_KEY")
            or DEFAULT_DEV_SECRET
        ).encode("utf-8")

    @staticmethod
    def _b64encode(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")

    @staticmethod
    def _b64decode(data: str) -> bytes:
        pad = 4 - (len(data) % 4)
        if pad and pad < 4:
            data += "=" * pad
        return base64.urlsafe_b64decode(data.encode("utf-8"))

    def create_token(
        self,
        principal: Any,
        ttl_seconds: int = 3600,
    ) -> str:
        """Generate HMAC-SHA256 signed bearer token with claims."""
        now = datetime.now(timezone.utc)
        exp_ts = int(now.timestamp()) + ttl_seconds
        iat_ts = int(now.timestamp())

        header = {"alg": "HS256", "typ": "JWT"}
        payload = {
            "sub": getattr(principal, "principal_id", str(principal)),
            "tid": getattr(principal, "tenant_id", "default"),
            "roles": list(getattr(principal, "roles", ["developer"])),
            "repos": list(getattr(principal, "allowed_repositories", ["*"])),
            "teams": list(getattr(principal, "team_ids", [])),
            "iat": iat_ts,
            "exp": exp_ts,
        }

        h_b64 = self._b64encode(json.dumps(header, sort_keys=True).encode("utf-8"))
        p_b64 = self._b64encode(json.dumps(payload, sort_keys=True).encode("utf-8"))
        signing_input = f"{h_b64}.{p_b64}".encode("utf-8")

        sig = hmac.new(self.secret_key, signing_input, hashlib.sha256).digest()
        sig_b64 = self._b64encode(sig)

        return f"{h_b64}.{p_b64}.{sig_b64}"

    def verify_token(self, token_str: str) -> Any:
        """Verify token signature and expiration, returning AuthenticatedPrincipal.

        Raises ValueError if invalid, tampered with, or expired.
        """
        from .session_protocol import AuthenticatedPrincipal

        if not token_str or token_str.count(".") != 2:
            raise ValueError("Malformed token format: expected 3 dot-separated segments.")

        h_b64, p_b64, sig_b64 = token_str.split(".")

        signing_input = f"{h_b64}.{p_b64}".encode("utf-8")
        expected_sig = hmac.new(self.secret_key, signing_input, hashlib.sha256).digest()
        provided_sig = self._b64decode(sig_b64)

        if not hmac.compare_digest(expected_sig, provided_sig):
            raise ValueError("Invalid cryptographic signature.")

        try:
            payload = json.loads(self._b64decode(p_b64).decode("utf-8"))
        except Exception as e:
            raise ValueError(f"Invalid token payload JSON: {e}")

        # Check expiration
        exp_ts = payload.get("exp")
        if exp_ts is not None:
            now_ts = int(datetime.now(timezone.utc).timestamp())
            if now_ts > exp_ts:
                raise ValueError("Token has expired.")

        return AuthenticatedPrincipal(
            principal_id=payload.get("sub", "unknown"),
            tenant_id=payload.get("tid", "default"),
            roles=payload.get("roles", ["developer"]),
            allowed_repositories=payload.get("repos", ["*"]),
            team_ids=payload.get("teams", []),
            is_authenticated=True,
        )


def principal_from_api_key(record: ApiKeyRecord) -> Any:
    """Construct an AuthenticatedPrincipal from a verified ApiKeyRecord."""
    from .session_protocol import AuthenticatedPrincipal

    return AuthenticatedPrincipal(
        principal_id=record.principal_id,
        tenant_id=record.tenant_id,
        roles=record.roles,
        allowed_repositories=record.allowed_repositories,
        team_ids=record.allowed_teams,
        is_authenticated=True,
    )
