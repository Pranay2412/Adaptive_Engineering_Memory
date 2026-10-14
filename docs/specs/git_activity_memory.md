# Specification: Git & Activity Memory Subsystem (Phase 7)

**Document Version:** 1.0.0  
**Status:** Implemented & Verified  
**Date:** 2026-10-14  
**Component:** `backend.git_memory`, `backend.team_memory_store`

---

## 1. Executive Summary & Purpose

The **Git & Activity Memory Subsystem** bridges the gap between active codebases, developer architectural decisions, and historical version-control activity. Rather than treating Git history as an unindexed log or performing expensive ad-hoc shell calls during retrieval, Synapse ingests and models Git activity into structured, queryable relational entities linked directly to code symbols, architectural decision records (ADRs), and documentation entities.

### Key Capabilities
1. **Traceable Commit Ingestion:** Structured extraction of commit SHA, author/committer identities, ISO timestamps, commit messages, parent SHAs, and file modification statuses (`added`, `modified`, `deleted`, `renamed`).
2. **Selective Diff Summaries:** Bounded snippet extraction capturing the core changes of modified files without overwhelming token budgets (`MAX_FILE_DIFF_CHARS = 512`, `MAX_SELECTIVE_DIFF_CHARS = 2048`).
3. **Multi-Parent Merge Topology:** Detection of merge commits (`len(parent_shas) >= 2`), branch merge points, and suppression of diff storms on merge commits.
4. **Rename & Deletion Tracking:** Full recognition of Git rename semantics (`R100 old_path new_path`), preserving file lineage across refactors.
5. **Entity & Decision Linking:** High-confidence automatic linking between commits and code symbols (`CodeSymbol`), documents (`DocumentEntity`), and architectural decisions (`EngineeringDecision`).
6. **Adaptive Context Orchestrator (ACO) Retrieval:** First-class integration producing `HybridRetrievalResult` objects with complete provenance (`source_type="git_commit"`, `channels=["git_history", "lexical"]`) for budget-aware context packing.

---

## 2. Architecture & Components

```
                                    +-----------------------+
                                    | Git Repository (.git) |
                                    +-----------+-----------+
                                                |
                                                | git log (delimited: \x1e, \x1f)
                                                v
+------------------------+          +-----------+-----------+
| Code Symbols / ADRs    +--------->|  GitMemoryExtractor   |
+------------------------+          +-----------+-----------+
            |                                   |
            | link_commits()                    | extract_commits()
            v                                   v
+-----------+------------+          +-----------+-----------+
|    GitCommitLinker     +--------->|       GitCommit       |
+-----------+------------+          +-----------+-----------+
            |                                   |
            | links                             | commits
            +-------------------+---------------+
                                |
                                v
                    +-----------+-----------+
                    |   GitMemoryService    |
                    +-----------+-----------+
                                |
             +------------------+------------------+
             |                                     |
             v                                     v
+------------+-------------+          +------------+-------------+
| TeamMemoryDocumentStore  |          | Adaptive Context Orchestr.|
| (git_commits, links)     |          | (retrieve_for_aco)        |
+--------------------------+          +---------------------------+
```

### 2.1 `GitMemoryExtractor`
- Executes safe `git log` commands using ASCII control delimiters:
  - `\x1e` (Record Separator): Precedes each commit record.
  - `\x1f` (Unit Separator): Separates metadata fields.
  - `--name-status -M`: Appended directly after the trailing field delimiter for deterministic file status parsing.
- Handles edge cases:
  - Non-git directories: detected via `is_git_repository()`, returns `[]` gracefully without exceptions.
  - Empty repositories: returns `[]` cleanly.
  - Detached HEAD & custom branches: supports targeted branch parsing via the `branch` parameter.

### 2.2 `GitCommitLinker`
Derives high-confidence links between commits and engineering artifacts:
- **Code Symbols:**
  - Files modified matching a symbol's `file`/`source_file` path.
  - Boosted to `confidence = 0.95` if the symbol name is explicitly mentioned in the commit message; `confidence = 0.75` for co-located files.
- **Engineering Decisions (ADRs):**
  - Regex detection matching references such as `ADR-042`, `DEC-10`, `RFC-007` (`confidence = 0.95`).
  - Multi-term title matching across significant vocabulary words (`confidence = 0.85`).

### 2.3 `GitMemoryService`
- **Idempotent Ingestion:** Inserts or updates commits and links via `ON CONFLICT(commit_sha, repository_id) DO UPDATE`.
- **ACO Historical Retrieval:** Ranked lexical and recency scoring matching query terms against commit summaries, full messages, authors, and affected file paths.
- **Repository Scoping:** Guarantees strict isolation by filtering on `repository_id = ?`.

---

## 3. Relational Schema Specification

### 3.1 `git_commits` Table

```sql
CREATE TABLE IF NOT EXISTS git_commits (
    commit_sha TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    short_sha TEXT NOT NULL,
    author_name TEXT,
    author_email TEXT,
    authored_at TEXT NOT NULL,
    committer_name TEXT,
    committer_email TEXT,
    committed_at TEXT NOT NULL,
    message TEXT,
    summary TEXT,
    parent_shas TEXT,                     -- JSON list: ["sha1", "sha2"]
    is_merge INTEGER DEFAULT 0,          -- 1 if multi-parent merge
    branch TEXT,
    files_changed TEXT,                   -- JSON list: ["path1", "path2"]
    change_summary TEXT,                  -- JSON dict: {"path1": "added", "path2": "modified"}
    selective_diff TEXT,                  -- JSON dict: {"path1": "truncated diff snippet"}
    metadata TEXT,                        -- JSON dict
    created_at TEXT NOT NULL,
    PRIMARY KEY (commit_sha, repository_id)
);

CREATE INDEX IF NOT EXISTS idx_git_commits_repo ON git_commits(repository_id);
CREATE INDEX IF NOT EXISTS idx_git_commits_committed ON git_commits(committed_at);
```

### 3.2 `git_commit_links` Table

```sql
CREATE TABLE IF NOT EXISTS git_commit_links (
    commit_sha TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    target_type TEXT NOT NULL,            -- "code_symbol" | "document_entity" | "decision" | "file"
    target_id TEXT NOT NULL,              -- Target entity identifier
    confidence REAL DEFAULT 1.0,         -- Confidence score (0.0 - 1.0)
    reason TEXT,                          -- Human-readable provenance justification
    created_at TEXT NOT NULL,
    PRIMARY KEY (commit_sha, repository_id, target_type, target_id)
);

CREATE INDEX IF NOT EXISTS idx_git_commit_links_target ON git_commit_links(target_type, target_id);
```

---

## 4. Selective Diff & Token Budget Safeguards

Uncontrolled diff extraction can easily exhaust context windows and storage databases. Synapse enforces two hard boundaries during diff extraction:

| Boundary | Limit | Behavior on Overflow |
| :--- | :--- | :--- |
| `MAX_FILE_DIFF_CHARS` | 512 characters | Truncates at limit and appends `\n... [diff truncated]` |
| `MAX_SELECTIVE_DIFF_CHARS` | 2,048 characters | Halts diff extraction for subsequent files in the commit |
| Merge Commit Guardrail | `is_merge == True` | Selective diff extraction is bypassed entirely to prevent merge diff explosions |
| Target File Limit | 6 files max | Diffs extracted only for the first 6 modified files |

---

## 5. Adaptive Context Orchestrator (ACO) Integration

When an engineering query (e.g., *"Why did we introduce Redis tiered caching?"* or *"Who changed the transaction rollback logic?"*) is processed:

1. `GitMemoryService.retrieve_for_aco(query, repository_id, limit)` scans historical commits for the repository.
2. It ranks candidate commits via term overlap and recency:
   $$\text{score} = 0.50 + 0.50 \times \left( \frac{|\text{matched\_terms}|}{|\text{query\_terms}|} \right)$$
3. Produces a standard `HybridRetrievalResult`:
   - `entity`: `Git Commit <short_sha>: <summary>`
   - `type`: `"GitCommit"`
   - `source`: `"git_history"`
   - `content_snippet`: Commit message and bounded diff snippets
   - `provenance`: `ResultProvenance(source_type="git_commit", channels=["git_history", "lexical"], ...)`
4. The ACO context packer places the result into the prompt context according to its ranking weight and available token budget.

---

## 6. Verification & Test Evidence

The subsystem is validated by test suite `tests/test_git_memory.py`:

| Test Name | Focus | Status |
| :--- | :--- | :--- |
| `test_git_commit_and_link_serialization` | `to_dict()`, `from_dict()`, and `to_hybrid_result()` round-trip | Passed |
| `test_non_git_directory_fallback` | Graceful non-throwing fallback on non-git paths | Passed |
| `test_empty_git_repository` | Safe handling of newly initialized repos with 0 commits | Passed |
| `test_commit_extraction_add_modify_delete` | Correct parsing of additions, modifications, and deletions | Passed |
| `test_file_rename_detection` | Recognition of `R100 old_path new_path` rename semantics | Passed |
| `test_multi_parent_merge_commit` | `is_merge=True` and multi-parent list tracking on git merge | Passed |
| `test_selective_diff_truncation` | Strict enforcement of `MAX_FILE_DIFF_CHARS` bounds | Passed |
| `test_git_commit_linker_symbols_and_decisions` | Automatic high-confidence symbol and ADR linking | Passed |
| `test_git_memory_service_ingestion_and_idempotency` | Conflict-free re-ingestion and store persistence | Passed |
| `test_aco_historical_retrieval` | Scoped retrieval, term matching, and score thresholding | Passed |

**Overall Test Suite Status:**
- `tests/test_git_memory.py`: **10 / 10 passed**
- Full Repository Suite: **275 / 275 passed** (0 failures, 0 regressions)
