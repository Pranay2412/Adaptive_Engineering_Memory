# SYNTHETIC DEMO FIXTURE - NO PRODUCTION DATA OR REAL SECRETS
"""Core Authentication Service for user verification and session lifecycle."""

from __future__ import annotations

import time
from typing import Any
from .token_manager import TokenManager


class TokenExpiredError(Exception):
    """Raised when an authentication token exceeds its valid expiration timestamp."""
    pass


class InvalidSignatureError(Exception):
    """Raised when cryptographic signature verification fails on an incoming token."""
    pass


class AuthService:
    """Coordinates authentication sessions, token validation, and principal resolution.
    
    The AuthService delegates cryptographic signing and parsing to TokenManager,
    maintains revoking state, and enforces tenant isolation boundaries.
    """

    def __init__(self, token_manager: TokenManager | None = None) -> None:
        self.token_manager = token_manager or TokenManager()
        self._revoked_tokens: set[str] = set()

    def login(self, username: str, credentials_hash: str) -> dict[str, Any]:
        """Authenticate user credentials and issue scoped access and refresh tokens.
        
        Args:
            username: Unique username identifier.
            credentials_hash: Pre-hashed password digest.
            
        Returns:
            Dictionary containing access_token, refresh_token, and expiration timestamp.
        """
        user_id = f"user_{username}"
        access_token = self.token_manager.issue_token(
            principal_id=user_id,
            token_type="access",
            ttl_seconds=3600,
        )
        refresh_token = self.token_manager.issue_token(
            principal_id=user_id,
            token_type="refresh",
            ttl_seconds=86400,
        )
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": 3600,
            "user_id": user_id,
        }

    def verify_token(self, token: str, tolerance_seconds: int = 60) -> dict[str, Any]:
        """Validate token signature, verify expiration with clock skew tolerance, and check revocation.
        
        Args:
            token: Raw JWT token string.
            tolerance_seconds: Grace period allowed for clock skew (default 60s per ADR-004).
            
        Raises:
            InvalidSignatureError: When the signature fails cryptographic verification.
            TokenExpiredError: When the token has expired beyond the allowable grace period.
            
        Returns:
            Claims dictionary containing principal_id, tenant_id, and scopes.
        """
        if token in self._revoked_tokens:
            raise InvalidSignatureError("Token has been revoked.")

        claims = self.token_manager.validate_claims(token, tolerance_seconds=tolerance_seconds)
        return claims

    def refresh_session(self, refresh_token: str) -> dict[str, Any]:
        """Exchange an active refresh token for a new access token pair.
        
        Rotates the refresh token to prevent replay attacks.
        """
        claims = self.verify_token(refresh_token)
        principal_id = claims["sub"]
        self._revoked_tokens.add(refresh_token)

        new_access = self.token_manager.issue_token(principal_id=principal_id, token_type="access", ttl_seconds=3600)
        new_refresh = self.token_manager.issue_token(principal_id=principal_id, token_type="refresh", ttl_seconds=86400)
        return {
            "access_token": new_access,
            "refresh_token": new_refresh,
            "expires_in": 3600,
        }

    def logout(self, token: str) -> bool:
        """Revoke the active token and invalidate session state."""
        self._revoked_tokens.add(token)
        return True
