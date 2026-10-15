<!-- SYNTHETIC DEMO FIXTURE - NO PRODUCTION DATA OR REAL SECRETS -->
# Architecture Overview: Authentication and Token Lifecycle

This document specifies the architecture, data flow, and security guarantees of the authentication subsystem in Synapse Demo Service.

## Authentication Architecture and Components

The authentication subsystem is partitioned into modular boundaries:
1. `AuthService`: Serves as the primary boundary facade. It coordinates credential verification, session issuance, and token revocation.
2. `TokenManager`: Handles low-level cryptographic token signing, JWT structure validation, and key rotation.
3. `SessionStore`: Maintains active session tokens and persistent tenant association.

```
[Client Application]
        │
        ▼ (Login / Token Request)
┌─────────────────────────────────┐
│          AuthService            │
└───────┬─────────────────┬───────┘
        │                 │
        ▼                 ▼
┌──────────────┐  ┌──────────────┐
│ TokenManager │  │ SessionStore │
└──────────────┘  └──────────────┘
```

## Security Specifications and Token Lifespan

All access tokens are cryptographically signed with SHA-256 HMAC (or RS256 in production environments) and have a strict lifespan of 3600 seconds (1 hour). Refresh tokens have an active lifespan of 86,400 seconds (24 hours).

Per architectural security guidelines:
- All endpoints verify `principal_id` and enforce multi-tenant isolation.
- Token signatures are verified against active key sets identified by the `kid` header.
- Revoked tokens are checked in memory or Redis cache before granting authorization.
