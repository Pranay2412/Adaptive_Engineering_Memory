# Synapse Token Optimization Engine Specification

## 1. Overview

The **Synapse Token Optimization Engine** (`backend/token_optimizer.py`) provides deterministic, rule-based context compression and token budgeting for the Adaptive Engineering Memory system. Rather than relying on expensive, non-deterministic LLM compression passes, it uses information density heuristics, multi-tier representation compaction, redundancy detection, and knapsack packing to fit maximal high-utility engineering context into a fixed token budget.

---

## 2. Core Optimization Pipeline

```
           Ranked Context Candidates (from ACO Retrieval)
                               │
                1. Deterministic Token Estimation
                               │
            2. Utility-Per-Token Density Ranking (U / C)
                               │
            3. Redundancy Detection & Deduplication
               ├── Exact Node ID / Entity Location Match
               ├── Structural Subsumption (Method inside Class)
               └── Lexical Jaccard Overlap (threshold >= 0.70)
                               │
            4. Multi-Tier Representation Compaction
               ├── Tier 1: FULL     (Complete signatures, docstrings, paths)
               ├── Tier 2: COMPACT  (Trimmed summaries, 1-line signatures)
               └── Tier 3: MINIMAL  (Compact identifier bullets)
                               │
            5. Relational Graph Context Preservation
               └── Inter-entity hops (SPECIFIES, CALLS, IMPORTS, etc.)
                               │
            6. Strict Budget Ceiling Enforcement
               └── Halts packing when remaining tokens < threshold
                               │
            Optimized Prompt Context + Full Audit Metrics
```

---

## 3. Key Components

### 3.1 Deterministic Token Estimation (`TokenEstimator`)
- Blends character-level heuristics ($\approx 4\text{ chars/token}$) with word-count ratios ($1.25\times\text{ words}$) for accurate offline token estimation without requiring external tokenizer libraries or network calls.
- Provides token cost estimation for raw text and candidate items at any representation tier.

### 3.2 Utility-per-Token Density Ranking
Candidates are evaluated for both base utility ($U$) and token footprint ($C$):
$$U = \text{Score} \times \text{QueryTokenBoost} \times \text{GraphConnectivityBoost}$$
$$\text{Density} = \frac{U}{\max(1, C_{\text{FULL}})}$$

Items with higher information density (concise symbols and key specifications) are packed before verbose, low-signal items.

### 3.3 Redundancy Pruning (`RedundancyDetector`)
Prevents token waste from three common sources of context redundancy:
1. **Exact Duplicate**: Identical `node_id` or same entity name within the same file location.
2. **Structural Subsumption**: A method or sub-element whose definition is already fully captured within an accepted enclosing class or module snippet.
3. **Lexical Jaccard Overlap**: Documents or chunks with token Jaccard similarity $\ge 0.70$.

### 3.4 Multi-Tier Compaction (`ContextTier`)
When an item has high utility but the remaining budget cannot accommodate its `FULL` representation, the optimizer degrades the item through a hierarchy of tiers:
- **`FULL`**: Complete symbol signature, documentation snippet, file location, confidence, and complete graph paths.
- **`COMPACT`**: Condensed summary ($\le 140$ chars), essential identifier signature, and concise relationship links ($\approx 40\text{--}50\%$ fewer tokens).
- **`MINIMAL`**: Single-line bullet with identifier and location ($\approx 75\text{--}85\%$ fewer tokens).

### 3.5 Graph Relationship Preservation
Relationships connecting accepted context items (e.g., `SPECIFIES`, `CALLS`, `DEPENDS_ON`, `IMPORTS`) are extracted and rendered in a dedicated relational section if budget permits:
```markdown
#### Relational Context & Traversal Paths
- `AuthService` -[CALLS]-> `JWTManager` (conf: 1.00)
- `Authentication RFC` -[SPECIFIES]-> `AuthService` (conf: 0.95)
```

### 3.6 Strict Budget Ceiling
The engine strictly enforces `estimated_input_tokens <= max_token_budget`. If an item cannot fit even in `MINIMAL` tier, it is logged in `items_removed` with the reason `budget_exceeded`.

---

## 4. Audit Metrics & Output Structure

The optimizer returns a `TokenOptimizationResult` containing complete metrics:

| Field | Type | Description |
| :--- | :--- | :--- |
| `optimized_context` | `str` | The assembled Markdown context ready for LLM prompt ingestion. |
| `estimated_input_tokens` | `int` | Token count of the final optimized context. |
| `original_estimated_tokens` | `int` | Token count if all candidates were rendered in `FULL` tier unbudgeted. |
| `tokens_saved` | `int` | Net tokens saved: $\max(0, \text{original} - \text{final})$. |
| `percentage_reduction` | `float` | Percentage of prompt tokens saved ($(\text{saved} / \text{original}) \times 100$). |
| `items_removed` | `list[dict]` | Audit log of pruned candidates with removal reasons and original token costs. |
| `items_retained` | `list[dict]` | List of included candidates with selected tier, token cost, and utility density. |
| `metadata` | `dict` | Additional diagnostic counters and budget utilization percentages. |

---

## 5. Usage Examples

### 5.1 Standalone Usage (`optimize_tokens`)

```python
from backend import optimize_tokens, HybridRetrievalResult

results = [
    HybridRetrievalResult(
        entity="AuthService",
        type="class",
        source="code",
        file_or_document="services/auth.py",
        score=0.92,
        content_snippet="Handles login and JWT token issuance.",
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

result = optimize_tokens(
    user_query="How does authentication work?",
    ranked_context_items=results,
    max_token_budget=500,
)

print(result.optimized_context)
print(f"Tokens saved: {result.tokens_saved} ({result.percentage_reduction}%)")
print(f"Retained {len(result.items_retained)} items, removed {len(result.items_removed)} items")
```

### 5.2 Integration with Adaptive Context Orchestrator (ACO)

When `max_tokens` is provided to `orchestrate_context`, the optimizer runs automatically and exposes its results in `retrieval_metadata["token_optimization"]`:

```python
from backend import orchestrate_context

orchestrated = orchestrate_context(
    query="where is AuthService?",
    user_id="dev-team",
    repository_id="core-backend",
    max_tokens=600,
)

token_opt = orchestrated.retrieval_metadata.get("token_optimization")
if token_opt:
    print("Optimization tokens saved:", token_opt["tokens_saved"])
    print("Percentage reduction:", token_opt["percentage_reduction"])
    for item in token_opt["items_retained"]:
        print(f" - {item['entity']} [{item['tier']}] ({item['tokens']} tokens)")
```

### 5.3 Using `TokenOptimizedContextPacker` Directly in ACO

To use multi-tier knapsack packing for the prompt context text itself:

```python
from backend import AdaptiveContextOrchestrator, TokenOptimizedContextPacker

orchestrator = AdaptiveContextOrchestrator(
    packer=TokenOptimizedContextPacker(),
)

context = orchestrator.orchestrate(
    query="explain authentication",
    user_id="dev-team",
    repository_id="core-backend",
    max_tokens=800,
)
```
