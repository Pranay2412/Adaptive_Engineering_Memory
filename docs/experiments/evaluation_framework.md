# Synapse Repeatable Evaluation Framework Guide (Phase 13)

**Version:** `1.3.0`  
**Phase:** 13 — Evaluation Framework  
**Status:** IMPLEMENTED, BENCHMARKED & VERIFIED  
**Framework Module:** [`benchmarks/eval_framework.py`](file:///C:/FYP/benchmarks/eval_framework.py)  
**CLI Runner:** [`benchmarks/run_evaluation.py`](file:///C:/FYP/benchmarks/run_evaluation.py)  
**Canonical Dataset:** [`benchmarks/datasets/queries_v1.json`](file:///C:/FYP/benchmarks/datasets/queries_v1.json)  
**Test Suite:** [`tests/test_evaluation_framework.py`](file:///C:/FYP/tests/test_evaluation_framework.py) (9/9 passed)  

---

## 1. Overview & Purpose

Phase 13 establishes a repeatable, machine-readable, and scientific evaluation platform for Synapse. Prior ad-hoc benchmark scripts are unified into a formalized framework that provides:
1. **Machine-Readable Versioned Query Sets:** Versioned query datasets with unique IDs (`SYN-Q01` through `SYN-Q05`), task categories, expected evidence lists, reference answers, and calibrated scoring rubrics.
2. **Defensive Schema Invariants:** Strictly typed models for queries, configurations, per-trial raw records, and separate retrieval vs. answer quality metrics.
3. **Resumable Execution Harness:** Atomic checkpointing via append-only JSONL ledgers (`.checkpoint.jsonl`), permitting safe resumption of interrupted runs without duplicate executions.
4. **Zero-Omission Failure Handling:** Catches trial exceptions cleanly, executes retries, and explicitly records failures/missing items rather than silently omitting them.
5. **Deterministic Offline Evaluation:** Zero-cost, 100% reproducible execution using deterministic mock LLMs and embeddings without requiring live API keys.
6. **Strict Metric Partitioning:** Decouples retrieval performance (context tokens, evidence precision, recall, F1, latency) from generative answer quality (prompt tokens, completion tokens, correctness, completeness, relevance, sufficiency).

---

## 2. Architecture & Data Flow

```
┌──────────────────────────────────────────────────────────────────┐
│              Versioned Dataset (queries_v1.json)                 │
│   (QueryItem: query_id, category, query, expected_evidence, ...) │
└─────────────────────────────────┬────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│                   EvaluationHarness (Runner)                     │
│  ┌───────────────────────────┐    ┌───────────────────────────┐  │
│  │  GraphifyBaselineRunner   │    │     SynapseAcoRunner      │  │
│  │ (Raw BFS Graph Retrieval) │    │ (Deterministic, None, LLM)│  │
│  └─────────────┬─────────────┘    └─────────────┬─────────────┘  │
│                │                                │                │
│                ▼                                ▼                │
│       Raw Context Dump                  ACO-Budgeted Context     │
│                │                                │                │
│                └────────────────┬───────────────┘                │
│                                 ▼                                │
│                     LLM Benchmark Invoker                        │
│                 (OpenAI live or Mock LLM)                        │
│                                 │                                │
│                                 ▼                                │
│                     EvaluationJudge (Rubrics)                    │
│           (Evidence Overlap F1 + 1-5 Quality Scoring)            │
│                                 │                                │
│                                 ▼                                │
│                Atomic Checkpoint (ledger.jsonl)                  │
└─────────────────────────────────┬────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│                      EvaluationAggregator                        │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ 1. Data Integrity Validation (No Duplicates, No Drops)     │  │
│  │ 2. Retrieval Metrics Aggregation (Tokens, Precision, F1)   │  │
│  │ 3. Answer Quality Aggregation (Correctness, Completeness)  │  │
│  │ 4. Multi-Format Exporter (JSON, CSV, Markdown)             │  │
│  └────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

---

## 3. Versioned Query Dataset Specification

The canonical dataset is stored in [`benchmarks/datasets/queries_v1.json`](file:///C:/FYP/benchmarks/datasets/queries_v1.json).

### Schema: `QueryItem`
| Field | Type | Description |
| :--- | :--- | :--- |
| `query_id` | `str` | Unique machine-readable identifier (e.g. `SYN-Q01`). |
| `category` | `str` | Task intent: `structural`, `implementation`, `dependency`, `debugging`, `architecture`. |
| `query` | `str` | Full prompt text presented to the system under test. |
| `expected_evidence` | `list[str]` | Ground truth filenames, symbols, or architectural decisions required to answer correctly. |
| `reference_answer` | `str` | Canonical reference explanation used as benchmark ground truth. |
| `scoring_rubric` | `dict[str, str]` | Specific criteria mapped to integer scores 1 through 5. |
| `version` | `str` | Semantic dataset version (e.g. `1.0.0`). |

### Canonical Query Inventory
1. **`SYN-Q01` (Structural):** *"What modules import models.py?"*  
   - Expected Evidence: `backend/db_loader.py`, `backend/bridge.py`, `backend/hybrid_retrieval.py`, `backend/orchestrator.py`, `backend/session_memory.py`, `backend/team_memory.py`.
2. **`SYN-Q02` (Implementation):** *"How does authentication work with JWT?"*  
   - Expected Evidence: `RS256 asymmetric signing`, `Private key rotation`, `Bearer token header`, `JWT Header/Payload/Signature`, `Clock skew leeway (60s)`.
3. **`SYN-Q03` (Dependency):** *"Which components call the authentication service?"*  
   - Expected Evidence: `FastAPI get_current_principal`, `backend/server.py`, `backend/mcp_server.py`, `backend/session_protocol.py`.
4. **`SYN-Q04` (Debugging):** *"Where could JWT verification fail and what code is involved?"*  
   - Expected Evidence: `Clock skew without leeway`, `Token expiration (exp)`, `Signature mismatch`, `backend/security.py (TokenService)`.
5. **`SYN-Q05` (Architecture):** *"Why was RS256 chosen for authentication?"*  
   - Expected Evidence: `Asymmetric key cryptography`, `Eliminates shared symmetric secret leakage`, `Public JWKS distribution`, `mem-auth-rs256`.

---

## 4. Scoring Rubric & Evaluation Judge

The [`EvaluationJudge`](file:///C:/FYP/benchmarks/eval_framework.py) evaluates generated answers against expected evidence:

### Metric Definitions
- **Evidence Precision:** Ratio of retrieved expected evidence to total retrieved evidence items.
- **Evidence Recall:** Ratio of retrieved expected evidence items to total ground truth expected items.
- **Evidence F1:** Harmonic mean of evidence precision and evidence recall:
  $$F_1 = 2 \cdot \frac{\text{Precision} \cdot \text{Recall}}{\text{Precision} + \text{Recall}}$$
- **Quality Dimensions (1.0 to 5.0 scale):**
  - **Correctness:** Factual accuracy matching reference facts.
  - **Completeness:** Breadth of required facts covered.
  - **Relevance:** Direct adherence to query intent without evasion.
  - **Evidence Sufficiency:** Concrete citations to files, symbols, or ADRs.
  - **Overall Score:** Unweighted mean of the four dimensions.

### Evasion & Disclaimer Guardrail
If a model returns an evasive disclaimer (e.g., *"the provided context does not contain information to answer this question"*) and fails to match any ground-truth facts, it receives a penalized correctness score of `1.0`.

---

## 5. CLI Execution & Reproducibility

### A. Deterministic Offline Run (Standard Command)
Runs 3 repetitions across all 5 queries and 3 configurations (45 trials total), completely offline with zero API keys:

```powershell
python -m benchmarks.run_evaluation --mock-llm --budget 1500 --repetitions 3 --output-dir benchmarks/results/eval_v1 --prefix eval_v1
```

### B. Dry Run (Validation Mode)
Validates query dataset, schema parsing, and runner instantiations without executing LLM invocations:

```powershell
python -m benchmarks.run_evaluation --dry-run
```

### C. Live OpenAI Model Execution
Runs evaluation using live OpenAI models (requires `OPENAI_API_KEY` in environment):

```powershell
python -m benchmarks.run_evaluation --model gpt-4o-mini --budget 1500 --repetitions 3
```

### D. Resuming Interrupted Evaluations
If an evaluation run was cancelled or interrupted, use `--resume` to detect completed trials from the checkpoint ledger and continue without re-executing finished queries:

```powershell
python -m benchmarks.run_evaluation --resume --output-dir benchmarks/results/eval_v1
```

---

## 6. How to Add New Queries

1. Open [`benchmarks/datasets/queries_v1.json`](file:///C:/FYP/benchmarks/datasets/queries_v1.json).
2. Append a new query block to the `"queries"` array following the schema:
   ```json
   {
     "query_id": "SYN-Q06",
     "category": "incremental",
     "query": "How are AST changes synchronized during file edits?",
     "expected_evidence": [
       "backend/incremental_update.py",
       "IncrementalUpdateService",
       "SingleFileExtractor"
     ],
     "reference_answer": "Incremental updates parse modified source files with tree-sitter or AST...",
     "scoring_rubric": {
       "5": "Identifies IncrementalUpdateService and SingleFileExtractor with accurate change detection flow.",
       "1": "Fails to identify incremental components or returns generic answer."
     }
   }
   ```
3. Re-run `python -m benchmarks.run_evaluation --dry-run` to verify dataset integrity.

---

## 7. Exported Artifacts

Every evaluation run generates three complementary artifacts in `--output-dir`:
1. **Raw & Aggregate JSON (`<prefix>_results.json`):** Full trial records including prompt/completion tokens, latencies, evidence items, and calculated aggregates.
2. **Tabular CSV (`<prefix>_results.csv`):** Flattened trial records for analysis in pandas, R, or spreadsheet software.
3. **Publication-Ready Markdown Report (`<prefix>_report.md`):** Formatted report with configuration tables, metric breakdowns, and integrity proofs.
