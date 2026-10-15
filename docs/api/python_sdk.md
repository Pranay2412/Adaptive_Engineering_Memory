# Synapse Python SDK Specification & Guide (Phase 11)

**Version:** `1.0.0`  
**Phase:** 11 — Python Client SDK  
**Status:** IMPLEMENTED & VERIFIED (358/358 passing tests)  
**Package Directory:** [`synapse_sdk/`](file:///C:/FYP/synapse_sdk)  
- Package Root: [`synapse_sdk/__init__.py`](file:///C:/FYP/synapse_sdk/__init__.py)
- Client: [`synapse_sdk/client.py`](file:///C:/FYP/synapse_sdk/client.py)
- Configuration: [`synapse_sdk/config.py`](file:///C:/FYP/synapse_sdk/config.py)
- Domain Models: [`synapse_sdk/models.py`](file:///C:/FYP/synapse_sdk/models.py)
- Exceptions: [`synapse_sdk/exceptions.py`](file:///C:/FYP/synapse_sdk/exceptions.py)
- Executable Example: [`examples/sdk_usage_example.py`](file:///C:/FYP/examples/sdk_usage_example.py)
- Test Suite: [`tests/test_sdk.py`](file:///C:/FYP/tests/test_sdk.py)

---

## 1. Overview & Architectural Principles

The **Synapse Python SDK** is the official, typed client library for integrating coding assistants, IDE extensions, CI/CD pipelines, and autonomous agent workflows with the Synapse Adaptive Engineering Memory Platform.

### Key Architectural Tenets
1. **Zero Internal Backend Imports:** The SDK communicates strictly over standard HTTP REST protocols. It operates as an external consumer without coupling to internal backend modules (`backend.*` is never imported by the SDK).
2. **Strict Idempotency-Aware Safe Retries:** Mutating operations (`POST` requests) without a recognized `idempotency_key` are **NEVER blindly retried** on failures, eliminating the risk of duplicate writes or ghost sessions.
3. **Connection Lifecycle & Resource Pooling:** Built upon `httpx.Client`, supporting connection reuse, keep-alive, context management (`with SynapseClient(...) as client:`), and explicit `.close()`.
4. **Structured Typed Models:** Every request and response maps to immutable dataclasses with field-level typing, docstrings, and helper methods.
5. **Expressive Exception Hierarchy:** Protocol errors (400, 401, 403, 404, 409, 413, 429, 500, 504) are automatically parsed from the `ProtocolErrorResponse` envelope into dedicated typed exception classes.

```
+───────────────────────────────────────────────────────────────────────────────────+
|                  External Coding Assistant / Developer Script                     |
+───────────────────────────────────────────────────────────────────────────────────+
                                         │
                                         ▼
+───────────────────────────────────────────────────────────────────────────────────+
|                    SynapseClient (synapse_sdk.SynapseClient)                      |
| - Authentication injection (Bearer JWT or API Key)                               |
| - Request dispatcher with safe idempotency evaluation                             |
| - Context manager & connection lifecycle                                          |
+───────────────────────────────────────────────────────────────────────────────────+
                                         │
                   ┌─────────────────────┴─────────────────────┐
                   │                                           │
         Idempotent Request                          Non-Idempotent Request
        (GET, or with Key)                            (POST without Key)
                   │                                           │
                   ▼                                           ▼
+───────────────────────────────────────+   +───────────────────────────────────────+
|  Transient Retry Loop with Backoff    |   |         Single-Shot Execution         |
|  - Retries on 502, 503, 504, Timeout  |   |  - On failure: raises immediately     |
|  - Sleep: factor * (2 ** attempt)     |   |  - NO automatic retry                 |
+───────────────────────────────────────+   +───────────────────────────────────────+
                                         │
                                         ▼
+───────────────────────────────────────────────────────────────────────────────────+
|               Synapse REST API Endpoint (/api/v1/sessions, etc.)                  |
+───────────────────────────────────────────────────────────────────────────────────+
```

---

## 2. Configuration Reference (`ClientConfig`)

[`ClientConfig`](file:///C:/FYP/synapse_sdk/config.py) manages client networking, credentials, and retry parameters:

```python
from synapse_sdk import ClientConfig, SynapseClient

config = ClientConfig(
    base_url="http://localhost:8000",             # Target API host and port
    api_key="synapse_sk_...",                     # API key (sent as X-API-Key)
    bearer_token="eyJhbG...",                     # Bearer JWT token (sent as Authorization: Bearer)
    timeout=30.0,                                 # Per-request timeout in seconds
    max_retries=3,                                # Max retry attempts on transient failures
    backoff_factor=0.5,                           # Exponential backoff factor (seconds)
    auto_idempotency_keys=False,                  # Auto-generate UUID keys for mutating writes
    custom_headers={"X-Client-App": "MyIDE"},    # Additional HTTP headers
)

client = SynapseClient(config=config)
```

---

## 3. Quickstart & Basic Usage

```python
from synapse_sdk import SynapseClient

# 1. Initialize client using context manager
with SynapseClient(base_url="http://localhost:8000", api_key="synapse_sk_...") as client:

    # 2. Check service liveness and readiness
    health = client.check_health()
    readiness = client.check_readiness()
    print(f"Service status: {health.status}, DB readiness: {readiness.checks.get('database')}")

    # 3. Create an active development session
    session = client.create_session(
        repository_id="repo-payments",
        title="Refactor Stripe Webhook Handler",
        client_type="vscode",
    )
    print(f"Session active: {session.session_id}")

    # 4. Store an accepted architectural decision (ADR)
    decision = client.store_decision(
        session_id=session.session_id,
        repository_id="repo-payments",
        title="Enforce Idempotency-Key Header on Webhooks",
        rationale="Stripe webhooks can deliver duplicates during network partitions.",
        category="architecture",
        impact_areas=["payments", "stripe"],
        affected_symbols=["process_webhook"],
    )
    print(f"Recorded decision: {decision.decision_id}")

    # 5. Retrieve token-budgeted context via Adaptive Context Orchestrator (ACO)
    context = client.query_context(
        session_id=session.session_id,
        repository_id="repo-payments",
        query="How are duplicate webhooks handled?",
        max_tokens=1500,
    )
    print(f"ACO returned {len(context.items)} items ({context.tokens_used}/{context.budget_allocated} tokens)")

    # 6. Distill conversation interaction into durable memory
    distill = client.distill_session(
        session_id=session.session_id,
        repository_id="repo-payments",
        prompt="How are duplicate webhooks handled?",
        response_summary="Checked idempotency key in payment_events table before processing.",
        affected_files=["services/webhook.py"],
    )
    print(f"Distilled {distill.distilled_memories_count} memory cards")

    # 7. Close session upon task completion
    closed = client.close_session(session.session_id, summary="Webhook idempotency implemented.")
    print(f"Session status: {closed.status}")
```

---

## 4. API Method Reference

### System Probes & Diagnostics
- `client.check_health() -> HealthStatus`: Returns service status, version, and mode.
- `client.check_readiness() -> ReadinessStatus`: Returns operational checks for database and orchestrator.
- `client.get_service_status() -> dict[str, Any]`: Detailed internal telemetry, active session counts, and version history.
- `client.get_config() -> dict[str, Any]`: Safe runtime configuration diagnostics with credentials redacted.

### Session Management
- `client.create_session(repository_id, title="", client_type="cli", assistant_name="python-sdk", ttl_seconds=86400, metadata=None, idempotency_key=None) -> SessionInfo`
- `client.resume_session(session_id, client_type="cli", assistant_name="python-sdk") -> SessionInfo`
- `client.get_session(session_id) -> SessionInfo`
- `client.close_session(session_id, reason="completed", summary="", idempotency_key=None) -> SessionInfo`

### Context Orchestration
- `client.query_context(session_id, repository_id, query, max_tokens=1500, optimization_mode="deterministic", active_file=None, cursor_position=None, include_scratchpad=False) -> ContextResult`

### Engineering Memory Writes
- `client.store_decision(session_id, repository_id, title, rationale, scope="repository", category="architecture", status="accepted", impact_areas=None, affected_symbols=None, team_id=None, idempotency_key=None) -> DecisionRecord`
- `client.store_fact(session_id, repository_id, title, content, category="implementation_summary", scope="repository", actionable_takeaways=None, impact_areas=None, team_id=None, idempotency_key=None) -> FactRecord`
- `client.distill_session(session_id, repository_id, prompt, response_summary, affected_files=None, affected_symbols=None, engineering_decisions=None, visibility="repository", team_id=None, store_raw_transcript=False, raw_messages=None) -> DistillationResult`

### Scoped Memory Retrieval
- `client.query_memories(repository_id, query, allowed_scopes=None, category_filter=None, limit=10) -> ScopedQueryResult`

### Security, RBAC & Audit Trails
- `client.get_my_principal() -> dict[str, Any]`: Verified principal ID, roles, and effective permissions.
- `client.list_audit_events(tenant_id=None, principal_id=None, event_type=None, limit=50) -> list[dict[str, Any]]`: Security audit trail query (restricted to admin or maintainer roles).
- `client.create_api_key(name, roles=None, allowed_repositories=None, allowed_teams=None, expires_at=None) -> dict[str, Any]`: Generate high-entropy API key.
- `client.revoke_api_key(key_id) -> bool`: Revoke active API key.

---

## 5. Safe Retry Policy Specifications

| Operation Type | HTTP Verbs | Transient Errors (502, 503, 504, Timeout) | Automatic Retry Policy |
| :--- | :--- | :--- | :--- |
| **Idempotent Reads** | `GET` | Eligible | **Retries up to `max_retries`** with exponential backoff |
| **Scoped Queries** | `POST /memories/query` | Eligible | **Retries up to `max_retries`** (read-only search) |
| **Context Retrieval**| `POST /context` | Eligible | **Retries up to `max_retries`** (read-only orchestration) |
| **Mutating Writes with Key** | `POST` (with `idempotency_key`) | Eligible | **Retries up to `max_retries`** (server deduplicates replay) |
| **Mutating Writes without Key** | `POST` (no `idempotency_key`) | Any failure | **NO RETRY**. Raises exception immediately to caller |

### Backoff Calculation
$$\text{sleep\_time} = \text{backoff\_factor} \times 2^{\text{attempt}}$$

---

## 6. Exception Hierarchy

All exceptions inherit from [`SynapseError`](file:///C:/FYP/synapse_sdk/exceptions.py#L11):

```text
SynapseError
├── SynapseConnectionError          # Failed to connect / network disconnect
├── SynapseTimeoutError             # Client or gateway timeout
└── SynapseApiError                 # Structured HTTP response errors
    ├── ValidationError             # HTTP 400 or 422 (MALFORMED_INPUT)
    ├── AuthenticationError         # HTTP 401 (AUTHENTICATION_REQUIRED)
    ├── AccessDeniedError           # HTTP 403 (ACCESS_DENIED)
    ├── InvalidScopeError           # HTTP 403 (INVALID_SCOPE)
    ├── NotFoundError               # HTTP 404 (SESSION_NOT_FOUND)
    ├── ConflictError               # HTTP 409 (SESSION_CLOSED, REPOSITORY_MISMATCH)
    │   └── IdempotencyConflictError # HTTP 409 (IDEMPOTENCY_CONFLICT)
    ├── PayloadTooLargeError        # HTTP 413 (PAYLOAD_TOO_LARGE)
    ├── RateLimitError              # HTTP 429 (RATE_LIMITED)
    └── ServerError                 # HTTP 500, 502, 503, 504 (INTERNAL_ERROR)
```

### Exception Inspection Attributes
Every `SynapseApiError` provides:
- `.status_code: int`: HTTP status code.
- `.error_code: str`: Machine-readable protocol error string (e.g. `SESSION_CLOSED`).
- `.request_id: str | None`: Correlation request ID for troubleshooting.
- `.retryable: bool`: Whether the server flagged the error as retryable.
- `.details: dict[str, Any]`: Server-provided diagnostic metadata.

---

## 7. Verification & Automated Test Results

The SDK is verified by the automated test suite [`tests/test_sdk.py`](file:///C:/FYP/tests/test_sdk.py):

| Test Category | Tested Capabilities | Status |
| :--- | :--- | :--- |
| **Config & Auth** | Defaults, bounds validation, Bearer token, API key, dev simulation headers | PASSED |
| **Lifecycle** | Normal close, double close safety, context manager auto-close, post-close rejection | PASSED |
| **Mock Operations**| Health, readiness, create/resume/close session, query context, store decision/fact, distill, query memories | PASSED |
| **Exception Matrix**| 400, 401, 403, 404, 409, 413, 422, 429, 500 status code mapping | PASSED |
| **Retry Semantics** | Idempotent retries succeeding on 3rd attempt; non-idempotent writes never retrying; keyed writes safely retrying | PASSED |
| **End-to-End E2E** | Full multi-step workflow executed against in-process FastAPI application | PASSED |

**Total passing tests across the entire platform:** `358 passed in 25.32s`.
