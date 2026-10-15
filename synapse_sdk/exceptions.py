"""Structured Exceptions for Synapse Python SDK (Phase 11).

Provides an expressive exception hierarchy mapping API status codes
and protocol error details into typed Python exceptions.
"""

from __future__ import annotations

from typing import Any


class SynapseError(Exception):
    """Base exception for all errors raised by the Synapse SDK."""
    pass


class SynapseConnectionError(SynapseError):
    """Raised when the SDK cannot establish a network connection to the Synapse server."""
    pass


class SynapseTimeoutError(SynapseError):
    """Raised when an API request times out."""
    pass


class SynapseApiError(SynapseError):
    """Base exception for HTTP errors returned by the Synapse API."""

    def __init__(
        self,
        message: str,
        status_code: int,
        error_code: str = "INTERNAL_ERROR",
        request_id: str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        raw_response: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"[{status_code} - {error_code}] {message} (request_id: {request_id or 'unknown'})")
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        self.request_id = request_id
        self.retryable = retryable
        self.details = details or {}
        self.raw_response = raw_response or {}


class ValidationError(SynapseApiError):
    """Raised on 400 Bad Request or 422 Unprocessable Content (e.g. MALFORMED_INPUT)."""
    pass


class AuthenticationError(SynapseApiError):
    """Raised on 401 Unauthorized (e.g. missing/invalid Bearer token or API key)."""
    pass


class AccessDeniedError(SynapseApiError):
    """Raised on 403 Forbidden (e.g. ACCESS_DENIED or insufficient role permissions)."""
    pass


class InvalidScopeError(SynapseApiError):
    """Raised on 403 Forbidden when requesting unauthorized memory scopes (e.g. cross-team)."""
    pass


class NotFoundError(SynapseApiError):
    """Raised on 404 Not Found (e.g. SESSION_NOT_FOUND)."""
    pass


class ConflictError(SynapseApiError):
    """Raised on 409 Conflict (e.g. SESSION_CLOSED, REPOSITORY_MISMATCH)."""
    pass


class IdempotencyConflictError(ConflictError):
    """Raised on 409 Conflict when an idempotency key is reused with a different payload."""
    pass


class PayloadTooLargeError(SynapseApiError):
    """Raised on 413 Payload Too Large when request body exceeds configured limit."""
    pass


class RateLimitError(SynapseApiError):
    """Raised on 429 Too Many Requests."""
    pass


class ServerError(SynapseApiError):
    """Raised on 500/502/503/504 internal server errors."""
    pass


def exception_from_response(
    status_code: int,
    data: dict[str, Any] | None,
    raw_text: str = "",
) -> SynapseApiError:
    """Parse structured ProtocolErrorResponse and return the corresponding typed exception."""
    data = data or {}
    envelope = data.get("envelope", {}) if isinstance(data, dict) else {}
    error = data.get("error", {}) if isinstance(data, dict) else {}

    request_id = envelope.get("request_id")
    error_code = error.get("code", "UNKNOWN_ERROR")
    message = error.get("message") or raw_text or f"HTTP error {status_code}"
    retryable = error.get("retryable", False)
    details = error.get("details", {})

    kwargs = {
        "message": message,
        "status_code": status_code,
        "error_code": error_code,
        "request_id": request_id,
        "retryable": retryable,
        "details": details,
        "raw_response": data,
    }

    if status_code == 400:
        return ValidationError(**kwargs)
    elif status_code == 401:
        return AuthenticationError(**kwargs)
    elif status_code == 403:
        if error_code == "INVALID_SCOPE":
            return InvalidScopeError(**kwargs)
        return AccessDeniedError(**kwargs)
    elif status_code == 404:
        return NotFoundError(**kwargs)
    elif status_code == 409:
        if error_code == "IDEMPOTENCY_CONFLICT":
            return IdempotencyConflictError(**kwargs)
        return ConflictError(**kwargs)
    elif status_code == 413:
        return PayloadTooLargeError(**kwargs)
    elif status_code == 422:
        return ValidationError(**kwargs)
    elif status_code == 429:
        return RateLimitError(**kwargs)
    elif status_code in (500, 502, 503, 504):
        return ServerError(**kwargs)

    return SynapseApiError(**kwargs)
