# Adaptive Context Orchestrator (ACO) - User Guide

## 1. Overview

The **Adaptive Context Orchestrator (ACO)** coordinates the entire retrieval-to-prompt lifecycle for Adaptive Engineering Memory. Given a user query, developer identity, repository context, and optional token budget, ACO constructs a concise, structured prompt context suitable for LLMs.

---

## 2. Seven-Step Execution Pipeline

```
1. Analyze Query Intent (Deterministic Rule-Based Classifier)
      │
2. Select Knowledge Sources & Strategy (Weights, Hop Limits, Channel Mix)
      │
3. Execute Hybrid Retrieval (Lexical + Dense Vector + Graph Traversal)
      │
4. Rank Retrieved Information (Configurable 8-Signal Multi-Factor Scoring)
      │
5. Remove Duplicate & Redundant Items (Path & Score Merging)
      │
6. Construct Optimized Prompt Context (Structured Markdown & Token Budgeting)
      │
7. Return Orchestrated Context & Metadata (Exposed for Evaluation & Auditing)
```

---

## 3. Supported Engineering Intents

| Intent | Key Indicators | Retrieval Strategy |
| :--- | :--- | :--- |
| **`code_navigation`** | `"where is"`, `"locate"`, `"find function"`, single symbol names | High lexical weight ($0.65$), $1$ hop, focuses on code symbols and module file paths. |
| **`explanation`** | `"how does ... work"`, `"explain"`, `"what does ... do"` | Balanced semantic ($0.45$) and lexical ($0.30$), $2$ hops across `SPECIFIES` links. |
| **`debugging`** | `"fix"`, `"bug"`, `"error"`, `"exception"`, `"failing"` | High lexical error matching ($0.45$), $2$ hops traversing `CALLS` and `DEPENDS_ON`. |
| **`feature_implementation`** | `"implement"`, `"add feature"`, `"create endpoint"`, `"build"` | High semantic pattern search ($0.40$), $2$ hops traversing specs and interfaces. |
| **`refactoring`** | `"refactor"`, `"clean up"`, `"extract method"`, `"decouple"` | Heavy graph traversal ($0.45$ graph weight), $2$ hops analyzing downstream dependents. |
| **`architecture`** | `"architecture"`, `"system design"`, `"topology"`, `"modules overview"` | Deep traversal ($3$ hops, $0.35$ graph weight) across module hierarchies and specs. |
| **`documentation`** | `"document"`, `"docstring"`, `"readme"`, `"swagger"`, `"specs"` | Balanced doc search ($0.40$ lexical, $0.40$ semantic), $1$ hop on `SPECIFIES` links. |
| **`session_continuation`** | `"continue"`, `"resume"`, `"what was I doing"`, `"next step"` | High recency weight ($0.35$), focuses on recently modified symbols and RFC notes. |

---

## 4. Programmatic Usage

### Quick Start with Convenience Function

```python
from backend import orchestrate_context

context = orchestrate_context(
    query="How does authentication work?",
    user_id="user-42",
    repository_id="core-service",
    max_tokens=1500,
)

print(context.context_text)
print("Intent detected:", context.intent.primary_intent)
print("Confidence:", context.intent.confidence)
print("Tokens used:", context.estimated_tokens)
```

### Advanced Usage with Custom Ranker and Classifier

```python
from backend import (
    AdaptiveContextOrchestrator,
    ConfigurableContextRanker,
    RankingWeights,
    RuleBasedIntentClassifier,
)

# Custom ranking weights prioritizing code authority and low token cost
weights = RankingWeights(
    source_reliability=0.30,
    lexical_relevance=0.25,
    semantic_relevance=0.20,
    token_cost=0.15,
    graph_relevance=0.10,
)

orchestrator = AdaptiveContextOrchestrator(
    classifier=RuleBasedIntentClassifier(),
    ranker=ConfigurableContextRanker(weights=weights, intent_adaptive=True),
)

context = orchestrator.orchestrate(
    query="where is AuthService?",
    user_id="dev-team",
    repository_id="backend-api",
    max_tokens=1000,
)
```

---

## 5. Output Format

The packed context text is organized into clear Markdown sections:

```markdown
### Engineering Memory Context (Intent: Explanation)
> Confidence: 0.88 | Strategy: Balancing semantic conceptual search with specifications and implementation symbols.

#### Architecture & Specifications
- **Authentication RFC** (`spec`) in `docs/rfcs/001-auth.md` [score: 0.92]
  > Specification for authentication, session tokens, and security policies.

#### Code Symbols & Interfaces
- **AuthService** (`class`) in `services/auth.py` [score: 0.89]
  > Handles user login and token validation.

#### System Relationships & Traversal Paths
- **JWTManager** (`class`) in `security/jwt.py` [score: 0.84]
  - *Path*: `AuthService -[CALLS]-> JWTManager`
```
