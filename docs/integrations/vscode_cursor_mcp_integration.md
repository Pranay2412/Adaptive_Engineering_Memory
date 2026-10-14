# Cross-IDE & Client Integration Guide (Phase 6.4)

**Version:** `1.0.0`  
**Phase:** 6.4 — First Cross-IDE / Client Integration  
**Status:** VERIFIED & TESTED  
**Integration Adapter:** [`backend/ide_client.py`](file:///C:/FYP/backend/ide_client.py)  
**End-to-End Test Suite:** [`tests/test_ide_e2e_integration.py`](file:///C:/FYP/tests/test_ide_e2e_integration.py) (6/6 passed)  
**Target Environments:** VS Code (Extension/MCP), Cursor IDE, Antigravity, Claude Code  

---

## 1. Overview & Verified Capabilities

Phase 6.4 validates the first end-to-end integration between external developer clients and the Synapse platform. It demonstrates that an external editor client can:
1. Establish a stable repository and session identity.
2. Query token-budgeted engineering context for active development tasks.
3. Record an approved architectural decision (ADR) during one session.
4. Close that session.
5. In a completely separate, subsequent session, execute a new context query and **successfully retrieve the persisted decision from the earlier session**.

---

## 2. Memory Capture Architecture & Invariant

> [!IMPORTANT]
> ### How Memory Capture Operates
> Memory capture in Synapse is **TOOL-INVOKED and DEVELOPER-APPROVED**.
> - **Not Automatic Background Scraping:** Synapse **does NOT** passively capture every keystroke, syntax error, or transient scratchpad edit. Uncontrolled background scraping pollutes engineering memory with high-entropy conversational fluff and syntax trials.
> - **Deliberate Knowledge Distillation:** Knowledge units enter the system when:
>   1. The developer or assistant invokes `synapse_store_decision` to record an architectural decision (ADR).
>   2. The developer or assistant invokes `synapse_store_fact` to store a recurring recipe or bug root cause.
>   3. The developer or assistant invokes `synapse_distill_session` at the end of a feature or debugging session.
> - **Approval Boundary:** Every write operation is stamped with source provenance, repository scope, and author identity.

---

## 3. End-to-End Workflow Demonstration

The following reproducible workflow was verified in [`tests/test_ide_e2e_integration.py`](file:///C:/FYP/tests/test_ide_e2e_integration.py):

```
Developer Session 1 (Morning)
  │
  ├─ 1. create_session(repo="repo-auth-service", title="JWT Architecture")
  │     └─ Received session ID: sess_morning_01
  │
  ├─ 2. Developer & Agent debate algorithm trade-offs (HS256 vs RS256).
  │
  ├─ 3. record_decision(
  │        session_id="sess_morning_01",
  │        title="ADR-002: Enforce RS256 token verification with public RSA key ring",
  │        rationale="RS256 asymmetric keys allow microservices to verify authenticity without private key distribution."
  │     )
  │     └─ Decision stored in SQLite doc store & indexed for ACO retrieval.
  │
  └─ 4. close_session(session_id="sess_morning_01", summary="Standardized on RS256.")
        └─ Session status transitioned to CLOSED.

[ ... Hours / Days Pass ... ]

Developer Session 2 (Afternoon / Subsequent Day)
  │
  ├─ 5. create_session(repo="repo-auth-service", title="Implementing Token Filter")
  │     └─ Received new distinct session ID: sess_afternoon_02
  │
  ├─ 6. query_context(
  │        session_id="sess_afternoon_02",
  │        query="What algorithm is required for JWT verification in this repository?"
  │     )
  │
  └─ 7. Context Returned by ACO:
        - Intent: debugging / explanation
        - Knapsack Packed Context includes:
          "#### Architecture Decisions & ADRs
           - Architecture Decision: ADR-002: Enforce RS256 token verification..."
        - Provenance: session_memory (confidence: 0.95)
```

---

## 4. Edge Case & Guardrail Verification

The following defensive behaviors were empirically tested and confirmed:

| Edge Case Scenario | Test Behavior | Result |
| :--- | :--- | :--- |
| **Repository Switching** | Decision recorded in `repo-auth-service` is queried from `repo-billing-service`. | **PASSED:** Private decisions from other repositories are strictly isolated and never leak. |
| **Empty / Unrelated Query** | Query contains non-existent or gibberish terms (`quantum_warp_hyperdrive_...`). | **PASSED:** Returns clean 200 OK with empty/low-scoring results rather than crashing. |
| **Duplicate Calls / Retries**| Client resends same decision with identical `idempotency_key`. | **PASSED:** Returns cached decision ID with zero duplicate rows in the database. |
| **Closed Session Mutation** | Client attempts to query or record in an already closed session. | **PASSED:** Rejected with `ErrorCode.SESSION_CLOSED` (HTTP 409). |
| **Invalid Scope Access** | Client attempts to record team-scoped memory for an unauthorized team. | **PASSED:** Rejected with `ErrorCode.INVALID_SCOPE` (HTTP 403). |

---

## 5. Client Setup Configuration

### Configuration without Hardcoded Machine Paths

For VS Code or Cursor with the Synapse extension or MCP integration:

```json
{
  "synapse.apiUrl": "http://localhost:8000/api/v1",
  "synapse.principalId": "${env:USER}",
  "synapse.defaultRepository": "${workspaceFolderBasename}",
  "synapse.maxTokenBudget": 1500,
  "synapse.autoContextInjection": false,
  "synapse.toolInvokedCapture": true
}
```

---

## 6. Limitations & Scope Boundaries

1. **Evaluated Environment:** The integration has been verified against standard Python HTTP and MCP clients (`SynapseIDEClient` and `FastMCP`). Native packaging as a packaged `.vsix` file is scheduled for Phase 14 / multi-IDE rollout.
2. **Authentication Boundary:** Principal identity is currently header-derived during local development. Cryptographic JWT verification and tenant enforcement will be implemented in **Phase 9**.
3. **Capture Scope:** Does not support automatic background keystroke capture (by design, to preserve knowledge density).
