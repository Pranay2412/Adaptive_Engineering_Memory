# SYNTHETIC DEMO FIXTURE - NO PRODUCTION DATA OR REAL SECRETS
"""Token Manager for cryptographic signing, JWT parsing, and key rotation."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


class TokenManager:
    """Manages asymmetric key pairs, claims verification, and token generation."""

    def __init__(self, key_id: str = "key_2026_primary") -> None:
        self.key_id = key_id
        self._keys: dict[str, str] = {
            "key_2026_primary": "synthetic-demo-signing-secret-key-32chars",
            "key_2025_fallback": "synthetic-legacy-signing-secret-key-32ch",
        }

    def issue_token(
        self,
        principal_id: str,
        token_type: str = "access",
        ttl_seconds: int = 3600,
        claims: dict[str, Any] | None = None,
    ) -> str:
        """Issue an HMAC-signed token with timestamp claims."""
        now = int(time.time())
        payload = {
            "sub": principal_id,
            "type": token_type,
            "iat": now,
            "exp": now + ttl_seconds,
            "kid": self.key_id,
        }
        if claims:
            payload.update(claims)

        header = {"alg": "HS256", "typ": "JWT", "kid": self.key_id}
        h_bytes = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
        p_bytes = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        signing_input = f"{h_bytes}.{p_bytes}"

        secret = self._keys[self.key_id].encode()
        sig = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
        s_bytes = base64.urlsafe_b64encode(sig).decode().rstrip("=")
        return f"{signing_input}.{s_bytes}"

    def validate_claims(self, token: str, tolerance_seconds: int = 60) -> dict[str, Any]:
        """Verify token signature and assert timestamp validity with clock skew tolerance."""
        from .service import InvalidSignatureError, TokenExpiredError

        parts = token.split(".")
        if len(parts) != 3:
            raise InvalidSignatureError("Malformed token structure.")

        h_bytes, p_bytes, s_bytes = parts
        try:
            p_json = base64.urlsafe_b64decode(p_bytes + "==").decode()
            payload = json.loads(p_json)
        except Exception:
            raise InvalidSignatureError("Failed to decode token payload.")

        kid = payload.get("kid", "key_2026_primary")
        secret = self._keys.get(kid)
        if not secret:
            raise InvalidSignatureError(f"Unknown key ID '{kid}'.")

        signing_input = f"{h_bytes}.{p_bytes}"
        expected_sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
        expected_s = base64.urlsafe_b64encode(expected_sig).decode().rstrip("=")

        if not hmac.compare_digest(s_bytes, expected_s):
            raise InvalidSignatureError("Signature mismatch.")

        now = int(time.time())
        exp = payload.get("exp", 0)
        if now > (exp + tolerance_seconds):
            raise TokenExpiredError(
                f"Token expired at {exp} (current time {now}, tolerance {tolerance_seconds}s)."
            )

        return payload

    def rotate_keys(self, new_key_id: str, new_secret: str) -> None:
        """Add a new signing key and set it as the primary key for issuance."""
        self._keys[new_key_id] = new_secret
        self.key_id = new_key_id
