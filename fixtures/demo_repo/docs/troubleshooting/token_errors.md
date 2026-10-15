<!-- SYNTHETIC DEMO FIXTURE - NO PRODUCTION DATA OR REAL SECRETS -->
# Troubleshooting Guide: Handling Expired Tokens and Signature Failures

This operational runbook documents common authentication errors, diagnostics, and resolution steps for engineering teams.

## Handling TokenExpiredError

When an API client receives an HTTP 401 response accompanied by `TokenExpiredError`:
1. Check client clock synchronization using NTP. Distributed microservices can experience clock drift.
2. Verify if the token expired naturally after its 3600-second TTL.
3. Apply the 60-second grace period configured in `AuthService.verify_token(token, tolerance_seconds=60)`.
4. If the token is beyond the grace period, the client MUST exchange its refresh token via `AuthService.refresh_session(refresh_token)`.

## Debugging InvalidSignatureError

An `InvalidSignatureError` occurs during signature verification under the following conditions:
- **Key Rotation in Progress**: If `TokenManager.rotate_keys()` was recently executed, verify that the fallback key dictionary still contains the previous `kid`.
- **Payload Tampering**: The token header or claims payload was altered in transit.
- **Revoked Token**: The token was explicitly invalidated via `AuthService.logout()`. Check the revoked token blacklist.
