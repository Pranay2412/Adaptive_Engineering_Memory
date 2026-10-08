# Adaptive Engineering Memory - Architecture

## 1. System Overview

**Adaptive Engineering Memory** is an intelligent, graph-grounded cognitive memory layer for software engineering. It bridges source code implementation with architectural documentation and technical design decisions, constructing a unified knowledge graph in Neo4j.

On top of this unified graph, it provides:
1. **Multi-Stage Entity-to-Code Matching Bridge**: Resolves documentation concepts to implementing code symbols using lexical, structural, and dense semantic embeddings.
2. **Hybrid Retrieval Layer**: Fuses lexical search, semantic dense vector search, and Neo4j graph traversal into normalized candidate results.
3. **Adaptive Context Orchestrator (ACO)**: Classifies engineering query intent, retrieves relevant context, deduplicates items, ranks candidates, and packs an optimized, token-budgeted prompt context.
4. **Configurable Context Ranking System**: Scores candidates across 8 explicit signals using transparent, configurable weighted formulas.

```
                              User Query
                                  │
                                  ▼
               ┌─────────────────────────────────────┐
               │  Adaptive Context Orchestrator (ACO)│
               │   - Query Intent Classification     │
               │   - Strategy & Source Selection     │
               └──────────────────┬──────────────────┘
                                  │
                                  ▼
               ┌─────────────────────────────────────┐
               │       Hybrid Retrieval Layer        │
               │  ┌──────────┬──────────┬──────────┐ │
               │  │ Lexical  │ Semantic │  Graph   │ │
               │  │  Search  │  Dense   │Traversal │ │
               │  └────┬─────┴────┬─────┴────┬─────┘ │
               └───────┼──────────┼──────────┼───────┘
                       │          │          │
                       ▼          ▼          ▼
             ┌─────────────────────────────────────────┐
             │       Unified Neo4j Memory Store        │
             │   (CodeSymbol ◄─[SPECIFIES]─► Document) │
             └─────────────────────────────────────────┘
                                  │
                                  ▼
               ┌─────────────────────────────────────┐
               │    Configurable Context Ranker      │
               │  - 8 Deterministic Scoring Signals  │
               │  - Intent-Adaptive Weight Tuning    │
               └──────────────────┬──────────────────┘
                                  │
                                  ▼
               ┌─────────────────────────────────────┐
               │      Deduplication & Packing        │
               │  - Path Merging                     │
               │  - Token Budget Enforcement         │
               └──────────────────┬──────────────────┘
                                  │
                                  ▼
                     Structured Markdown Context
                     + Rich Retrieval Metadata
```

---

## 2. Ingestion & Graph Unification

### Ingestion Components
1. **Graphify AST Extraction (`backend/ingestion_code.py`)**:
   - Parses repository code trees into normalized `CodeSymbol` nodes (functions, classes, modules, interfaces).
   - Captures code hierarchies (`DEFINES`), dependency imports (`IMPORTS`), function calls (`CALLS`), and inheritance (`INHERITS`).
2. **Cognee Document Entity Extraction (`backend/ingestion_docs.py`)**:
   - Ingests Markdown specifications, RFCs, and architecture docs.
   - Extracts semantic `DocumentEntity` nodes representing concepts, constraints, and features.
3. **Multi-Stage Semantic Cross-Linking Bridge (`backend/bridge.py` & `backend/semantic_matcher.py`)**:
   - Compares document concepts with code symbols.
   - Applies normalized canonical matching, contextual path/module matching, entity type compatibility, and dense embedding similarity.
   - Creates `SPECIFIES` relationships with explicit confidence, matching method, and component scores.
4. **Neo4j Memory Store (`backend/db_loader.py`)**:
   - Idempotently upserts nodes and edges using Cypher `MERGE`.
   - Enforces user and repository scoping via deterministic UUIDs (`scoped_id`).

---

## 3. Retrieval, Ranking & Orchestration Pipeline

1. **Rule-Based Intent Classifier (`RuleBasedIntentClassifier`)**:
   - Analyzes user queries across 8 engineering intents:
     - `code_navigation`, `explanation`, `debugging`, `feature_implementation`, `refactoring`, `architecture`, `documentation`, `session_continuation`.
   - Extracts candidate symbols (PascalCase, camelCase, snake_case, file paths).
2. **Hybrid Retrieval Service (`HybridRetrievalService`)**:
   - Queries lexical inverted terms, dense vector similarity indices, and multi-hop Cypher/in-memory graph edges simultaneously.
3. **Configurable Context Ranker (`ConfigurableContextRanker`)**:
   - Evaluates each candidate across 8 deterministic signals:
     `semantic_relevance`, `lexical_relevance`, `graph_relevance`, `freshness`, `confidence`, `repository_match`, `source_reliability`, and `token_cost`.
4. **Deduplication Engine (`deduplicate_results`)**:
   - Groups candidates by `node_id` or unique entity-type-path tuples.
   - Merges relationship paths, preserves maximum confidence, combines score breakdowns, and keeps the richest snippet.
5. **Adaptive Context Packer (`AdaptiveContextPacker`)**:
   - Constructs organized Markdown sections (*Architecture & Specifications*, *Code Symbols & Interfaces*, *System Relationships & Traversal Paths*).
   - Strictly enforces prompt token budgets with compaction fallbacks.
