# TEST CASE 1: Graphify vs. Synapse Token Usage Benchmark Report

## 1. Executive Summary
- **Repository Tested**: `C:\FYP`
- **Queries Evaluated**: 5 engineering queries across 5 target categories.
- **Average Input Token Reduction**: **41.99%**
- **Average Context Token Reduction**: **43.45%**
- **Average Absolute Tokens Saved**: **631.6 tokens** per query.

## 2. Token Reduction by Query

| Query | Category | Graphify Input Tokens | Synapse Input Tokens | Reduction % |
| :--- | :--- | :--- | :--- | :--- |
| What modules import models.py? | `structural` | 1520 | 813 | **46.51%** |
| How does authentication work with JWT? | `implementation` | 1490 | 959 | **35.64%** |
| Which components call the authentication service? | `dependency` | 1490 | 889 | **40.34%** |
| Where could JWT verification fail and what code is involved? | `debugging` | 1494 | 797 | **46.65%** |
| Why was RS256 chosen for authentication? | `architecture` | 1526 | 904 | **40.76%** |

## 3. Comprehensive Token Breakdown

| Query | Graphify Context | Synapse Context | Graphify Total | Synapse Total | Total Reduction % |
| :--- | :--- | :--- | :--- | :--- | :--- |
| What modules import models.py? | 1471 | 766 | 1577 | 893 | **43.37%** |
| How does authentication work with JWT? | 1441 | 905 | 1542 | 1361 | **11.74%** |
| Which components call the authentication service? | 1441 | 844 | 1596 | 1149 | **28.01%** |
| Where could JWT verification fail and what code is involved? | 1441 | 747 | 1563 | 1052 | **32.69%** |
| Why was RS256 chosen for authentication? | 1478 | 850 | 1581 | 1000 | **36.75%** |

## 4. Latency & Information Parity Analysis

| Query | Graphify Latency (ms) | Synapse Latency (ms) | Graphify Modality | Synapse Modality |
| :--- | :--- | :--- | :--- | :--- |
| What modules import models.py? | 5897.13 ms | 2773.31 ms | Code Snippets | **Code + Docs + ADRs** |
| How does authentication work with JWT? | 2035.52 ms | 6079.23 ms | Code Snippets | **Code + Docs + ADRs** |
| Which components call the authentication service? | 3588.49 ms | 4710.63 ms | Code Snippets | **Code + Docs + ADRs** |
| Where could JWT verification fail and what code is involved? | 2588.82 ms | 3542.25 ms | Code Snippets | **Code + Docs + ADRs** |
| Why was RS256 chosen for authentication? | 1552.1 ms | 2219.53 ms | Code Snippets | **Code + Docs + ADRs** |

## 5. Aggregate Findings
- **Total Graphify Input Tokens**: 7,520
- **Total Synapse Input Tokens**: 4,362
- **Total Prompt Tokens Saved**: **3,158 tokens**
- **Mean Input Reduction Ratio**: **41.99%**
- **Mean Context Reduction Ratio**: **43.45%**
- **Mean Total Reduction Ratio**: **30.59%**

## 6. Methodology & Scientific Parity
1. **Code-Resolved Parity**: Graphify was executed in `code_resolved` mode, extracting full code snippets from node file/line coordinates rather than raw ASCII labels. This ensures both workflows provide the LLM with sufficient source code to answer implementation questions.
2. **Identical Scaffolding**: Both workflows shared the exact same system prompt template, context budget (1,500 tokens), user query strings, and LLM configuration.
3. **Token Counting**: Measured using `tiktoken` (`cl100k_base`) preflight tokenizer counting and validated against provider-reported API usage.