# TEST CASE 1: Graphify vs. Synapse Token Usage Benchmark Report

## 1. Executive Summary
- **Repository Tested**: `C:\FYP`
- **Queries Evaluated**: 5 engineering queries across 5 target categories.
- **Average Input Token Reduction**: **41.73%**
- **Average Context Token Reduction**: **43.22%**
- **Average Absolute Tokens Saved**: **627.6 tokens** per query.
- **Average Graphify Answer Quality**: **1.3 / 5.0**
- **Average Synapse Answer Quality**: **4.2 / 5.0**

## 2. Token Reduction by Query

| Query | Category | Graphify Input Tokens | Synapse Input Tokens | Reduction % |
| :--- | :--- | :--- | :--- | :--- |
| What modules import models.py? | `structural` | 1520 | 813 | **46.51%** |
| How does authentication work with JWT? | `implementation` | 1490 | 959 | **35.64%** |
| Which components call the authentication service? | `dependency` | 1490 | 889 | **40.34%** |
| Where could JWT verification fail and what code is involved? | `debugging` | 1494 | 797 | **46.65%** |
| Why was RS256 chosen for authentication? | `architecture` | 1526 | 924 | **39.45%** |

## 3. Comprehensive Token & Quality Breakdown

| Query | Graphify Context | Synapse Context | Graphify Total | Synapse Total | Graphify Quality | Synapse Quality |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| What modules import models.py? | 1471 | 766 | 1578 | 910 | 1.00 | **2.00** |
| How does authentication work with JWT? | 1441 | 905 | 1542 | 1357 | 1.00 | **4.75** |
| Which components call the authentication service? | 1441 | 844 | 1616 | 1146 | 2.50 | **5.00** |
| Where could JWT verification fail and what code is involved? | 1441 | 747 | 1564 | 1000 | 1.00 | **4.25** |
| Why was RS256 chosen for authentication? | 1478 | 867 | 1581 | 1022 | 1.00 | **5.00** |

## 4. Latency Distribution & Overhead Analysis

> [!IMPORTANT]
> **Latency Overhead Disclosure**: Synapse significantly reduces token usage, but introduces higher retrieval latency compared to raw Graphify BFS. This is due to multi-source hybrid retrieval (BM25 + vector matching), intent classification, 8-signal ranking, and multi-tier utility-density compaction.

| Query | Graphify Median (ms) | Graphify Spread (ms) | Synapse Median (ms) | Synapse Spread (ms) | Latency Delta |
| :--- | :---: | :---: | :---: | :---: | :---: |
| What modules import models.py? | 4865.46 ms | ±9564.09 ms | 2127.5 ms | ±1022.37 ms | +-2738.0 ms |
| How does authentication work with JWT? | 2568.69 ms | ±954.34 ms | 4813.5 ms | ±457.45 ms | +2244.8 ms |
| Which components call the authentication service? | 3266.9 ms | ±442.05 ms | 3524.85 ms | ±445.52 ms | +257.9 ms |
| Where could JWT verification fail and what code is involved? | 2310.74 ms | ±676.15 ms | 4461.23 ms | ±1016.68 ms | +2150.5 ms |
| Why was RS256 chosen for authentication? | 2325.82 ms | ±240.98 ms | 3821.7 ms | ±1868.15 ms | +1495.9 ms |

## 5. Aggregate Findings
- **Total Graphify Input Tokens**: 7,520
- **Total Synapse Input Tokens**: 4,382
- **Total Prompt Tokens Saved**: **3,138 tokens**
- **Mean Input Reduction Ratio**: **41.73%**
- **Mean Context Reduction Ratio**: **43.22%**
- **Mean Total Reduction Ratio**: **31.04%**
- **Mean Graphify Latency**: 4482.1 ms
- **Mean Synapse Latency**: 3920.0 ms (Overhead: +-562.1 ms)

## 6. Methodology & Scientific Parity
1. **Code-Resolved Parity**: Graphify was executed in `code_resolved` mode, extracting full code snippets from node file/line coordinates rather than raw ASCII labels. This ensures both workflows provide the LLM with sufficient source code to answer implementation questions.
2. **Identical Scaffolding**: Both workflows shared the exact same system prompt template, context budget (1,500 tokens), user query strings, and LLM configuration.
3. **Token Counting**: Measured using `tiktoken` (`cl100k_base`) preflight tokenizer counting and validated against provider-reported API usage.
4. **Quality Evaluation**: Both workflows were evaluated on the identical calibrated reference-based and LLM rubric across a 1.0–5.0 scale without artificial floors.