"""Synapse: Adaptive Engineering Memory Platform - Python SDK (Phase 11).

Official, typed Python SDK for integrating coding assistants, IDE extensions,
and development tools with the Synapse memory and context platform.

Usage:
    from synapse_sdk import SynapseClient, ClientConfig

    client = SynapseClient(base_url="http://localhost:8000", api_key="synapse_sk_...")
    session = client.create_session(repository_id="repo-core", title="Refactoring Auth")
    context = client.query_context(session.session_id, "repo-core", "How is token validation implemented?")
"""

from __future__ import annotations

from .client import SynapseClient
from .config import ClientConfig
from .exceptions import (
    AccessDeniedError,
    AuthenticationError,
    ConflictError,
    IdempotencyConflictError,
    InvalidScopeError,
    NotFoundError,
    PayloadTooLargeError,
    RateLimitError,
    ServerError,
    SynapseApiError,
    SynapseConnectionError,
    SynapseError,
    SynapseTimeoutError,
    ValidationError,
)
from .models import (
    ContextItem,
    ContextResult,
    DecisionRecord,
    DistillationResult,
    FactRecord,
    HealthStatus,
    ReadinessStatus,
    ScopedMemoryItem,
    ScopedQueryResult,
    SessionInfo,
)

__version__ = "1.0.0"

__all__ = [
    # Client & Configuration
    "SynapseClient",
    "ClientConfig",
    "__version__",
    # Domain Models
    "HealthStatus",
    "ReadinessStatus",
    "SessionInfo",
    "ContextItem",
    "ContextResult",
    "DecisionRecord",
    "FactRecord",
    "DistillationResult",
    "ScopedMemoryItem",
    "ScopedQueryResult",
    # Exceptions
    "SynapseError",
    "SynapseConnectionError",
    "SynapseTimeoutError",
    "SynapseApiError",
    "ValidationError",
    "AuthenticationError",
    "AccessDeniedError",
    "InvalidScopeError",
    "NotFoundError",
    "ConflictError",
    "IdempotencyConflictError",
    "PayloadTooLargeError",
    "RateLimitError",
    "ServerError",
]
