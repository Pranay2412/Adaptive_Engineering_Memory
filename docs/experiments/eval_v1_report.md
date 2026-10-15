# Synapse Evaluation Framework Benchmark Report

**Generated At:** `2026-10-15T05:51:15.798993+00:00`  
**Total Executed Trials:** `45`  
**Integrity Status:** `PASSED`  

---

## 1. Executive Summary & Configuration Comparison

| Configuration | Mean Context Tokens | Mean Total Tokens | Context Reduction % | Evidence F1 | Overall Quality (1-5) | Mean Latency |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `graphify_baseline` | 1454.4 | 1504.4 | **Baseline** | 0.0 | **2.25 / 5.0** | 444.58 ms |
| `synapse_deterministic` | 825.8 | 885.6 | **43.22%** | 0.12 | **2.25 / 5.0** | 281.9 ms |
| `synapse_none` | 825.8 | 885.6 | **43.22%** | 0.12 | **2.25 / 5.0** | 299.15 ms |

---

## 2. Separate Retrieval & Answer Quality Metrics

### A. Retrieval Quality (Precision, Recall, & Modalities)

| Configuration | Evidence Precision | Evidence Recall | Evidence F1 | Retrieval Latency |
| :--- | :--- | :--- | :--- | :--- |
| `graphify_baseline` | 0.0 | 0.0 | 0.0 | 439.9 ms |
| `synapse_deterministic` | 0.12 | 0.12 | 0.12 | 278.76 ms |
| `synapse_none` | 0.12 | 0.12 | 0.12 | 295.92 ms |

### B. LLM Answer Quality Breakdown (1-5 Rubric)

| Configuration | Correctness | Completeness | Relevance | Evidence Sufficiency | Overall Quality |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `graphify_baseline` | 2.0 | 2.0 | 3.0 | 2.0 | **2.25 / 5.0** |
| `synapse_deterministic` | 2.0 | 2.0 | 3.0 | 2.0 | **2.25 / 5.0** |
| `synapse_none` | 2.0 | 2.0 | 3.0 | 2.0 | **2.25 / 5.0** |

---

## 3. Dataset & Data Integrity Verification

- **Unique Trial Keys:** 45
- **Duplicate Records:** 0
- **Failed Trials:** 0
- **Missing Trials:** 0

*(All aggregates computed deterministically from raw trial records. No manual editing applied.)*
