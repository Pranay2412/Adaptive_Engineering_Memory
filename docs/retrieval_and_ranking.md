# Retrieval and Ranking Specification

## 1. Hybrid Retrieval Architecture

The Hybrid Retrieval Layer (`backend/hybrid_retrieval.py`) combines three independent search channels to retrieve comprehensive engineering context:

1. **Lexical Channel**:
   - Matches keywords, identifier tokens, and query substrings against symbol names, descriptions, and file paths.
   - Provides exact identifier lookups with zero embedding latency.
2. **Semantic / Dense Vector Channel**:
   - Embeds document entities and code symbols using configurable embedding models (`openai`, `gemini`, or `mock`).
   - Retrieves top-N semantically similar concepts (e.g. mapping "payment processor" to `StripePaymentHandler`).
3. **Graph Traversal Channel**:
   - Expands seed matches across graph edges (`SPECIFIES`, `CALLS`, `IMPORTS`, `DEPENDS_ON`, `INHERITS`).
   - Reconstructs multi-hop relationship paths connecting requirements to implementation code.

---

## 2. Configurable Context Ranking System

The ranking engine (`backend/ranking.py`) evaluates candidate context items across 8 explicit signals on a normalized $[0.0, 1.0]$ scale.

### The 8 Ranking Signals

| Signal | Evaluation Logic | Range |
| :--- | :--- | :---: |
| **`semantic_relevance`** | Vector cosine similarity or concept alignment score. | $[0.0, 1.0]$ |
| **`lexical_relevance`** | Exact match ($1.0$), substring overlap ($0.90$), or token overlap ratio. | $[0.0, 1.0]$ |
| **`graph_relevance`** | Multi-hop decay ($0.85^{\text{hops}-1}$) multiplied by relation type importance (`SPECIFIES`: $1.0$, `CALLS`: $0.95$, `DEPENDS_ON`: $0.90$, `IMPORTS`: $0.85$). | $[0.0, 1.0]$ |
| **`freshness`** | Timestamp recency decay: $\le 7$ days $\to 1.0$, $\le 30$ days $\to 0.85$, $\le 90$ days $\to 0.70$, $>180$ days $\to 0.35$. Neutral baseline ($0.50$). | $[0.0, 1.0]$ |
| **`confidence`** | Extracted edge confidence, AST parser certainty, or bridge match score. | $[0.0, 1.0]$ |
| **`repository_match`** | Matching target repo ($1.0$), sub-repo/module ($0.75$), unscoped ($0.50$), foreign repository ($0.10$). | $[0.0, 1.0]$ |
| **`source_reliability`** | Ground-truth code AST ($1.0$), formal spec/RFC ($0.95$), general docs ($0.85$), heuristic ($0.65$), external ($0.50$). | $[0.0, 1.0]$ |
| **`token_cost`** | Efficiency score ($e^{-\text{tokens}/T_{\text{ref}}}$) rewarding high information density and penalizing bloated context items. | $[0.0, 1.0]$ |

### Weighted Composite Scoring Formula

$$\text{Total Score} = \sum_{k=1}^8 W_k \times S_k$$

Where weights are defined via `RankingWeights`:
```python
from backend.models import RankingWeights
from backend.ranking import ConfigurableContextRanker

# Example custom weights prioritizing lexical accuracy and low token footprint
weights = RankingWeights(
    lexical_relevance=0.35,
    semantic_relevance=0.25,
    graph_relevance=0.15,
    freshness=0.05,
    confidence=0.05,
    repository_match=0.05,
    source_reliability=0.05,
    token_cost=0.05,
)

ranker = ConfigurableContextRanker(weights=weights)
```

---

## 3. Score Exposure for Debugging & Research

Every candidate exposes its full scoring breakdown in `scores_breakdown` and `metadata`:

```json
{
  "entity": "AuthService",
  "score": 0.8932,
  "scores_breakdown": {
    "semantic_relevance": 0.88,
    "lexical_relevance": 0.95,
    "graph_relevance": 0.72,
    "freshness": 0.85,
    "confidence": 1.0,
    "repository_match": 1.0,
    "source_reliability": 1.0,
    "token_cost": 0.89,
    "total_score": 0.8932
  },
  "metadata": {
    "ranking_explanation": "Score: 0.8932 | sem=0.88, lex=0.95, graph=0.72, fresh=0.85, conf=1.00, repo=1.00, src=1.00, cost=0.89"
  }
}
```

---

## 4. Intent-Adaptive Weight Tuning

When `intent_adaptive=True` (default), the ranker dynamically modulates weights based on detected intent:
- **`code_navigation`**: Boosts `lexical_relevance` ($\times 1.8$) and `confidence` ($\times 1.4$).
- **`refactoring`**: Boosts `graph_relevance` ($\times 2.2$) to expose caller and import blast radius.
- **`explanation`**: Boosts `semantic_relevance` ($\times 1.6$) and `source_reliability` ($\times 1.4$).
- **`session_continuation`**: Boosts `freshness` ($\times 2.5$).
