# Synapse Session & Context REST API Specification (Phase 6.2)

**Version:** `1.0.0`  
**Phase:** 6.2 — Session/Context API  
**Status:** IMPLEMENTED & TESTED  
**Service Entrypoint:** [`backend/api.py`](file:///C:/FYP/backend/api.py)  
**Interactive Docs:** `http://localhost:8000/docs` (Swagger UI) / `http://localhost:8000/openapi.json`  
**Security Status:** DEVELOPMENT-ONLY (Cryptographic JWT auth scheduled for Phase 9)  

---

## 1. Overview

The **Synapse Session & Context REST API** exposes the Adaptive Context Orchestrator (ACO), document stores, and session memory through a stable HTTP interface. It allows external clients (such as IDE extensions for VS Code, JetBrains, or independent CLI utilities) to interact with the system **without importing backend internal Python modules**.

### Key Architectural Characteristics
- **Transport Agnostic:** JSON REST payload envelopes with structured error formats.
- **Deterministic Token Accounting:** All context query endpoints return token budgets, exact BPE tokens utilized, budget utilization percentage, item counts, and multi-channel provenance.
- **Strict Scope Isolation:** Every request is authenticated against an authorized repository and scope. Repository mismatches return HTTP 409 Conflict.
- **Automatic Secret Redaction:** API keys, personal access tokens, AWS keys, JWTs, and private keys in queries or titles are masked before execution.
- **Idempotency Support:** Mutating endpoints accept client-generated `idempotency_key` tokens to safely handle network retries without duplicate writes.

---

## 2. Security Disclaimer (Development Mode)

> [!WARNING]
> **DEVELOPMENT-ONLY MODE**  
> In Phase 6.2, authentication headers (`X-Principal-ID`, `X-Tenant-ID`, `X-Team-IDs`, `X-Allowed-Repositories`) are used to simulate authorized principals during local development.  
> **Body-supplied user/team identifiers are NEVER trusted as secure identity.**  
> Cryptographic JWT / OAuth2 token verification, role-based access control (RBAC), and tenant isolation will be implemented in **Phase 9**.

---

## 3. Route Summary

| Method | Route | Description | Success Status |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` / `/api/v1/health` | Service health status and version | 200 OK |
| `GET` | `/api/v1/status` | Active sessions count and database connectivity | 200 OK |
| `POST` | `/api/v1/sessions` | Create new session bound to repository and client | 201 Created |
| `GET` | `/api/v1/sessions/{session_id}` | Inspect active session state | 200 OK |
| `POST` | `/api/v1/sessions/{session_id}/resume` | Resume active session and update telemetry | 200 OK |
| `POST` | `/api/v1/sessions/{session_id}/close` | Close session and finalize engineering summary | 200 OK |
| `POST` | `/api/v1/sessions/{session_id}/context` | Retrieve budgeted, orchestrated context for query | 200 OK |
| `POST` | `/api/v1/sessions/{session_id}/decisions`| Store architectural decision (ADR) | 201 Created |
| `POST` | `/api/v1/sessions/{session_id}/facts` | Store technical recipe, fact, or bug root cause | 201 Created |
| `POST` | `/api/v1/sessions/{session_id}/distill` | Distill full session prompt & response into memory | 200 OK |
| `POST` | `/api/v1/memories/query` | Scoped retrieval across repository & team memories | 200 OK |

---

## 4. Example Usage & curl Commands

### 4.1 Check Service Health
```bash
curl -X GET http://localhost:8000/api/v1/health
```
**Response:**
```json
{
  "status": "ok",
  "service": "synapse-session-context-api",
  "version": "1.0.0",
  "mode": "development-only",
  "timestamp": "2026-10-14T12:00:00Z"
}
```

---

### 4.2 Create a Development Session
```bash
curl -X POST http://localhost:8000/api/v1/sessions \
  -H "Content-Type: application/json" \
  -H "X-Principal-ID: user-alice" \
  -H "X-Allowed-Repositories: repo-core-api" \
  -d '{
    "repository": {
      "repository_id": "repo-core-api",
      "name": "Core Authentication API",
      "branch": "main"
    },
    "client": {
      "client_type": "vscode",
      "client_version": "1.85.0",
      "assistant_name": "antigravity"
    },
    "title": "Investigating JWT token verification",
    "ttl_seconds": 86400
  }'
```
**Response (201 Created):**
```json
{
  "success": true,
  "envelope": {
    "protocol_version": "1.0.0",
    "request_id": "req_2026-10-14_102030",
    "timestamp": "2026-10-14T12:00:01Z",
    "idempotency_key": null
  },
  "session_id": "sess_da866678c67c",
  "status": "active",
  "repository_id": "repo-core-api",
  "created_at": "2026-10-14T12:00:01Z",
  "expires_at": "2026-10-15T12:00:01Z",
  "metadata": {}
}
```

---

### 4.3 Query Budgeted Engineering Context
```bash
curl -X POST http://localhost:8000/api/v1/sessions/sess_da866678c67c/context \
  -H "Content-Type: application/json" \
  -H "X-Principal-ID: user-alice" \
  -d '{
    "repository_id": "repo-core-api",
    "query": "Where could JWT verification fail and what code is involved?",
    "max_tokens": 1500,
    "optimization_mode": "deterministic"
  }'
```
**Response (200 OK):**
```json
{
  "success": true,
  "envelope": {
    "protocol_version": "1.0.0",
    "request_id": "req_2026-10-14_102031",
    "timestamp": "2026-10-14T12:00:02Z"
  },
  "session_id": "sess_da866678c67c",
  "repository_id": "repo-core-api",
  "intent": "debugging",
  "formatted_context": "#### Architecture Decisions & ADRs\n- Architecture Decision: RS256 JWT Token Choice...\n\n#### Code Symbols\n- def verify_jwt_token(token: str)...",
  "token_budget": 1500,
  "tokens_used": 953,
  "budget_utilization_pct": 63.53,
  "items_packed": 6,
  "items_retained_by_category": {
    "architecture_decision": 1,
    "code_symbol": 3,
    "bug_root_cause": 2
  },
  "provenance": [
    {"source": "session_memory", "repository": "repo-core-api", "confidence": 0.95},
    {"source": "graphify", "repository": "repo-core-api", "confidence": 0.90}
  ],
  "latency_ms": 32.4
}
```

---

### 4.4 Store an Architectural Decision (ADR)
```bash
curl -X POST http://localhost:8000/api/v1/sessions/sess_da866678c67c/decisions \
  -H "Content-Type: application/json" \
  -H "X-Principal-ID: user-alice" \
  -d '{
    "repository_id": "repo-core-api",
    "title": "Adopt RS256 for asymmetric JWT verification",
    "rationale": "Asymmetric signatures allow client services to verify authenticity without knowing the private key.",
    "scope": "repository",
    "category": "architecture",
    "status": "accepted",
    "impact_areas": ["backend/auth.py", "backend/security.py"]
  }'
```
**Response (201 Created):**
```json
{
  "success": true,
  "envelope": {
    "protocol_version": "1.0.0",
    "request_id": "req_2026-10-14_102032",
    "timestamp": "2026-10-14T12:00:03Z",
    "idempotency_key": null
  },
  "decision_id": "dec_3a8b2c1d9e",
  "status": "stored",
  "scope": "repository"
}
```

---

### 4.5 Close the Session
```bash
curl -X POST http://localhost:8000/api/v1/sessions/sess_da866678c67c/close \
  -H "Content-Type: application/json" \
  -H "X-Principal-ID: user-alice" \
  -d '{
    "summary": "Completed investigation and documented RS256 token verification architecture."
  }'
```
**Response (200 OK):**
```json
{
  "success": true,
  "envelope": {
    "protocol_version": "1.0.0",
    "request_id": "req_2026-10-14_102033",
    "timestamp": "2026-10-14T12:00:04Z"
  },
  "session_id": "sess_da866678c67c",
  "status": "closed",
  "closed_at": "2026-10-14T12:00:04Z",
  "summary": "Completed investigation and documented RS256 token verification architecture."
}
```

---

## 5. Error Catalog & HTTP Status Codes

When an operation violates protocol constraints, a structured JSON error response is returned with the corresponding HTTP code:

| Error Code | HTTP Status | Description |
| :--- | :--- | :--- |
| `MALFORMED_INPUT` | 400 Bad Request | Payload failed validation (e.g., negative budget or missing field). |
| `PAYLOAD_TOO_LARGE` | 400 Bad Request | Text length exceeds protocol constants (e.g., query > 8192 chars). |
| `AUTHENTICATION_REQUIRED` | 401 Unauthorized | Unauthenticated request. |
| `ACCESS_DENIED` | 403 Forbidden | Principal lacks permission for repository or tenant. |
| `INVALID_SCOPE` | 403 Forbidden | Principal attempted to write to team memory without team membership. |
| `SESSION_NOT_FOUND` | 404 Not Found | Requested `session_id` does not exist. |
| `SESSION_CLOSED` | 409 Conflict | Cannot query or write to a closed session. |
| `SESSION_EXPIRED` | 409 Conflict | Session TTL has elapsed. |
| `REPOSITORY_MISMATCH` | 409 Conflict | Request `repository_id` does not match session worktree. |
| `IDEMPOTENCY_CONFLICT` | 409 Conflict | Reused `idempotency_key` with different payload. |
| `UNSUPPORTED_PROTOCOL_VERSION` | 422 Unprocessable | Incompatible protocol version. |
