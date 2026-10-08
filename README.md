# Adaptive Engineering Memory

Adaptive Engineering Memory constructs a unified cognitive repository knowledge graph in Neo4j by combining Graphify's AST code extraction with Cognee's document entity extraction. It bridges technical design documentation to code implementation and provides a multi-channel **Hybrid Retrieval Layer**, an **Adaptive Context Orchestrator (ACO)**, and a **Configurable Multi-Signal Context Ranking Engine**.

---

## Key Capabilities

1. **Unified Graph Schema (`backend/models.py`, `backend/db_loader.py`)**:
   - Represents `CodeSymbol`, `DocumentEntity`, `Repository`, `Module`, and `Document` nodes.
   - Captures first-class relationships: `CALLS`, `IMPORTS`, `INHERITS`, `DEFINES`, `DEPENDS_ON`, `MOTIVATES`, and `SPECIFIES`.
   - Stores confidence, matching methods, and component scores on `SPECIFIES` cross-links.

2. **Multi-Stage Semantic Cross-Linking Bridge (`backend/bridge.py`, `backend/semantic_matcher.py`)**:
   - Matches document entities to code symbols using exact, canonical, contextual, and dense vector embeddings (OpenAI, Gemini, or Mock).
   - Generates candidates cheaply first, reserving semantic scoring for ambiguous pairs.

3. **Hybrid Retrieval Layer (`backend/hybrid_retrieval.py`)**:
   - Fuses lexical keyword search, dense vector similarity, and multi-hop Neo4j graph traversal into a single normalized result structure with complete provenance.

4. **Adaptive Context Orchestrator (ACO) (`backend/orchestrator.py`)**:
   - Analyzes query intent across 8 core engineering intents (`code_navigation`, `explanation`, `debugging`, `feature_implementation`, `refactoring`, `architecture`, `documentation`, `session_continuation`).
   - Selects intent-specific retrieval strategies, ranks candidates, deduplicates paths, and packs an optimized, token-budgeted prompt context.

5. **Configurable Context Ranking System (`backend/ranking.py`)**:
   - Scores candidates across 8 explicit signals: `semantic_relevance`, `lexical_relevance`, `graph_relevance`, `freshness`, `confidence`, `repository_match`, `source_reliability`, and `token_cost`.
   - Supports configurable weights, intent-adaptive modulation, and full individual score exposure for debugging and research evaluation.

6. **Synapse Token Optimization Engine (`backend/token_optimizer.py`)**:
   - Executes a 4-stage optimization pipeline: `raw context` $\to$ `deduplication` $\to$ `structured compression` $\to$ `token budget enforcement`.
   - Supports 3 configurable modes for benchmarking: `none` (raw baseline), `deterministic` (utility-density ranking + multi-tier compaction), and `llm` (structured LLM synthesis).
   - Enforces non-negotiable provenance preservation: never drops source identifiers, symbols, relationships, recent changes, or engineering decisions; fail-safe `ProvenanceGuardrail` automatically restores traceability ledgers.
   - Provides side-by-side mode comparison (`compare_optimization_modes`) and full audit metrics.

---

## Requirements

- **Python 3.12** recommended on Windows / PowerShell.
- **Neo4j** instance with Bolt protocol enabled (default: `bolt://localhost:7687`).
- **OpenAI API Key** or **Google Gemini API Key** (for document entity extraction and embeddings).
- **Git** (recommended for Graphify repository scanning).

---

## Setup & Installation

### 1. Create Virtual Environment & Install Dependencies

```powershell
py -3.12 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 2. Configure Environment Variables

Copy `.env.example` to `.env` and fill in your credentials:

```powershell
Copy-Item .env.example .env
```

Key configuration variables:
```dotenv
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=replace-with-your-neo4j-password

LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
OPENAI_API_KEY=replace-with-your-openai-key

EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=openai/text-embedding-3-small
```

*(See [`.env.example`](.env.example) for all available options including Gemini).*

### 3. Upgrade Cognee Migrations

Run Cognee forward migrations prior to ingestion:

```powershell
cognee-cli upgrade
cognee-cli current
```

---

## Ingestion Pipeline

Run repository ingestion from the project root:

```powershell
python -m backend.pipeline --repo "C:\path\to\repository" --user-id "user-123" --repo-id "core-service"
```

- Ingests repository AST via Graphify.
- Ingests Markdown documentation via Cognee.
- Establishes `SPECIFIES` cross-links between documentation concepts and code symbols.
- Upserts the combined graph into Neo4j with idempotent `MERGE` semantics.

---

## Querying with the Adaptive Context Orchestrator (ACO)

Use the top-level convenience API to retrieve and assemble token-budgeted prompt contexts:

```python
from backend import orchestrate_context

# Execute end-to-end query orchestration
context = orchestrate_context(
    query="How does authentication work?",
    user_id="user-123",
    repository_id="core-service",
    max_tokens=1500,
)

# Access packed prompt context and metadata
print(context.context_text)
print("Detected Intent:", context.intent.primary_intent.value)
print("Intent Confidence:", context.intent.confidence)
print("Estimated Tokens:", context.estimated_tokens)
print("Retrieved Results:", len(context.retrieved_results))
```

### Custom Context Ranking

Configure ranking weights across the 8 scoring signals:

```python
from backend import (
    AdaptiveContextOrchestrator,
    ConfigurableContextRanker,
    RankingWeights,
)

# Custom weights prioritizing exact lexical relevance and low token cost
weights = RankingWeights(
    lexical_relevance=0.35,
    semantic_relevance=0.25,
    graph_relevance=0.15,
    token_cost=0.15,
    source_reliability=0.10,
)

orchestrator = AdaptiveContextOrchestrator(
    ranker=ConfigurableContextRanker(weights=weights, intent_adaptive=True),
)

context = orchestrator.orchestrate(
    query="where is AuthService?",
    user_id="user-123",
    repository_id="core-service",
    max_tokens=1000,
)
```

---

## Running Tests

All unit tests run completely offline with zero external network or database dependencies using mock drivers and providers:

```powershell
python -m unittest discover -s tests -v
```

Test suite breakdown (**150 tests passed**):
- `test_token_optimizer.py`: Provenance invariants, LLM compression, 4-stage pipeline execution, 3-mode comparison (`none`, `deterministic`, `llm`), budget stopping, and orchestrator integration.
- `test_orchestrator.py`: Intent classification across all 8 intents, strategy selection, token budgeting, and ACO orchestration.
- `test_ranking.py`: All 8 ranking signals, weight normalization, deterministic reordering, score exposure, and tie-breaking.
- `test_hybrid_retrieval.py`: Lexical, dense vector, and multi-hop graph traversal fusion.
- `test_bridge.py` & `test_semantic_matcher.py`: Multi-stage cross-linking and embedding candidate retrieval.
- `test_schema.py`: Neo4j unified schema, indexes, constraints, and migration safety.
- `test_normalization.py`: Identifier canonicalization and entity kind detection.

---

## Detailed Documentation

- [System Architecture](docs/architecture.md)
- [Synapse Token Optimization Engine](docs/token_optimization.md)
- [Hybrid Retrieval & Configurable Ranking](docs/retrieval_and_ranking.md)
- [Adaptive Context Orchestrator (ACO) Guide](docs/orchestrator_guide.md)
- [Neo4j Unified Schema Reference](docs/schema_reference.md)

---

## Troubleshooting

- **Neo4j connection refused:** Confirm container or service is running and `NEO4J_URI` points to the active Bolt port (`bolt://localhost:7687`).
- **Missing model credentials:** Provide `OPENAI_API_KEY` or `GEMINI_API_KEY` in `.env`.
- **`Could not find lbug C API shared library` on Windows:** Use Python 3.12 (Cognee 1.6+ requires OpenSSL 3 runtime).
- **Missing Graphify output:** Verify Graphify is installed in your virtual environment and the repository contains `.graphify/graph.json` or `graphify-out/graph.json`.
