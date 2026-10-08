# Team Memory Subsystem

The **Team Memory Subsystem** extends Adaptive Engineering Memory from code-and-document cross-linking into a multi-tenant, collaborative organizational brain. It captures, organizes, and retrieves **distilled, reusable engineering knowledge** rather than noisy conversational chat transcripts.

---

## 1. Core Principles

1. **Distilled Knowledge Over Raw Transcripts**:
   Raw chat logs are filled with ephemeral pleasantries, iterative syntax debugging, and conversational fluff. Team Memory models knowledge as structured engineering cards (`Memory`):
   - **`title`**: Concise descriptive headline.
   - **`content`**: Distilled rationale, root causes, or architectural patterns.
   - **`category`**: Formal engineering category.
   - **`impact_areas`**: File paths, modules, or services affected.
   - **`actionable_takeaways`**: Checklists or rules for future development.
   - **`provenance`**: Complete lineage (source session, PR, commit, symbols, files, confidence, timestamps).

2. **Multi-Tenant Scoping & Isolation**:
   - **`personal`**: Private to an individual contributor. Never visible to other teammates.
   - **`team`**: Shared across engineers belonging to a specific team or engineering pod.
   - **`repository`**: Grounded in a specific codebase, accessible to authorized contributors working within that repository.

3. **Immutable Revisions & Evolution**:
   When engineering standards or design choices evolve, older decisions are not destructively deleted. The system maintains an append-only revision log (`memory_revisions`) and establishes `SUPERSEDES` links between historical and modern practices.

4. **Dual Persistence**:
   - **Document / Relational Store (`backend/team_memory_store.py`)**: SQLite engine (with 100% PostgreSQL-compatible DDL schema) for fast, transactional, revisioned querying and multi-tenant access filtering.
   - **Neo4j Graph (`backend/db_loader.py`)**: Relational graph representation establishing links between memories, decisions, events, sessions, code symbols, documents, teams, and users.

---

## 2. Supported Engineering Categories

| Category | Typical Source | Primary Purpose |
| :--- | :--- | :--- |
| **`architecture_decision`** | ADRs, design discussions, AI session consensus | Documents choices between competing technical architectures and trade-offs. |
| **`bug_root_cause`** | Incident postmortems, debugging sessions | Captures subtle bugs, concurrency pitfalls, and regression prevention rules. |
| **`implementation_summary`** | Feature branch summaries, PR descriptions | High-level architectural walkthrough of non-trivial feature implementations. |
| **`api_decision`** | API reviews, contract migrations | Details contract modifications, breaking changes, deprecations, and envelopes. |
| **`important_refactor`** | Technical debt reduction sessions | Clarifies motivations and before/after structures of refactorings. |
| **`deployment_lesson`** | Outages, CI/CD rollouts, staging verifications | Captures environment configuration traps, database migration nuances, and rollback recipes. |
| **`recurring_solution`** | Idiom discussions, code reviews | Standard reusable patterns and practices to avoid reinventing custom logic. |
| **`general`** | Personal scratchpads, ad-hoc insights | User-scoped scratchpad thoughts or general notes. |

---

## 3. Subsystem Architecture

```
+-------------------------------------------------------------------------+
|                         AI Session / Engineer                           |
+------------------------------------+------------------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------+
|                     SessionKnowledgeDistiller                          |
|  - Filters out conversational noise & syntax trial loops                |
|  - Identifies decisions, root causes, refactors, and patterns           |
|  - Extracts affected file paths and CodeSymbol references              |
+------------------------------------+------------------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------+
|                        TeamMemoryService                                |
|  - record_architecture_decision / record_bug_root_cause                 |
|  - record_api_decision / record_deployment_lesson / ...                 |
|  - Multi-tenant scoping & retrieval filtering                           |
|  - search_as_hybrid_results (ACO Integration)                           |
+------------------+-----------------------------------+------------------+
                   |                                   |
                   v                                   v
+--------------------------------------+  +-------------------------------+
|     TeamMemoryDocumentStore          |  |       Neo4jMemoryStore        |
|  - SQLite (PostgreSQL compatible DDL)|  |  - Relational graph linking:  |
|  - memory_revisions audit trail      |  |    (:User)-[:AUTHORED]->(:Mem)|
|  - Scoping isolation queries         |  |    (:Mem)-[:SHARED_WITH]->(:T)|
|  - Keyword search & category filters |  |    (:Mem)-[:REFERENCES]->(:CS)|
+--------------------------------------+  +-------------------------------+
```

---

## 4. Multi-Tenant Scoping Rules

When querying memories via `retrieve_memories(user_id, team_id, repository_id)`:
1. **Personal Memory**:
   - `WHERE scope = 'personal' AND author_id = user_id`.
   - Strictly isolated to the individual author.
2. **Team Memory**:
   - `WHERE scope = 'team' AND (author_id = user_id OR team_id IN (user_team_ids) OR team_id IS NULL)`.
   - Shared with members of the authoring team.
3. **Repository Memory**:
   - `WHERE scope = 'repository' AND (author_id = user_id OR repository_id = target_repo_id)`.
   - Grounded in codebase context for any authorized engineer working on that repository.

---

## 5. Integration with Adaptive Context Orchestrator (ACO)

Any `Memory` can be converted into a normalized `HybridRetrievalResult` using:

```python
hybrid_result = memory.to_hybrid_result(score=0.95)
```

This ensures that team memories can be seamlessly ranked by the 8-signal `ConfigurableContextRanker` and packed by the `TokenOptimizationEngine` alongside AST code symbols and documentation entities.

---

## 6. AI Session Memory & Knowledge Distillation

The **AI Session Memory** subsystem (`backend/session_memory.py`) captures interactive AI-assisted engineering sessions and distills them into reusable, structured organizational knowledge.

### 6.1 Recording API & Schema Fields

The recording API (`AISessionMemoryService.record_session()` or `record_ai_session_api()`) captures 11 core session attributes:

| Field | Type | Description |
| :--- | :--- | :--- |
| `session_id` | `str` (Required) | Unique identifier for the AI interaction session. |
| `user_id` | `str` (Required) | Identifier of the engineer who ran the session. |
| `repository_id` | `str` (Required) | Target repository where changes or discussions occurred. |
| `prompt` | `str` (Required) | User's starting prompt or task statement. |
| `response_summary` | `str` (Required) | Concise architectural summary of the AI's response/solution. |
| `affected_files` | `list[str]` | File paths modified or inspected during the session. |
| `affected_symbols` | `list[str]` | Code symbols (classes, functions) affected or discussed. |
| `engineering_decisions`| `list[str]` | Technical decisions, trade-offs, or constraints adopted. |
| `timestamp` | `str` | ISO 8601 timestamp of session occurrence (defaults to UTC now). |
| `visibility` | `str` | Multi-tenant scope (`personal`, `team`, or `repository`). |
| `team_id` | `str | None` | Optional team affiliation for team-scoped sharing. |

### 6.2 Raw Transcript Retention Policy

By default, **full conversation data is NOT stored** (`store_raw_transcript=False` / `raw_transcript=None`). 
- Raw conversational turns, iterative prompt tweaks, and back-and-forth debugging pleasantries are analyzed **in-memory** by the summarizer.
- Only the distilled prompt, response summary, engineering decisions, impact areas, and actionable takeaways are persisted.
- Engineers can opt into storing raw messages by passing `store_raw_transcript=True`, but this remains off by default to keep the knowledge base high-signal and privacy-conscious.

### 6.3 Knowledge Summarizer (`AISessionKnowledgeSummarizer`)

The summarizer converts raw session inputs into high-value engineering knowledge cards:
1. **Category Detection**: Classifies the interaction into one of the core categories (`architecture_decision`, `bug_root_cause`, `api_decision`, `important_refactor`, `deployment_lesson`, `recurring_solution`, or `implementation_summary`).
2. **Noise Reduction**: Cleans prompt prefixes ("please fix...", "can you implement...") to generate a professional title.
3. **Structured Content**: Builds a bulleted summary detailing the context, resolution, affected files, and symbols.
4. **Actionable Takeaways**: Distills explicit rules and caveats for future developers.
5. **Engineering Decision Linking**: Creates and links first-class `EngineeringDecision` objects for formal ADR tracking.

### 6.4 Neo4j Graph Lineage

AI sessions and distilled memories establish complete traceability in Neo4j:
- `(:User)-[:INITIATED_SESSION]->(:AISession)`
- `(:AISession)-[:IN_REPOSITORY]->(:Repository)`
- `(:AISession)-[:TOUCHES_SYMBOL]->(:CodeSymbol)`
- `(:AISession)-[:TOUCHES_FILE]->(:Document)`
- `(:Memory)-[:EXTRACTED_FROM_SESSION]->(:AISession)`
- `(:Memory)-[:JUSTIFIED_BY]->(:EngineeringDecision)`

### 6.5 Consumption by the Adaptive Context Orchestrator (ACO)

When querying via ACO, session memories are retrieved, scored, and packed:

```python
from backend import orchestrate_context, AISessionMemoryService

session_service = AISessionMemoryService()

context = orchestrate_context(
    query="How does authentication work with JWT?",
    user_id="dev-charlie",
    repository_id="repo-auth",
    session_memory_service=session_service,
    max_tokens=2000,
)

# Rendered context includes the dedicated section:
# ## Engineering Decisions & AI Session Memory
# - [Architecture Decision] Sign JWT tokens with RS256: ...
```

### 6.6 Standalone JSON / REST API Handlers

The service provides lightweight dictionary-based handlers ready to mount onto FastAPI, Flask, or any HTTP router:
- `record_ai_session_api(doc_store, payload: dict)`: Ingests a new AI session and returns distilled knowledge.
- `get_ai_session_api(doc_store, session_id: str)`: Fetches an existing session record and its distilled memories.
- `query_ai_sessions_api(doc_store, user_id, repository_id, ...)`: Lists and filters sessions with multi-tenant scoping.
