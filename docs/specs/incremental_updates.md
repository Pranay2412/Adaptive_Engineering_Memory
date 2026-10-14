# Specification: Incremental Updates Subsystem (Phase 8)

**Document Version:** 1.0.0  
**Status:** Implemented & Verified  
**Date:** 2026-10-14  
**Component:** `backend.incremental_update`, `backend.hybrid_retrieval`, `backend.team_memory_store`, `backend.db_loader`

---

## 1. Executive Summary & Objectives

In real-world software engineering, codebases evolve continuously through iterative commits, refactors, renames, and file deletions. A naive memory platform that performs a full re-parsing and re-embedding of the entire repository on every change is computationally wasteful, slow, and expensive.

The **Incremental Updates Subsystem** enables Synapse to surgically detect and process changes without re-indexing untouched files, while guaranteeing that the resulting graph and semantic state is strictly identical to a clean full rebuild.

### Core Objectives
1. **Change Detection:** Accurately classify file additions, modifications, deletions, and content-preserving renames (`IncrementalChangeDetector`).
2. **Surgical Extraction:** Extract symbols and documentation concepts only from affected files (`SingleFileExtractor`).
3. **Graph & Semantic Synchronization:** Invalidate stale records for deleted/modified files and update indexes without complete rebuilds.
4. **Idempotency & Checkpoints:** Persist atomic checkpoints (`RepositoryCheckpoint`) in SQLite/PostgreSQL, ensuring safe retries and no-op re-runs.
5. **Full Rebuild Retention & Strict Equivalence:** Retain full rebuild capabilities and verify that incremental updates produce state identical to a clean rebuild.

---

## 2. Architecture & Pipeline Flow

```
+-------------------------------------------------------------+
|                     Repository Worktree                     |
+------------------------------+------------------------------+
                               |
                               | scan_repository_hashes()
                               v
               +---------------+---------------+
               |   IncrementalChangeDetector   |
               +---------------+---------------+
                               |
                               | compare with previous checkpoint
                               v
            +------------------+------------------+
            | FileChangeRecords:                  |
            | [added, modified, deleted, renamed] |
            +------------------+------------------+
                               |
                               v
               +---------------+---------------+
               |   IncrementalUpdateService    |
               +---------------+---------------+
                               |
         +---------------------+---------------------+
         |                                           |
         | (1) For deleted/modified:                 | (2) For added/modified/renamed:
         |     invalidate stale records              |     SingleFileExtractor.extract_file()
         v                                           v
+--------+---------------------+           +---------+---------------------+
| Graph & Semantic Invalidation|           | Incremental Re-extraction     |
| - Neo4j / DocumentStore      |           | - Classes, functions, methods |
| - SemanticKnowledgeIndex     |           | - Concepts, sections, edges   |
+--------+---------------------+           +---------+---------------------+
         |                                           |
         +---------------------+---------------------+
                               |
                               | (3) Upsert newly extracted items
                               v
               +---------------+---------------+
               |    SemanticKnowledgeIndex     |
               |    Neo4j / DocumentStore      |
               +---------------+---------------+
                               |
                               | (4) Atomic Checkpoint Creation
                               v
               +---------------+---------------+
               |      RepositoryCheckpoint     |
               | (incremental_checkpoints tbl) |
               +-------------------------------+
```

---

## 3. Data Model & Storage Schemas

### 3.1 `RepositoryCheckpoint` Dataclass

```python
@dataclass
class RepositoryCheckpoint:
    id: str                                  # Unique checkpoint ID: cp_<repo_id>_<timestamp>
    repository_id: str                      # Scoped repository identifier
    user_id: str                            # Active user / principal
    commit_sha: str | None = None           # Head Git commit SHA (if available)
    timestamp: str                          # ISO 8601 timestamp
    file_hashes: dict[str, str]             # rel_path -> sha256 hash
    symbols_by_file: dict[str, list[str]]   # rel_path -> list of symbol IDs
    entities_by_file: dict[str, list[str]]  # rel_path -> list of entity IDs
    edges_by_file: dict[str, list[dict]]    # rel_path -> serialized edges
    symbol_count: int = 0
    entity_count: int = 0
    edge_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
```

### 3.2 Relational Checkpoint Table (`incremental_checkpoints`)

```sql
CREATE TABLE IF NOT EXISTS incremental_checkpoints (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    commit_sha TEXT,
    timestamp TEXT NOT NULL,
    file_hashes TEXT NOT NULL,              -- JSON dict: rel_path -> sha256
    symbols_by_file TEXT,                   -- JSON dict: rel_path -> [symbol_ids]
    entities_by_file TEXT,                  -- JSON dict: rel_path -> [entity_ids]
    edges_by_file TEXT,                     -- JSON dict: rel_path -> [edges]
    symbol_count INTEGER DEFAULT 0,
    entity_count INTEGER DEFAULT 0,
    edge_count INTEGER DEFAULT 0,
    metadata TEXT,                          -- JSON dict
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_repo ON incremental_checkpoints(repository_id);
```

---

## 4. Change Detection & Rename Heuristics

The `IncrementalChangeDetector` operates on SHA-256 content hashes of all tracked files (`.py`, `.md`), excluding build caches and virtual environments (`.git`, `__pycache__`, `.venv`, `node_modules`):

1. **Initial Baseline:** When no prior checkpoint exists, all files are classified as `added` and a full rebuild is performed.
2. **Path Unchanged, Hash Matches:** File is marked `unchanged` and skipped.
3. **Path Unchanged, Hash Differs:** File is marked `modified`.
4. **Path Added, Matching Old Hash:** If a newly appeared path has an identical SHA-256 hash to a path that disappeared, it is recognized as `renamed` with `old_path`.
5. **Path Added, No Match:** File is marked `added`.
6. **Path Missing, No Match:** File is marked `deleted`.

---

## 5. Incremental Semantic Synchronization

Stale semantic vectors can pollute candidate retrieval if not purged when code changes. `SemanticKnowledgeIndex` supports 4 surgical operations:

1. `remove_symbols_by_file(file_path: str) -> int`: Filters out symbols and corresponding dense vectors originating from `file_path`.
2. `remove_entities_by_path(document_path: str) -> int`: Filters out documentation concepts and corresponding vectors for `document_path`.
3. `upsert_symbols(symbols: list[CodeSymbol]) -> int`: Replaces symbols matching existing IDs and batch-embeds newly added symbols without re-embedding untouched files.
4. `upsert_entities(entities: list[DocumentEntity]) -> int`: Incrementally embeds new or updated documentation concepts.

---

## 6. Strict Equivalence Verification

To satisfy the acceptance requirement (*"compare incremental state with clean rebuild. Done when tested results agree"*), `IncrementalUpdateService.verify_incremental_consistency()` compares the incremental state against a fresh clean rebuild on identical disk files:

$$\text{Agrees} \iff \begin{cases}
\mathcal{H}_{\text{incremental}} = \mathcal{H}_{\text{clean}} & \text{(File Hashes)} \\
\mathcal{S}_{\text{incremental}} = \mathcal{S}_{\text{clean}} & \text{(Code Symbol IDs)} \\
\mathcal{E}_{\text{incremental}} = \mathcal{E}_{\text{clean}} & \text{(Document Entity IDs)}
\end{cases}$$

If any symbol or entity is missing or orphaned, the verification fails and outputs the exact discrepancies.

---

## 7. Verification & Test Evidence

Validated by the test suite [`tests/test_incremental_update.py`](file:///C:/FYP/tests/test_incremental_update.py):

| Test Case | Verifies | Status |
| :--- | :--- | :--- |
| `test_change_detector_add_modify_delete_rename` | Detection of additions, edits, deletions, and content renames | **Passed** |
| `test_single_file_extractor_python` | AST extraction of classes, methods, functions, docstrings, line ranges | **Passed** |
| `test_single_file_extractor_markdown` | Extraction of concept headings and documentation entities | **Passed** |
| `test_checkpoint_persistence_sqlite` | Atomic serialization and round-trip SQLite storage | **Passed** |
| `test_incremental_service_initial_and_idempotent` | Baseline full rebuild followed by clean idempotent no-op | **Passed** |
| `test_incremental_service_surgical_modification` | Modifying 1 file re-parses only that file and updates semantic index | **Passed** |
| `test_incremental_service_deletion_and_rename` | Purging deleted paths and re-indexing under renamed targets | **Passed** |
| `test_incremental_vs_clean_rebuild_strict_consistency` | **Strict mathematical agreement between incremental update and clean full rebuild** | **Passed** |

**Summary Results:**
- `tests/test_incremental_update.py`: **8 / 8 passed** in 3.06s
- Full Suite: **283 / 283 passed** in 22.63s (0 failures, 0 regressions)
