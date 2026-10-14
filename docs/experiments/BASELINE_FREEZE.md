# Synapse Baseline Freeze Declaration (v1.0.0)

**Date:** October 14, 2026  
**Document Status:** FROZEN / IMMUTABLE  
**Baseline Identifier:** `baseline-v1.0.0`  
**Git Working Tree State:** Commit `7e3d98c` + Baseline Audits & Harnesses  
**Repository:** `https://github.com/Pranay2412/Adaptive_Engineering_Memory`  
**Handoff Stage:** Prompt 3 Complete -> Awaiting Authorization for Phase 6.1  

---

## 1. Executive Summary & Purpose

This document formally freezes the **Synapse Evaluation Baseline v1.0.0**. 

Following the completion of **Prompt 0 (Repository Audit)**, **Prompt 1 (Corrected TC2 Budget Benchmark)**, and **Prompt 2 (Final Baseline Evaluation)**, the core retrieval, ranking, token optimization, and context packing mechanisms have been rigorously audited, corrected, and verified with deterministic token measurement and empirical LLM evaluations.

As mandated by Section A5 ("Prompt 3 — Freeze baseline") of `Synapse_Agent_Handoff_Full.md`:
1. **The baseline code, algorithms, datasets, rubrics, and results are now frozen.**
2. **No new product features or structural modifications may be introduced into the baseline evaluation set.**
3. **Future development (starting with Phase 6.1 Provider-neutral session protocol) must treat these baseline results as an immutable reference.**
4. **Any future modification to retrieval algorithms, ranking heuristics, tokenizers, knapsack packing, test queries, or scoring rubrics invalidates direct numerical comparison with v1.0.0 and requires issuing a new baseline version (`baseline-v2.0.0`).**

---

## 2. Frozen Environment & Runtime Specifications

All baseline benchmarks and unit tests were executed under the following verified runtime environment:

| Property | Value | Notes |
| :--- | :--- | :--- |
| **Operating System** | Windows 11 Pro AMD64 (10.0.26100) | Local development workstation |
| **Python Runtime** | Python 3.14.3 | Default interpreter (`C:\Python314\python.exe`) |
| **Unit Test Suite** | Pytest 9.0.3, Pluggy 1.6.0 | 205 passed in 7.26s |
| **Tokenizer Library** | `tiktoken` 0.12.0 | Exact OpenAI BPE token counting |
| **BPE Encoding Scheme** | `cl100k_base` | Matches `gpt-4o`, `gpt-4o-mini`, `gpt-3.5-turbo` |
| **Evaluation Model** | OpenAI `gpt-4o-mini` (2024-07-18) | Invoked via direct HTTPS API (`temperature=0.0`) |
| **Target Codebase** | `C:\FYP` (Synapse Repository) | 16 backend source files, ~100k characters |
| **Embedding Subsystem**| `MockEmbeddingProvider` (deterministic hash) | Standardized in repo; avoids non-deterministic external calls |

---

## 3. Artifact Registry & Immutability Classification

The benchmark suite maintains strict provenance separation between early preliminary experiments (which contained budget over-allocation artifacts) and the authoritative corrected baseline experiments.

### A. Authoritative Baseline v1.0.0 Artifacts (IMMUTABLE)

These files represent the verified, scientifically sound baseline. They must not be modified or overwritten:

| Artifact Path | Format | Records / Size | Description |
| :--- | :--- | :--- | :--- |
| [`docs/REPOSITORY_AUDIT.md`](file:///C:/FYP/docs/REPOSITORY_AUDIT.md) | Markdown | 467 lines | Prompt 0 architectural audit, 16 backend file audits, full trace. |
| [`tests/test_tc2_budget_enforcement.py`](file:///C:/FYP/tests/test_tc2_budget_enforcement.py) | Python | 14 test cases | Regression suite verifying zero budget overflow under all budgets. |
| [`benchmarks/results/final_baseline_tc1_results.json`](file:///C:/FYP/benchmarks/results/final_baseline_tc1_results.json) | JSON | 15 trials | TC1 Comparative benchmark (5 queries x 3 timing repetitions). |
| [`benchmarks/results/final_baseline_tc1_results.csv`](file:///C:/FYP/benchmarks/results/final_baseline_tc1_results.csv) | CSV | 15 rows | Tabular raw metrics for TC1 comparative evaluation. |
| [`benchmarks/results/corrected_budget_benchmark_results.json`](file:///C:/FYP/benchmarks/results/corrected_budget_benchmark_results.json) | JSON | 75 trials | TC2 Multi-budget evaluation (6 budgets x 3 optimization modes). |
| [`benchmarks/results/corrected_budget_benchmark_results.csv`](file:///C:/FYP/benchmarks/results/corrected_budget_benchmark_results.csv) | CSV | 75 rows | Tabular raw metrics for TC2 budget sweep. |
| [`docs/experiments/test_case_1_final_baseline_tc1.md`](file:///C:/FYP/docs/experiments/test_case_1_final_baseline_tc1.md) | Markdown | Comprehensive report | Detailed analysis of Graphify vs. Synapse comparative evaluation. |
| [`docs/experiments/test_case_2_corrected_budget_benchmark.md`](file:///C:/FYP/docs/experiments/test_case_2_corrected_budget_benchmark.md) | Markdown | Comprehensive report | Detailed analysis of ACO token budgeting, knapsack packing, and quality. |
| [`docs/experiments/final_baseline_evaluation_report.md`](file:///C:/FYP/docs/experiments/final_baseline_evaluation_report.md) | Markdown | Consolidated report | Cross-cutting synthesis of TC1 and TC2 findings for publication/handoff. |

### B. Preserved Preliminary Artifacts (HISTORICAL / READ-ONLY)

These files are preserved for provenance and historical record. They must never be deleted:

| Artifact Path | Description | Known Historical Limitations |
| :--- | :--- | :--- |
| `benchmarks/results/benchmark_results.json` / `.csv` | Preliminary TC1 benchmark | Single-run timing, missing provider usage distinction. |
| `benchmarks/results/budget_benchmark_results.json` / `.csv` | Preliminary TC2 benchmark | 114% over-budget artifact caused by whitespace/separator omission. |
| `docs/experiments/test_case_1_token_benchmark.md` | Preliminary TC1 report | Pre-Prompt 0 comparative findings. |
| `docs/experiments/test_case_2_budget_benchmark.md` | Preliminary TC2 report | Documents initial budget violation discovery. |

---

## 4. Authoritative Baseline v1.0.0 Findings

### A. Test Case 1: Graphify Code-Resolved vs. Synapse Complete
*Evaluated across 5 canonical software engineering queries with 3 timing repetitions (15 total runs).*

1. **Context Token Reduction:** Synapse packs an average of **834.0 context tokens** vs. Graphify's **1,468.8 context tokens** (**43.22% reduction**; saving an average of 634.8 tokens per request).
2. **Total Prompt/Input Reduction:** Synapse requires **875.4 input tokens** vs. Graphify's **1,503.0 input tokens** (**41.73% reduction**; saving 627.6 tokens per prompt).
3. **Response Quality Improvement:** Synapse achieved an average calibrated quality score of **4.20 / 5.0** vs. Graphify's **1.30 / 5.0** (**+223% relative improvement**).
   - On architectural queries (`Why was RS256 chosen for authentication?`), Graphify scored **1.00** (total hallucination/refusal due to zero ADR awareness) while Synapse scored **4.33** (grounded in `ADR-001`).
   - On debugging queries (`Where could JWT verification fail?`), Graphify scored **1.33** while Synapse scored **4.33** (grounded in session bug history).
4. **Latency Trade-Off (Empirical Disclosure):**
   - Synapse end-to-end latency averaged **2.457s** vs. Graphify's **1.487s** (Synapse is **+0.970s / +65.2% slower**).
   - The extra latency stems from multi-source hybrid retrieval (Graphify AST + Cognee semantic + Session history + Team ADRs) followed by cross-source deduplication, heuristic ranking, and exact BPE knapsack packing.

### B. Test Case 2: ACO Token Budget Scaling & Invariant Enforcement
*Evaluated across 6 budget tiers (500, 1000, 1500, 2500, 4000, unbounded) and 3 optimization modes (75 total trials).*

1. **Mathematical Budget Invariant:** Across all 75 trials, **0 trials exceeded their configured budget** ($Utilization \le 100.0\%$).
   - Max observed utilization: **99.8%** at the 500-token budget.
   - Mean utilization at 500 budget: **93.8%**.
2. **Context Saturation Limit:**
   - In this repository, total relevant context saturates at approximately **1,191 tokens**.
   - Increasing the budget from 1,500 to 4,000 tokens yielded **0 additional context items** and **0 quality gain** (quality flatlined at 4.22 / 5.0), while budget utilization dropped to 29.8%.
3. **Optimal Operating Point (The Sweet Spot):**
   - **Budget = 1,500 tokens** is the empirical optimum:
     - Context tokens: 953
     - Budget utilization: 63.5%
     - ADR retention: 100%
     - Architecture documentation retention: 80%
     - Answer quality: 4.22 / 5.0
     - Total latency: 1.95s

---

## 5. Explicit Limitations & Scope Boundaries

To maintain scientific integrity, the following limitations are recorded:

1. **Synthetic / Mock Embedding Provider:**
   The current environment utilizes `MockEmbeddingProvider` (deterministic hash-based vectors) in `backend/semantic_matcher.py`. While structural Graphify retrieval, ADR parsing, session retrieval, and knapsack packing operate identically to production, dense semantic search was evaluated structurally rather than through a live third-party vector database (e.g., Pinecone or Qdrant).
2. **Repository Size & Domain:**
   Evaluation was performed on `C:\FYP` (Synapse codebase itself, 16 backend files). Scaling behaviors on multi-million line enterprise monoliths may require pagination or tiered indexing.
3. **Query Sample Size:**
   The canonical query set comprises 5 carefully designed queries covering the five primary engineering modalities:
   - Q1: Structural (`What modules import models.py?`)
   - Q2: Implementation (`How does authentication work with JWT?`)
   - Q3: Dependency (`Which components call the authentication service?`)
   - Q4: Debugging (`Where could JWT verification fail and what code is involved?`)
   - Q5: Architecture (`Why was RS256 chosen for authentication?`)
   Statistical significance over larger distributions (e.g., SWE-bench) should be evaluated in Phase 13.
4. **LLM Judge Inherent Variance:**
   Quality scoring was evaluated using `gpt-4o-mini` with strict `temperature=0.0`, calibrated rubric criteria, and JSON schema enforcement. While variance across identical prompt runs was $< 0.15$ points, human peer evaluation remains the gold standard.

---

## 6. Baseline Change Policy (The Versioning Invariant)

Any future work in Phase 6 through Phase 22 must adhere to the following governance rule:

> **BASELINE IMMUTABILITY RULE:**  
> The algorithms in `backend/token_optimizer.py` (specifically `TokenOptimizedContextPacker` and `estimate_tokens`), `backend/orchestrator.py` (`AdaptiveContextOrchestrator`), and the scoring criteria in `benchmarks/budget_benchmark.py` are locked.  
> If an upcoming phase requires changing retrieval weights, introducing a new tokenizer, modifying the knapsack packing algorithm, altering the 5 canonical queries, or updating the evaluation rubric:
> 1. It must NOT overwrite or modify the v1.0.0 baseline results.
> 2. It must be versioned as a distinct benchmark configuration (`baseline-v2.0.0`).
> 3. It must report differential deltas against `baseline-v1.0.0`.

---

## 7. Exact Reproduction Commands

To reproduce the frozen baseline results from scratch on this repository:

### Step 1: Run Full Unit Test Regression
```powershell
python -m pytest -v
# Expected: 205 passed in ~7-9s
```

### Step 2: Reproduce Test Case 1 (Comparative Final Baseline)
```powershell
python -m benchmarks.run_benchmark
# Output written to:
# - benchmarks/results/final_baseline_tc1_results.json
# - benchmarks/results/final_baseline_tc1_results.csv
```

### Step 3: Reproduce Test Case 2 (Corrected Multi-Budget Sweep)
```powershell
python -m benchmarks.run_budget_benchmark
# Output written to:
# - benchmarks/results/corrected_budget_benchmark_results.json
# - benchmarks/results/corrected_budget_benchmark_results.csv
```

---

## 8. Proposed Git Tag

- **Proposed Tag:** `baseline-v1.0.0`
- **Target Commit SHA:** `7e3d98c` + Baseline Audits & Benchmark Harnesses
- **Sign-off:** Lead Software Engineer / AI Coding Assistant
- **Status:** **FROZEN — Awaiting User Authorization to Proceed to Phase 6.1 (Session Protocol)**
