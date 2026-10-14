# Synapse Model Context Protocol (MCP) Adapter Guide (Phase 6.3)

**Version:** `1.0.0`  
**Phase:** 6.3 — MCP Adapter  
**Status:** IMPLEMENTED & TESTED  
**Adapter Module:** [`backend/mcp_server.py`](file:///C:/FYP/backend/mcp_server.py)  
**Contract Test Suite:** [`tests/test_mcp_adapter.py`](file:///C:/FYP/tests/test_mcp_adapter.py) (9/9 passed)  
**Transport:** Stdio (standard input/output JSON-RPC)  

---

## 1. Overview & Defensive Architecture

The **Synapse MCP Adapter** allows external AI desktop applications, coding agents, and IDE assistants to discover and invoke Synapse capabilities over the standard [Model Context Protocol (MCP)](https://modelcontextprotocol.io/).

### Defensive Guardrails
1. **Narrow Tool Surface:** Strictly limited to context orchestration, memory querying, decision persistence, and session lifecycle. **No arbitrary shell execution, file system exploration, or raw database manipulation is exposed.**
2. **Untrusted Evidence Invariant:** All retrieved code and documentation passages are explicitly flagged with `untrusted_evidence_warning`. Coding agents are instructed to treat retrieved snippets as unverified reference data rather than trusted execution instructions.
3. **Automated Secret Redaction:** Prompts, queries, and decisions are sanitized through `SecretRedactor` before indexing or context packing.
4. **Development Security Disclaimer:** In Phase 6.3, authenticated identity defaults to a simulated developer principal (`mcp-client-user`). Production multi-tenant authentication is scheduled for Phase 9.

---

## 2. Exposed MCP Tool Inventory

| Tool Name | Purpose | Key Inputs | Key Outputs |
| :--- | :--- | :--- | :--- |
| `synapse_query_context` | Retrieve token-budgeted, orchestrated context via ACO | `repository_id`, `query`, `max_tokens` (default 1500), `optimization_mode` | `formatted_context`, `token_budget`, `tokens_used`, `budget_utilization_pct`, `provenance` |
| `synapse_query_memories`| Query stored architectural decisions & facts within scope | `repository_id`, `query`, `scopes` (default `["repository", "team"]`), `limit` | `total_found`, `memories` |
| `synapse_store_decision`| Persist an accepted architectural decision (ADR) | `repository_id`, `title`, `rationale`, `scope`, `impact_areas` | `decision_id`, `status` |
| `synapse_store_fact` | Store a technical recipe, best practice, or bug root cause | `repository_id`, `title`, `content`, `category`, `takeaways` | `memory_id`, `status` |
| `synapse_distill_session`| Distill a completed session into reusable knowledge cards | `repository_id`, `prompt`, `response_summary`, `decisions`, `affected_files` | `session_id`, `distilled_decisions_count`, `status` |
| `synapse_manage_session`| Create, inspect/resume, or close an engineering session | `action` ("create" / "resume" / "close"), `repository_id`, `session_id`, `title`, `summary` | Session lifecycle state |

---

## 3. Client Configuration

The Synapse MCP adapter runs as a standard Python process communicating over `stdio`.

### A. Claude Desktop Setup
Add Synapse to your `claude_desktop_config.json`:
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "synapse": {
      "command": "python",
      "args": [
        "-m",
        "backend.mcp_server"
      ],
      "cwd": "C:\\FYP"
    }
  }
}
```

### B. Cursor IDE Setup
Add Synapse to your `.cursor/mcp.json` (or Cursor Settings > Features > MCP):

```json
{
  "mcpServers": {
    "synapse": {
      "command": "python",
      "args": ["-m", "backend.mcp_server"],
      "cwd": "C:\\FYP"
    }
  }
}
```

### C. Antigravity IDE / CLI Setup
Add to Antigravity MCP config:

```json
{
  "servers": {
    "synapse": {
      "command": "python",
      "args": ["-m", "backend.mcp_server"],
      "cwd": "C:\\FYP"
    }
  }
}
```

---

## 4. Verification & Testing

To test tool discovery and programmatic execution:

```powershell
# Run the dedicated MCP adapter contract test suite:
python -m pytest tests/test_mcp_adapter.py -v
```

**Verified Test Outcomes:**
- `test_mcp_tool_discovery`: Verifies all 6 tools are discovered with typed schemas and descriptions.
- `test_mcp_query_context`: Verifies ACO context retrieval and token accounting.
- `test_mcp_query_memories`: Verifies scoped memory queries.
- `test_mcp_store_decision`: Verifies ADR storage and ID generation.
- `test_mcp_store_fact`: Verifies recipe and bug root cause persistence.
- `test_mcp_distill_session`: Verifies session distillation into memory cards.
- `test_mcp_manage_session_lifecycle`: Verifies create -> resume -> close workflow.
- `test_mcp_manage_session_invalid_action`: Verifies error handling on unknown actions.
- `test_mcp_secret_redaction`: Verifies credentials are masked over MCP.

---

## 5. IDE Integration Scope Boundary

> [!NOTE]
> Per the master execution plan, **Phase 6.3 establishes the MCP server contract and verification**. Full cross-IDE end-to-end integration with state persistence between developer sessions will be executed in **Phase 6.4 — First cross-IDE/client integration**.
