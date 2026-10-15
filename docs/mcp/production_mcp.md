# Synapse Production Model Context Protocol (MCP) Guide (Phase 12)

**Version:** `1.2.0`  
**Phase:** 12 — Production MCP  
**Status:** IMPLEMENTED & VERIFIED  
**Server Module:** [`backend/mcp_server.py`](file:///C:/FYP/backend/mcp_server.py)  
**Contract Test Suite:** [`tests/test_mcp_adapter.py`](file:///C:/FYP/tests/test_mcp_adapter.py) (9/9 passed)  
**Production Test Suite:** [`tests/test_mcp_production.py`](file:///C:/FYP/tests/test_mcp_production.py) (11/11 passed)  
**Supported Transports:** `stdio` (default), `sse` (Server-Sent Events)

---

## 1. Architectural Overview & Hardening

Phase 12 transforms the initial MCP adapter into an enterprise-grade, multi-client production MCP service. It equips external AI coding assistants with secure, governed access to Synapse's Adaptive Context Orchestration (ACO), architectural decision records (ADRs), and cross-session engineering memory.

### Production Hardening Pillars

```
┌─────────────────────────────────────────────────────────────────┐
│                    External MCP Clients                         │
│  (Cursor, Claude Desktop, Antigravity, VS Code, Windsurf)       │
└────────────────────────────────┬────────────────────────────────┘
                                 │ stdio / SSE (JSON-RPC)
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│               Synapse FastMCP Production Server                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ 1. Principal Auth & Token Resolution (ApiKey / HMAC Token) │  │
│  │ 2. Strict Input Clamping & Bounds Validation              │  │
│  │ 3. RBAC & Repository Boundary Isolation Enforcement       │  │
│  │ 4. Async Execution Timeouts & Cancellation Traps          │  │
│  │ 5. Structured Audit Logging ([MCP_AUDIT], Zero Leakage)   │  │
│  └─────────────────────────────┬─────────────────────────────┘  │
└────────────────────────────────┼────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                  Session Protocol Engine                        │
│   (ACO Packing, Vector/Lexical Retrieval, Distillation)         │
└─────────────────────────────────────────────────────────────────┘
```

1. **Enterprise Identity & Multi-Tenancy:**
   - Supports raw API keys (`syn_...`) validated against cryptographic hashes (`ApiKeyService`).
   - Supports HMAC-SHA256 signed bearer tokens with tenant, team, and repository claims (`TokenService`).
   - Enforces optional or mandatory authentication via `SYNAPSE_REQUIRE_AUTH=true`.
2. **Strict Scope & RBAC Isolation:**
   - Role-based permissions prevent unauthorized actions (e.g., `viewer` role denied write access to decisions).
   - Principal repository whitelisting restricts context retrieval strictly to permitted codebases.
3. **Execution Reliability & Async Timeouts:**
   - All tool invocations are wrapped in non-blocking async execution with timeout guardrails (`SYNAPSE_MCP_TIMEOUT_SECONDS`, default 30s).
   - Graceful handling of client cancellations (`asyncio.CancelledError`).
4. **Idempotency & Conflict Guardrails:**
   - Transparently handles duplicate submissions with matching idempotency keys, replaying cached outcomes.
   - Detects conflicting payloads reusing the same idempotency key, preventing silent overwrites (`IDEMPOTENCY_CONFLICT`).
5. **Privacy-Preserving Audit Logging:**
   - Structured audit lines (`[MCP_AUDIT]`) log tool invocation metadata, tenant IDs, repository scopes, and latencies without ever leaking sensitive prompts, proprietary code, or credentials.
6. **Untrusted Evidence Invariant:**
   - Context packings explicitly carry `untrusted_evidence_warning`, reminding coding agents to treat indexed snippets as untrusted reference context rather than executable instructions.

---

## 2. Complete Tool Inventory (7 Tools)

| Tool Name | Purpose | Key Inputs | Guardrails & Clamping |
| :--- | :--- | :--- | :--- |
| `synapse_query_context` | Retrieve token-budgeted, orchestrated context via ACO | `repository_id`, `query`, `max_tokens` (100–32,000), `optimization_mode` | Clamped token budgets; secret redaction; repository permission verification |
| `synapse_query_memories`| Query stored architectural decisions & facts within scope | `repository_id`, `query`, `scopes` (`repository`, `team`, `organization`), `limit` (1–100) | Scope validation; repository boundary checks |
| `synapse_store_decision`| Persist an accepted architectural decision (ADR) | `repository_id`, `title`, `rationale`, `scope`, `category`, `status`, `idempotency_key` | RBAC (`DECISION_WRITE`); idempotent replay and conflict detection |
| `synapse_store_fact` | Store a technical recipe, best practice, or bug root cause | `repository_id`, `title`, `content`, `category`, `scope`, `idempotency_key` | RBAC (`MEMORY_WRITE`); ephemeral session binding |
| `synapse_distill_session`| Distill a completed session into reusable knowledge cards | `repository_id`, `session_id`, `prompt`, `response_summary`, `decisions`, `affected_files` | RBAC (`SESSION_MANAGE`); secret sanitization |
| `synapse_manage_session`| Manage session lifecycle (create, resume, close) | `action` (`create`, `resume`, `close`), `repository_id`, `session_id`, `title`, `summary` | Role validation; tenant and repository scope boundary enforcement |
| `synapse_get_server_status` | Inspect server health, protocol version, and verified principal | `api_key` (optional), `auth_token` (optional) | Identity resolution; active role and permission discovery |

---

## 3. Client Configuration

### A. Cursor IDE
Edit `.cursor/mcp.json` or configure in Cursor Settings > Features > MCP:

```json
{
  "mcpServers": {
    "synapse": {
      "command": "python",
      "args": ["-m", "backend.mcp_server"],
      "cwd": "C:\\FYP",
      "env": {
        "SYNAPSE_API_KEY": "syn_live_your_api_key_here",
        "SYNAPSE_MCP_TIMEOUT_SECONDS": "30.0"
      }
    }
  }
}
```

### B. Claude Desktop
Edit `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "synapse": {
      "command": "python",
      "args": ["-m", "backend.mcp_server"],
      "cwd": "C:\\FYP",
      "env": {
        "SYNAPSE_API_KEY": "syn_live_your_api_key_here"
      }
    }
  }
}
```

### C. Antigravity IDE & CLI
Configure in Antigravity settings or MCP configuration:

```json
{
  "servers": {
    "synapse": {
      "command": "python",
      "args": ["-m", "backend.mcp_server"],
      "cwd": "C:\\FYP",
      "env": {
        "SYNAPSE_API_KEY": "syn_live_your_api_key_here"
      }
    }
  }
}
```

### D. VS Code (Cline / Roo Code / Generic MCP Extensions)
In VS Code MCP extension settings:

```json
{
  "mcpServers": {
    "synapse": {
      "command": "python",
      "args": ["-m", "backend.mcp_server"],
      "cwd": "C:\\FYP",
      "env": {
        "SYNAPSE_API_KEY": "syn_live_your_api_key_here"
      }
    }
  }
}
```

---

## 4. Transports & Server Runner

The server CLI accepts transport and network parameters:

### Stdio Transport (Default)
Standard communication via process stdin/stdout, ideal for local desktop extensions:
```powershell
python -m backend.mcp_server --transport stdio
```

### Server-Sent Events (SSE) Transport
Network-accessible HTTP/SSE transport for containerized, remote, or multi-agent environments:
```powershell
python -m backend.mcp_server --transport sse --port 8001
```

---

## 5. Environment Variables & Security Settings

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `SYNAPSE_REQUIRE_AUTH` | `false` | When `true`, rejects unauthenticated MCP requests with `AUTHENTICATION_REQUIRED`. |
| `SYNAPSE_API_KEY` | None | Default API key injected into environment by local MCP client configurations. |
| `SYNAPSE_SECRET_KEY` | Built-in Dev Secret | Cryptographic signing secret for HMAC-SHA256 bearer tokens. |
| `SYNAPSE_MCP_TIMEOUT_SECONDS` | `30.0` | Execution timeout ceiling for individual tool invocations. |
| `SYNAPSE_MCP_DEFAULT_TOKEN_BUDGET` | `1500` | Default token budget for context queries when unspecified. |
| `SYNAPSE_MCP_MAX_TOKEN_BUDGET` | `32000` | Upper bound clamp for `max_tokens` argument. |

---

## 6. Verification & Automated Testing

Verify the production MCP implementation using pytest:

```powershell
# Run the complete MCP test suite (20 tests):
python -m pytest tests/test_mcp_adapter.py tests/test_mcp_production.py -v

# Run the full platform test suite:
python -m pytest
```
