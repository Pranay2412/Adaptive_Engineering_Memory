# Synapse FastAPI Service Specification (Phase 10)

**Version:** `1.0.0`  
**Phase:** 10 — FastAPI Service Packaging & Production Readiness  
**Status:** IMPLEMENTED & VERIFIED (330/330 passing tests)  
**Implementation Modules:**
- Application Factory & Routes: [`backend/api.py`](file:///C:/FYP/backend/api.py)
- Configuration & Validation: [`backend/config.py`](file:///C:/FYP/backend/config.py)
- CLI Runner: [`backend/server.py`](file:///C:/FYP/backend/server.py)
- Environment Template: [`.env.example`](file:///C:/FYP/.env.example)
- Comprehensive Test Suite: [`tests/test_fastapi_service.py`](file:///C:/FYP/tests/test_fastapi_service.py)

---

## 1. Overview & Architecture

Phase 10 promotes the Synapse Session & Context REST API from a development-only prototype to a hardened, production-ready microservice. The service wraps the Adaptive Context Orchestrator (ACO), document stores, and cryptographic RBAC security boundary with:

1. **Typed Configuration & Safety Guardrails:** Strict runtime bounds checking, environment variables loader, and rejection of development secrets in production environments.
2. **Modern Lifespan Lifecycle:** Structured startup and shutdown context management for database connections and background workers.
3. **Observability & Probes:** Dual-probe Kubernetes-compatible architecture separating liveness (`/health`) from operational readiness (`/ready`).
4. **Defensive Request Guardrails:** Content-Length and body payload size limiting (HTTP 413) and configurable request execution timeouts (HTTP 504).
5. **Production Error Masking:** Sanitization of unhandled server exceptions to prevent filesystem path, SQL query, or credential leakage in production mode.
6. **Unified CLI Server:** Command-line entry point supporting environment overrides, interface binding, port selection, and development reloading.

```
+-----------------------------------------------------------------------------------+
|                           HTTP Request / Client Pipeline                          |
+-----------------------------------------------------------------------------------+
                                         │
                                         ▼
+───────────────────────────────────────────────────────────────────────────────────+
|                           CORS & Security Middleware                              |
| - Origin validation (Strict list in production; wildcard prohibited with auth)    |
| - Standard headers: X-Request-ID, X-Response-Time-Ms, X-Synapse-Mode              |
+───────────────────────────────────────────────────────────────────────────────────+
                                         │
                                         ▼
+───────────────────────────────────────────────────────────────────────────────────+
|                    Payload Size Limiter (HTTP 413 Guard)                          |
| - Fast-reject Content-Length > config.max_request_payload_bytes                   |
| - Inspects chunked/streamed bodies on mutating verbs (POST, PUT, PATCH)           |
+───────────────────────────────────────────────────────────────────────────────────+
                                         │
                                         ▼
+───────────────────────────────────────────────────────────────────────────────────+
|                     Request Timeout Guard (HTTP 504)                              |
| - Wraps downstream execution with asyncio.wait_for(timeout=config.timeout)        |
| - Returns structured ProtocolErrorResponse on deadline expiration                |
+───────────────────────────────────────────────────────────────────────────────────+
                                         │
                                         ▼
+───────────────────────────────────────────────────────────────────────────────────+
|                        Authentication & RBAC Dependency                           |
| - Bearer JWT Token verification (Phase 9)                                         |
| - High-entropy SHA-256 API Key verification (Phase 9)                             |
| - Local simulation headers (Development fallback only)                            |
+───────────────────────────────────────────────────────────────────────────────────+
                                         │
                                         ▼
+───────────────────────────────────────────────────────────────────────────────────+
|                   Route Handlers & Error Sanitization (HTTP 500)                  |
| - In Development: Full error message returned for rapid local debugging           |
| - In Production: Traceback logged securely; generic sanitized message to client   |
+───────────────────────────────────────────────────────────────────────────────────+
```

---

## 2. Configuration Reference (`SynapseConfig`)

Configuration is declared in the dataclass [`SynapseConfig`](file:///C:/FYP/backend/config.py) and loaded via [`load_config_from_env()`](file:///C:/FYP/backend/config.py).

### Environment Variables & Defaults

| Variable | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `SYNAPSE_ENV` | `string` | `development` | Runtime mode: `development`, `staging`, `production`, `testing` |
| `SYNAPSE_HOST` | `string` | `127.0.0.1` | Network interface address to bind |
| `SYNAPSE_PORT` | `integer` | `8000` | Port to listen on (range: `1` to `65535`) |
| `SYNAPSE_SECRET_KEY` | `string` | `dev-secret-key-...` | Cryptographic secret for signing tokens |
| `SYNAPSE_REQUIRE_AUTH` | `boolean` | `false` | When true, disables unauthenticated dev header fallback |
| `SYNAPSE_DB_PATH` | `string` | `data/synapse_team_memory.db` | Path to persistent SQLite storage file |
| `SYNAPSE_CORS_ORIGINS` | `csv` | `*` | Comma-separated allowed CORS origins |
| `SYNAPSE_REQUEST_TIMEOUT` | `float` | `30.0` | Global per-request timeout in seconds |
| `SYNAPSE_MAX_PAYLOAD_BYTES` | `integer` | `10485760` (10 MB) | Maximum accepted request payload size in bytes |
| `SYNAPSE_LOG_LEVEL` | `string` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `SYNAPSE_ENABLE_METRICS` | `boolean` | `true` | Enable internal latency and token metrics tracking |

### Production Validation Guardrails

When `environment == "production"`, `config.validate()` strictly enforces:
1. **Forbidden Dev Secrets:** `secret_key` must NOT match `DEFAULT_DEV_SECRET`.
2. **Entropy Minimum:** `secret_key` must be at least 32 characters in length.
3. **CORS Boundary:** Wildcard origin (`*`) is rejected when `require_auth` is enabled.
4. **Interactive Docs Disabled:** `/docs` (Swagger UI) and `/openapi.json` are suppressed in production.

---

## 3. Liveness and Readiness Probes

### 3.1 Liveness Probe (`GET /health`, `GET /api/v1/health`)
Checks if the process is up and responding to HTTP traffic.

**Response (HTTP 200 OK):**
```json
{
  "status": "ok",
  "service": "synapse-session-context-api",
  "version": "1.0.0",
  "mode": "development-only",
  "timestamp": "2026-10-15T05:22:00Z"
}
```

### 3.2 Readiness Probe (`GET /ready`, `GET /api/v1/ready`)
Verifies that downstream storage and orchestrator subsystems are connected and ready to accept queries.

- **Healthy State (HTTP 200 OK):**
```json
{
  "status": "ready",
  "service": "synapse-session-context-api",
  "timestamp": "2026-10-15T05:22:00Z",
  "checks": {
    "database": "ok (42 memories)",
    "orchestrator": "ok"
  }
}
```

- **Degraded / Not Ready State (HTTP 503 Service Unavailable):**
```json
{
  "status": "not_ready",
  "service": "synapse-session-context-api",
  "timestamp": "2026-10-15T05:22:00Z",
  "checks": {
    "database": "error: disk I/O failure",
    "orchestrator": "ok"
  }
}
```

---

## 4. Middleware & Defensive Enforcement

### 4.1 Payload Size Limiter (HTTP 413)
Requests exceeding `config.max_request_payload_bytes` are rejected immediately before parsing:
```json
{
  "envelope": {
    "protocol_version": "1.0.0",
    "request_id": "req_2026-10-15_123456",
    "timestamp": "2026-10-15T05:22:00Z"
  },
  "error": {
    "code": "PAYLOAD_TOO_LARGE",
    "message": "Request payload size (20971520 bytes) exceeds limit of 10485760 bytes.",
    "retryable": false
  }
}
```

### 4.2 Request Timeout Middleware (HTTP 504)
Requests that exceed `config.request_timeout_seconds` are cancelled and returned as:
```json
{
  "envelope": {
    "protocol_version": "1.0.0",
    "request_id": "req_2026-10-15_123456",
    "timestamp": "2026-10-15T05:22:00Z"
  },
  "error": {
    "code": "INTERNAL_ERROR",
    "message": "Request processing timed out after 30.0s.",
    "retryable": true
  }
}
```

### 4.3 Safe Configuration Diagnostics (`GET /api/v1/config`)
Inspects active service parameters without exposing sensitive cryptographic secrets:
```bash
curl -X GET http://localhost:8000/api/v1/config \
  -H "Authorization: Bearer <TOKEN>"
```
**Response (HTTP 200 OK):**
```json
{
  "status": "ok",
  "config": {
    "environment": "production",
    "host": "0.0.0.0",
    "port": 8000,
    "require_auth": true,
    "db_path": "data/synapse_team_memory.db",
    "cors_allowed_origins": ["https://ide.internal"],
    "request_timeout_seconds": 30.0,
    "max_request_payload_bytes": 10485760,
    "log_level": "INFO",
    "enable_metrics": true,
    "secret_key_configured": true,
    "secret_key_length": 44
  },
  "timestamp": "2026-10-15T05:22:00Z"
}
```

---

## 5. CLI Server Runner

The service can be launched via the CLI runner [`backend/server.py`](file:///C:/FYP/backend/server.py):

```bash
# Start with environment defaults
python -m backend.server

# Start with custom interface, port, and auto-reload
python -m backend.server --host 0.0.0.0 --port 8080 --reload
```

---

## 6. Verification & Automated Testing

All Phase 10 functionality is validated by the test suite [`tests/test_fastapi_service.py`](file:///C:/FYP/tests/test_fastapi_service.py):

| Test Category | Test Names | Status |
| :--- | :--- | :--- |
| **Config Validation** | `test_default_config_valid`, `test_invalid_port_raises`, `test_invalid_timeout_raises`, `test_invalid_payload_limit_raises`, `test_production_default_secret_forbidden`, `test_production_short_secret_forbidden`, `test_production_wildcard_cors_with_auth_forbidden`, `test_production_valid_configuration`, `test_safe_dict_redaction`, `test_load_config_from_env` | PASSED |
| **Lifespan Lifecycle** | `test_lifespan_lifecycle` | PASSED |
| **Liveness & Readiness** | `test_health_liveness_probe`, `test_readiness_probe_healthy`, `test_readiness_probe_database_error`, `test_readiness_probe_shutdown_state` | PASSED |
| **Payload Limits** | `test_payload_limit_via_content_length_header`, `test_payload_limit_via_actual_body_size` | PASSED |
| **Timeouts** | `test_request_timeout_middleware` | PASSED |
| **Error Sanitization** | `test_development_mode_exposes_internal_error_details`, `test_production_mode_masks_internal_error_details` | PASSED |
| **Diagnostics Endpoint**| `test_safe_config_endpoint` | PASSED |
| **CLI Runner** | `test_server_runner_argument_resolution` | PASSED |

**Total passing tests across the entire repository:** `330 passed in 44.51s`.
