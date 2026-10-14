# Synapse Authentication, Authorization, RBAC & Security Audit Specification

**Document Version:** 1.0.0  
**Phase:** Stage C — Phase 9 (Authentication and authorization)  
**Status:** Implemented & Verified  
**Implementation Modules:** [`backend/security.py`](file:///C:/FYP/backend/security.py), [`backend/session_protocol.py`](file:///C:/FYP/backend/session_protocol.py), [`backend/api.py`](file:///C:/FYP/backend/api.py), [`backend/team_memory_store.py`](file:///C:/FYP/backend/team_memory_store.py)  
**Test Suite:** [`tests/test_auth_security.py`](file:///C:/FYP/tests/test_auth_security.py) (25/25 passed)

---

## 1. Executive Summary

Synapse provides an adaptive, multi-agent, cross-IDE engineering memory platform. Because engineering memory records contain proprietary source architecture, design trade-offs, system credentials, and organization knowledge, robust multi-tenant and repository-level isolation is foundational.

Phase 9 establishes the cryptographically grounded authentication and server-side authorization framework for Synapse. Prior to Phase 9, identity parameters were accepted from request headers or payloads in development mode. Phase 9 enforces:
1. **Server-Side Trust Boundaries:** Caller identity is asserted through cryptographically signed HMAC-SHA256 bearer tokens or high-entropy SHA-256 hashed API keys. Client-supplied request body IDs are never trusted as identity boundaries.
2. **Role-Based Access Control (RBAC):** Five standardized principal roles (`admin`, `maintainer`, `developer`, `viewer`, `system_service`) mapped to discrete, fine-grained operation permissions.
3. **Multi-Tenant and Repository Boundary Enforcement:** Strict scoping guarantees that sessions, context queries, decisions, and memories cannot be accessed, modified, or leaked across tenant or repository boundaries.
4. **Immutable Security Audit Logging:** Every access check—both permitted operations and denied authorization attempts—is logged with structured telemetry, failure reasons, and timestamps into the durable store without exposing secrets, passwords, or raw code prompts.

---

## 2. Trust Boundaries & Scoping Hierarchy

Synapse structures authorization across four concentric security boundaries:

```
+-------------------------------------------------------------------------------+
| Tenant Boundary (tenant_id)                                                   |
|   +-------------------------------------------------------------------------+ |
|   | Repository Boundary (allowed_repositories)                              | |
|   |   +-------------------------------------------------------------------+ | |
|   |   | Team Boundary (team_ids)                                          | | |
|   |   |   +-------------------------------------------------------------+ | | |
|   |   |   | Session / Author Boundary (session_id / principal_id)       | | | |
|   |   |   |   - Active IDE Session State                                | | | |
|   |   |   |   - Personal Scoped Memories (author-only visibility)       | | | |
|   |   |   +-------------------------------------------------------------+ | | |
|   |   +-------------------------------------------------------------------+ | |
|   +-------------------------------------------------------------------------+ |
+-------------------------------------------------------------------------------+
```

### 2.1 Scope Isolation Rules
- **Tenant Boundary:** Principals from `tenant-A` cannot read, resume, or mutate sessions or memory records created under `tenant-B`.
- **Repository Boundary:** Sessions are bound to a single repository upon creation. Principals can only query or store memory for repositories explicitly present in their `allowed_repositories` list (or `*` for administrators).
- **Team Boundary:** Memory cards and decisions marked with `scope = "team"` are strictly restricted to principals who belong to the target `team_id`.
- **Personal Scope Boundary:** Memories marked with `scope = "personal"` are strictly isolated and visible only to the original author (`author_id == principal.principal_id`), preventing other developers from seeing draft notes or personal scratchpad items.

---

## 3. Role-Based Access Control (RBAC) Matrix

### 3.1 Defined Roles
- **`admin`**: Full administrative rights across all operations, tenants, repositories, API key issuance, and security audit log inspection.
- **`maintainer`**: Repository and team lead with rights to manage sessions, read/write memories and architectural decisions, trigger Git ingestion, and read audit logs.
- **`developer`**: Day-to-day engineer with rights to manage sessions, read/write memories, record architectural decisions, and query context.
- **`viewer`**: Read-only stakeholder (e.g. auditor, intern, read-only AI agent) permitted only to query context and read stored memories. Strictly forbidden from creating sessions or storing decisions/facts.
- **`system_service`**: Background automation daemon (e.g. CI/CD indexing bot, Git commit ingestion worker) with rights to ingest Git metadata, manage automated sessions, and store distilled facts.

### 3.2 Discrete Permissions

| Permission | Description | Allowed Roles |
|---|---|---|
| `session:manage` | Create, resume, and close engineering sessions | `admin`, `maintainer`, `developer`, `system_service` |
| `context:query` | Execute ACO context packing and hybrid retrieval | `admin`, `maintainer`, `developer`, `viewer` |
| `memory:read` | Scoped retrieval of engineering memories and recipes | `admin`, `maintainer`, `developer`, `viewer`, `system_service` |
| `memory:write` | Store technical facts, solution patterns, session summaries | `admin`, `maintainer`, `developer`, `system_service` |
| `decision:write` | Record accepted Architectural Decision Records (ADRs) | `admin`, `maintainer`, `developer` |
| `git:ingest` | Ingest commit history and file diffs into memory | `admin`, `maintainer`, `system_service` |
| `audit:read` | Query security audit events and access logs | `admin`, `maintainer` |
| `*` | Superuser bypass for all operations | `admin` |

---

## 4. Authentication Mechanisms

### 4.1 Cryptographic Bearer Tokens (`TokenService`)
Synapse implements an HMAC-SHA256 signed bearer token service conforming to JWT payload semantics:
- **Header:** `{"alg": "HS256", "typ": "JWT"}`
- **Claims Payload:**
  - `sub`: Principal ID (user or service identity)
  - `tid`: Tenant ID
  - `roles`: Assigned roles list
  - `repos`: Allowed repository IDs (or `["*"]`)
  - `teams`: Team memberships list
  - `iat`: Epoch issuance timestamp
  - `exp`: Epoch expiration timestamp
- **Signature:** `HMAC-SHA256(secret_key, base64url(header) + "." + base64url(payload))`
- **Verification:** Constant-time cryptographic comparison (`hmac.compare_digest`) prevents timing side-channel attacks. Expired or tampered tokens are rejected with HTTP 401 `AuthenticationRequiredError`.

### 4.2 High-Entropy API Keys (`ApiKeyService`)
For headless CI/CD agents, IDE extensions, and automation scripts:
- **Format:** `syn_<prefix>_<secret_entropy>` (e.g., `syn_a1b2c3_9e4f2...`)
- **Prefix:** First 10 characters stored in plaintext for human identification and indexing.
- **Security Storage:** Plaintext secrets are **NEVER** stored in the database. Only the cryptographic SHA-256 digest (`key_hash = sha256(raw_key)`) is persisted in `api_keys`.
- **Revocation:** API keys can be immediately revoked via `revoke_api_key(key_id)`, immediately invalidating subsequent requests.
- **Expiration:** Optional ISO 8601 UTC expiration timestamp. Expired keys are rejected on lookup.

### 4.3 Safe Development Mode
In local test environments without production OAuth/OIDC providers:
- If neither `Authorization: Bearer <token>` nor `X-API-Key` is supplied, Synapse provides a transparent development fallback using verified simulation headers (`X-Principal-ID`, `X-Tenant-ID`, `X-Roles`).
- **Production Safety Enforcer:** Setting environment variable `SYNAPSE_REQUIRE_AUTH="true"` permanently disables development fallback, enforcing strict cryptographic authentication across all endpoints.

---

## 5. Security Audit Logging

Security audit events are persisted into the `security_audit_events` table in `TeamMemoryDocumentStore`.

### 5.1 Audit Record Schema
- `id`: Unique audit identifier (`audit_<uuid>`)
- `event_type`: Event category (`authorization_check`, `api_key_create`, `api_key_revoke`, `login`)
- `principal_id`: Authenticated user or service account
- `tenant_id`: Multi-tenant organization scope
- `repository_id`: Associated repository (if applicable)
- `team_id`: Associated team (if applicable)
- `action`: Operation attempted (`create_session`, `query_context`, `store_decision`, `store_fact`, `scoped_retrieval`)
- `resource`: Target session ID or repository ID
- `status`: Outcome (`allow`, `deny`, `revoked`, `expired`)
- `reason`: Explanation for rejection (e.g. `"Roles ['viewer'] lack required permission 'session:manage'"`)
- `ip_address`: Source client IP
- `timestamp`: UTC ISO 8601 timestamp
- `metadata`: Sanitized structured metadata

### 5.2 Mandatory Secret Sanitization
All audit metadata passes through `SecretRedactor.redact_dict` before persistence. Credentials, OpenAI/Anthropic API keys (`sk-...`), GitHub PATs (`ghp_...`), AWS secret access keys, JWT tokens, and private keys are replaced with redaction placeholders (`[REDACTED_API_KEY]`, `[REDACTED_GITHUB_TOKEN]`).

---

## 6. REST API Endpoints Reference

| Method | Path | Required Permission | Description |
|---|---|---|---|
| `GET` | `/api/v1/auth/me` | Authenticated | Inspect current principal claims, roles, and effective permissions |
| `GET` | `/api/v1/security/audit` | `audit:read` | Query historical security audit events (restricted to admin/maintainer) |
| `POST` | `/api/v1/auth/api-keys` | `admin` / `maintainer` | Generate new high-entropy API key |
| `DELETE` | `/api/v1/auth/api-keys/{key_id}` | `admin` / `maintainer` | Revoke an active API key |
| `POST` | `/api/v1/sessions` | `session:manage` | Initialize active engineering session |
| `GET` | `/api/v1/sessions/{id}` | `session:manage` | Resume active engineering session |
| `POST` | `/api/v1/sessions/{id}/close` | `session:manage` | Terminate and lock engineering session |
| `POST` | `/api/v1/sessions/{id}/query` | `context:query` | Orchestrated context retrieval |
| `POST` | `/api/v1/sessions/{id}/decisions` | `decision:write` | Persist ADR |
| `POST` | `/api/v1/sessions/{id}/facts` | `memory:write` | Persist engineering fact / recipe |
| `POST` | `/api/v1/sessions/{id}/distill` | `memory:write` | Record session interaction summary |
| `POST` | `/api/v1/memories/query` | `memory:read` | Scoped retrieval across memory cards |

---

## 7. Verification & Acceptance Evidence

The implementation has been verified by the dedicated test suite [`tests/test_auth_security.py`](file:///C:/FYP/tests/test_auth_security.py) (25 test cases) and the full regression test suite (308 tests total):
1. **RBAC Coverage:** All 5 roles tested against discrete permissions; role inheritance and aliases verified.
2. **Access Control Paths:** Viewer forbidden from mutation (403); developer allowed; admin granted full access.
3. **Multi-Tenant Isolation:** Verified that Tenant A cannot inspect or resume sessions belonging to Tenant B.
4. **API Key Security:** Hashing, verification, expiration, and revocation verified.
5. **Token Cryptography:** Valid tokens succeed; tampered and expired tokens rejected.
6. **Audit Sanitization:** Secret redactor verified to sanitize keys before writing to SQLite/PostgreSQL audit tables.
