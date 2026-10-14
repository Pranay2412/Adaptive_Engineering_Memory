# Synapse Provider-Neutral Session Protocol Specification (v1.0.0)

**Protocol Version:** `1.0.0`  
**Phase:** 6.1 — Provider-Neutral Session Protocol  
**Status:** IMPLEMENTED & TESTED  
**Target Clients:** VS Code, JetBrains, Cursor, Claude Code, Antigravity, Custom CLI  
**Module:** [`backend/session_protocol.py`](file:///C:/FYP/backend/session_protocol.py)  
**Contract Test Suite:** [`tests/test_session_protocol.py`](file:///C:/FYP/tests/test_session_protocol.py)  

---

## 1. Overview & Architectural Goals

The **Synapse Session Protocol** provides a standardized, versioned, and provider-neutral contract enabling heterogeneous developer clients (IDEs, coding assistants, CI bots, and CLI agents) to interact with the Adaptive Engineering Memory platform.

### Core Objectives
1. **Multi-IDE Interoperability:** A single uniform protocol supported across VS Code, JetBrains IDEs, Cursor, Claude Code, Antigravity, and terminal CLI tools without requiring proprietary plugins or leaking internal AST structures.
2. **Explicit Trust Boundaries:** Client-supplied user/team identifiers are treated as unverified telemetry; all access and scoping decisions are enforced against an `AuthenticatedPrincipal` asserted by server auth middleware.
3. **Strict Scope & Repository Isolation:** Sessions are strictly bound to a single repository worktree. Context queries and decision writes cannot accidentally query or cross-contaminate disparate repositories.
4. **Secret Redaction & Privacy Guardrails:** Automatic scanning and masking of credentials, tokens, and keys (OpenAI, Anthropic, GitHub PATs, AWS access/secret keys, JWTs, private keys) before ingestion, context orchestration, or storage.
5. **Idempotent Mutations:** Write operations accept client-generated `idempotency_key` tokens to ensure network retries and duplicate transmissions never produce duplicate decisions, memories, or duplicate session records.
6. **Token-Budgeted Context Delivery:** Context queries seamlessly integrate with the Adaptive Context Orchestrator (ACO) to pack relevant knowledge into a deterministic BPE token budget.

---

## 2. Protocol Versioning & Compatibility Invariants

- **Active Version:** `1.0.0`
- **Supported Versions:** `{"1.0.0"}`
- Any request presenting an unsupported version or omitting version fields returns `ErrorCode.UNSUPPORTED_PROTOCOL_VERSION` with a list of supported versions.
- **Backward Compatibility Guarantee:** Minor version increments (`1.1.0`) will only introduce optional fields and additive operations. Breaking schema changes require version `2.0.0`.

---

## 3. Trust Model & Scope Isolation

```
[ Client / IDE (Untrusted Environment) ]
           │
           │  (ClientIdentity + Request Payload)
           ▼
[ Auth Middleware / Gateway (Trust Boundary) ]
           │
           │  Asserts AuthenticatedPrincipal (Verified ID, Tenant, Allowed Repos)
           ▼
[ SessionProtocolEngine (Strict Scope Enforcement) ]
           ├── 1. Verify principal.can_access_repository(repo_id)
           ├── 2. Verify session.repository_id == request.repository_id
           ├── 3. Redact all credentials/keys via SecretRedactor
           ├── 4. Check idempotency cache
           └── 5. Dispatch to ACO / Document Store / Session Service
```

### Scope Boundaries
| Scope | Visibility | Permission Rule |
| :--- | :--- | :--- |
| **Personal** | Developer only | Only accessible by the creator (`principal.principal_id`). |
| **Team** | Team members | Creator must belong to `team_id` (`principal.team_ids`). |
| **Repository** | Repository contributors | Principal must have read/write permission for `repository_id`. |

---

## 4. Standard Error Envelope & Catalog

All protocol errors return HTTP 4xx/5xx equivalent structured payloads with `success: false`:

```json
{
  "success": false,
  "envelope": {
    "protocol_version": "1.0.0",
    "request_id": "req_8f1b2c4d9e0a",
    "timestamp": "2026-10-14T12:00:00Z",
    "idempotency_key": null
  },
  "error": {
    "code": "REPOSITORY_MISMATCH",
    "message": "Repository 'repo-frontend' does not match session 'sess_123' repository 'repo-core-api'.",
    "field": "repository_id",
    "details": {
      "session_id": "sess_123",
      "expected_repository_id": "repo-core-api",
      "provided_repository_id": "repo-frontend"
    },
    "retryable": false
  }
}
```

### Error Code Catalog
| Error Code | Description | Retryable |
| :--- | :--- | :--- |
| `MALFORMED_INPUT` | Missing required field, negative number, or schema violation. | No |
| `UNSUPPORTED_PROTOCOL_VERSION` | Client requested unsupported protocol version. | No |
| `AUTHENTICATION_REQUIRED` | Missing or invalid authentication token. | No |
| `ACCESS_DENIED` | Principal not authorized for repository or cross-tenant session. | No |
| `SESSION_NOT_FOUND` | `session_id` does not exist in store. | No |
| `SESSION_CLOSED` | Attempted write or query operation on a closed session. | No |
| `SESSION_EXPIRED` | Session TTL has elapsed. | No |
| `REPOSITORY_MISMATCH` | Request `repository_id` does not match session's bound repository. | No |
| `IDEMPOTENCY_CONFLICT` | Same `idempotency_key` transmitted with different payload. | No |
| `PAYLOAD_TOO_LARGE` | Query, prompt, or content exceeds maximum allowed character length. | No |
| `INVALID_SCOPE` | Specified scope is invalid or principal lacks team membership. | No |
| `RATE_LIMITED` | Client exceeded request concurrency or throughput limits. | Yes |
| `INTERNAL_ERROR` | Unexpected backend server error. | Yes |

---

## 5. Operations & Payload Specifications

### 5.1 Session Lifecycle

#### `SessionCreateRequest`
```json
{
  "envelope": {
    "protocol_version": "1.0.0",
    "request_id": "req_create_001",
    "timestamp": "2026-10-14T12:00:00Z",
    "idempotency_key": "idemp_sess_init_42"
  },
  "repository": {
    "repository_id": "repo-core-api",
    "name": "Core Authentication API",
    "branch": "main",
    "commit_sha": "a1b2c3d4"
  },
  "client": {
    "client_type": "vscode",
    "client_version": "1.85.0",
    "assistant_name": "antigravity",
    "assistant_version": "2.1.0",
    "environment": {"os": "windows"}
  },
  "title": "Investigating JWT verification failures",
  "ttl_seconds": 86400,
  "metadata": {"task": "PH-043"}
}
```

#### `SessionCreateResponse`
```json
{
  "success": true,
  "envelope": {
    "protocol_version": "1.0.0",
    "request_id": "req_create_001",
    "timestamp": "2026-10-14T12:00:01Z",
    "idempotency_key": "idemp_sess_init_42"
  },
  "session_id": "sess_8f9e1d2c3b4a",
  "status": "active",
  "repository_id": "repo-core-api",
  "created_at": "2026-10-14T12:00:01Z",
  "expires_at": "2026-10-15T12:00:01Z",
  "metadata": {"task": "PH-043"}
}
```

---

### 5.2 Context Query Operation

#### `ContextQueryRequest`
```json
{
  "envelope": {
    "protocol_version": "1.0.0",
    "request_id": "req_query_002",
    "timestamp": "2026-10-14T12:01:00Z"
  },
  "session_id": "sess_8f9e1d2c3b4a",
  "repository_id": "repo-core-api",
  "query": "Where could JWT verification fail and what code is involved?",
  "max_tokens": 1500,
  "optimization_mode": "deterministic",
  "active_file": "backend/auth.py",
  "include_scratchpad": false
}
```

#### `ContextQueryResponse`
```json
{
  "success": true,
  "envelope": {
    "protocol_version": "1.0.0",
    "request_id": "req_query_002",
    "timestamp": "2026-10-14T12:01:02Z"
  },
  "session_id": "sess_8f9e1d2c3b4a",
  "repository_id": "repo-core-api",
  "intent": "debugging",
  "formatted_context": "### Architectural Decision: ADR-001 RS256 JWT Verification\n...\n### Source Code: backend/auth.py\ndef verify_jwt_token(token: str)...",
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
  "latency_ms": 48.2
}
```

---

### 5.3 Storing Architectural Decisions (`StoreDecision`)

#### `StoreDecisionRequest`
```json
{
  "envelope": {
    "protocol_version": "1.0.0",
    "request_id": "req_dec_003",
    "timestamp": "2026-10-14T12:05:00Z",
    "idempotency_key": "idemp_dec_rs256_01"
  },
  "session_id": "sess_8f9e1d2c3b4a",
  "repository_id": "repo-core-api",
  "title": "Migrate JWT token verification to RS256 asymmetric keys",
  "rationale": "RS256 allows microservices to verify JWT authenticity using the public key without distributing the signing key.",
  "scope": "repository",
  "category": "architecture",
  "status": "accepted",
  "impact_areas": ["backend/auth.py", "backend/security.py"],
  "affected_symbols": ["verify_jwt_token", "create_access_token"]
}
```

#### `StoreDecisionResponse`
```json
{
  "success": true,
  "envelope": {
    "protocol_version": "1.0.0",
    "request_id": "req_dec_003",
    "timestamp": "2026-10-14T12:05:01Z",
    "idempotency_key": "idemp_dec_rs256_01"
  },
  "decision_id": "dec_3a8b2c1d9e",
  "status": "stored",
  "scope": "repository"
}
```

---

### 5.4 Scoped Memory Retrieval (`ScopedRetrieval`)

#### `ScopedRetrievalRequest`
```json
{
  "envelope": {
    "protocol_version": "1.0.0",
    "request_id": "req_retrieve_004",
    "timestamp": "2026-10-14T12:10:00Z"
  },
  "repository_id": "repo-core-api",
  "query": "authentication token RS256",
  "allowed_scopes": ["repository", "team"],
  "category_filter": ["architecture", "bug_root_cause"],
  "limit": 5
}
```

#### `ScopedRetrievalResponse`
```json
{
  "success": true,
  "envelope": {
    "protocol_version": "1.0.0",
    "request_id": "req_retrieve_004",
    "timestamp": "2026-10-14T12:10:01Z"
  },
  "repository_id": "repo-core-api",
  "query": "authentication token RS256",
  "total_found": 1,
  "memories": [
    {
      "id": "mem_dec_3a8b2c1d9e",
      "title": "Architecture Decision: Migrate JWT token verification to RS256 asymmetric keys",
      "category": "architecture",
      "scope": "repository",
      "confidence": 0.95
    }
  ],
  "scopes_included": ["repository", "team"]
}
```

---

## 6. Secret Redaction Rules

Before any text is evaluated by the ACO, stored in the SQLite document store, or synchronized to the Neo4j knowledge graph, `SecretRedactor` executes pattern matching against:

| Secret Type | Regex Pattern Tested | Sanitized Output |
| :--- | :--- | :--- |
| **OpenAI API Key** | `\b(sk-[a-zA-Z0-9_\-]{20,})\b` | `[REDACTED_API_KEY]` |
| **Anthropic Key** | `\b(sk-ant-[a-zA-Z0-9_\-]{20,})\b` | `[REDACTED_API_KEY]` |
| **GitHub Token** | `\b(gh[pousr]_[A-Za-z0-9_]{36,})\b` | `[REDACTED_GITHUB_TOKEN]` |
| **AWS Access Key** | `\b(AKIA[0-9A-Z]{16})\b` | `[REDACTED_AWS_KEY]` |
| **AWS Secret** | `(?i)(?:aws_secret_access_key\|secret_key)\s*[:=]\s*...` | `[REDACTED_AWS_SECRET]` |
| **JWT Token** | `\b(eyJ[a-zA-Z0-9_-]+\.eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+)\b` | `[REDACTED_JWT_TOKEN]` |
| **Private Keys** | `-----BEGIN [A-Z ]+ PRIVATE KEY-----...` | `[REDACTED_PRIVATE_KEY]` |
| **URL Passwords** | `https?://[^:]+:([^@]+)@` | `https://user:[REDACTED_PASSWORD]@...` |

---

## 7. Migration & Backward Compatibility Strategy

1. **Schema Extension Invariant:** Fields added in future protocol revisions (e.g. `1.1.0`) must have default values in dataclasses. Existing clients sending `1.0.0` envelopes will continue to function without modification.
2. **Internal Model Bridges:** The protocol layer sits above `backend/models.py`, `backend/orchestrator.py`, and `backend/session_memory.py`. It maps domain models (`AISessionRecord`, `Memory`, `EngineeringDecision`) to protocol schemas without mutating legacy tables.
3. **Database Independence:** The protocol schemas are purely transport-agnostic and decouple IDE clients from whether the storage backend is SQLite, PostgreSQL, Neo4j, or an in-memory test store.
