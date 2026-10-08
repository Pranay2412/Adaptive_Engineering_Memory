# Synapse Token Optimization Engine Specification

## 1. Overview

The **Synapse Token Optimization Engine** (`backend/token_optimizer.py`) provides intelligent context compression and token budgeting for the Adaptive Engineering Memory system. It supports both **deterministic rule-based optimization** and **optional LLM-based structured compression**, allowing developers to trade off between zero-latency heuristic compaction and deep semantic synthesis.

### Core Guarantee: Non-Negotiable Provenance Preservation
Regardless of the compression mode used, the engine strictly enforces that:
- Source identifiers are preserved.
- Code symbols are preserved without alteration.
- Important relationships (`SPECIFIES`, `CALLS`, `IMPORTS`, `DEPENDS_ON`, `INHERITS`) are preserved.
- Recent changes and freshness timestamps are preserved.
- Engineering decisions and architectural rationales are preserved.
- **Compression is never allowed to remove provenance.** The compressed context remains 100% traceable to its original sources.

---

## 2. Four-Stage Optimization Pipeline

The engine executes context optimization through an explicit four-stage pipeline:

```
                      Ranked Context Candidates (from ACO Retrieval)
                                             │
                        ┌────────────────────▼────────────────────┐
                        │          STAGE 1: RAW CONTEXT           │
                        │  - Invariants & Provenance Anchoring    │
                        │  - Unoptimized Baseline Cost Counting   │
                        └────────────────────┬────────────────────┘
                                             │
                        ┌────────────────────▼────────────────────┐
                        │         STAGE 2: DEDUPLICATION          │
                        │  - Exact Node / Location Match Pruning  │
                        │  - Structural Subsumption (Method/Class)│
                        │  - Lexical Jaccard Overlap (>= 0.70)    │
                        └────────────────────┬────────────────────┘
                                             │
                        ┌────────────────────▼────────────────────┐
                        │     STAGE 3: STRUCTURED COMPRESSION     │
                        │                                         │
                        │  Mode "none":                           │
                        │    Full raw text without compaction     │
                        │                                         │
                        │  Mode "deterministic":                  │
                        │    Utility-per-token density ranking    │
                        │    Multi-tier compaction (F/C/M)        │
                        │    Active graph traversal synthesis     │
                        │                                         │
                        │  Mode "llm":                            │
                        │    Structured LLM synthesis pass        │
                        │    ProvenanceGuardrail verification     │
                        │    Traceability Ledger auto-restoration │
                        └────────────────────┬────────────────────┘
                                             │
                        ┌────────────────────▼────────────────────┐
                        │   STAGE 4: TOKEN BUDGET ENFORCEMENT     │
                        │  - Strict Budget Ceiling (tokens <= max)│
                        │  - Itemized Retained & Removed Tracking │
                        │  - Tokens Saved & Reduction Metrics     │
                        └─────────────────────────────────────────┘
```

---

## 3. Supported Optimization Modes

The engine provides three selectable modes via `OptimizationMode`:

| Mode | Description | Compaction Mechanism | Latency / Dependencies |
| :--- | :--- | :--- | :--- |
| **`OptimizationMode.NONE`** (`"none"`) | No optimization / raw baseline. | Sequential raw inclusion until budget is reached; trailing items dropped. | Zero latency, no compression. |
| **`OptimizationMode.DETERMINISTIC`** (`"deterministic"`) | Rule-based utility-density optimization. | Ranks by $U / C$, degrades representations across `FULL` $\to$ `COMPACT` $\to$ `MINIMAL` tiers, extracts graph paths. | Zero latency, offline, 100% deterministic. |
| **`OptimizationMode.LLM`** (`"llm"`) | Structured LLM context compression. | LLM synthesizes concise technical explanations, audited by `ProvenanceGuardrail` to ensure all source links remain intact. | Requires LLM pass (OpenAI, Gemini, or Mock). |

---

## 4. Invariants & Provenance Guardrails

### 4.1 Provenance Anchors (`CandidateProvenanceAnchor`)
Before compression, the engine extracts an explicit provenance anchor for each candidate:
1. **Source Identifiers**: `file_or_document`, `node_id`, `source`, `repository_id`.
2. **Code Symbols**: Exact entity names (`AuthService`), type signatures, qualified names.
3. **Important Relationships**: Multi-hop directional links (`AuthService -[CALLS]-> JWTManager`).
4. **Recent Changes**: Version timestamps, refactor notes (`Migrated to refresh rotation on 2026-10-05`).
5. **Engineering Decisions**: Architectural rationales (`Decision: RS256 chosen over HS256 for asymmetric key rotation`).
6. **Provenance Channels & Confidence**: Extracted AST certainty, semantic similarity, match channels.

### 4.2 Provenance Guardrail (`ProvenanceGuardrail`)
After LLM compression, `ProvenanceGuardrail` audits the generated text:
- Verifies that every source file path and symbol is cited.
- Verifies that relationships and architectural decisions are preserved.
- **Fail-Safe Restoration**: If the LLM dropped any citation, the guardrail automatically appends a formatted **Provenance & Source Traceability Ledger**:

```markdown
#### Provenance & Source Traceability Ledger
- [Source: `services/auth.py` | Node: `sym-auth-001` | Conf: 0.95] Symbol: `AuthService` (class) | Relations: AuthService -[CALLS]-> JWTManager | Decisions: RS256 chosen over HS256 | Recent: Migrated on 2026-10-05
- [Source: `docs/rfcs/001-auth.md` | Node: `doc-spec-001` | Conf: 0.90] Symbol: `Authentication RFC` (spec) | Decisions: Asymmetric RS256 verification
```

---

## 5. Output Schema & Audit Metrics

The engine returns a `TokenOptimizationResult` containing:

| Field | Type | Description |
| :--- | :--- | :--- |
| `optimized_context` | `str` | Final Markdown context ready for LLM prompt ingestion. |
| `estimated_input_tokens` | `int` | Token count of the final optimized context. |
| `original_estimated_tokens` | `int` | Token count of raw uncompressed candidates. |
| `tokens_saved` | `int` | Net tokens saved ($\max(0, \text{original} - \text{final})$). |
| `percentage_reduction` | `float` | Percentage reduction in prompt tokens. |
| `items_retained` | `list[dict]` | Included candidates with selected tier, token cost, and utility density. |
| `items_removed` | `list[dict]` | Pruned candidates with explicit reason (`budget_exceeded`, `duplicate_node_id`, `high_content_overlap`, `structural_subsumption`). |
| `mode` | `str` | Active optimization mode (`"none"`, `"deterministic"`, or `"llm"`). |
| `provenance_audit` | `dict` | Audit report confirming all sources and symbols were preserved. |
| `metadata` | `dict` | Budget utilization and candidate counts. |

---

## 6. Programmatic Usage

### 6.1 Comparing All Three Optimization Modes Side-by-Side

Use `compare_optimization_modes` to benchmark the trade-offs across all three modes:

```python
from backend import compare_optimization_modes, HybridRetrievalResult

results = [
    HybridRetrievalResult(
        entity="AuthService",
        type="class",
        source="code",
        file_or_document="services/auth.py",
        score=0.92,
        content_snippet="Handles login and JWT token issuance. Decision: RS256 used for zero-trust token signing.",
    ),
    HybridRetrievalResult(
        entity="JWTManager",
        type="class",
        source="code",
        file_or_document="security/jwt.py",
        score=0.88,
        content_snippet="Encodes and verifies RSA256 JWT tokens.",
    ),
]

comparison = compare_optimization_modes(
    user_query="How does authentication work?",
    ranked_context_items=results,
    max_token_budget=500,
)

# Inspect side-by-side comparison table
for row in comparison.comparison_table:
    print(f"Mode: {row['name']:<30} Tokens: {row['estimated_input_tokens']:<5} Saved: {row['tokens_saved']:<5} Reduction: {row['percentage_reduction']}% Provenance Preserved: {row['provenance_preserved']}")

# Access individual result containers
print(comparison.none_result.optimized_context)
print(comparison.deterministic_result.optimized_context)
print(comparison.llm_result.optimized_context)
```

### 6.2 Running LLM Compression in Adaptive Context Orchestrator (ACO)

```python
from backend import AdaptiveContextOrchestrator, OptimizationMode

orchestrator = AdaptiveContextOrchestrator()
context = orchestrator.orchestrate(
    query="where is AuthService?",
    user_id="dev-team",
    repository_id="core-backend",
    max_tokens=600,
    optimization_mode=OptimizationMode.LLM,
)

opt_meta = context.retrieval_metadata["token_optimization"]
print("Mode used:", opt_meta["mode"])
print("Tokens saved:", opt_meta["tokens_saved"])
print("Provenance audit:", opt_meta["provenance_audit"])
```

### 6.3 Using `TokenOptimizedContextPacker` Directly

```python
from backend import AdaptiveContextOrchestrator, OptimizationMode, TokenOptimizedContextPacker

# Pack prompt context using LLM structured compression
orchestrator = AdaptiveContextOrchestrator(
    packer=TokenOptimizedContextPacker(mode=OptimizationMode.LLM),
)

context = orchestrator.orchestrate(
    query="explain authentication",
    user_id="dev-team",
    repository_id="core-backend",
    max_tokens=800,
)
```
