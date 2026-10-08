"""Configurable Context Ranking System for Adaptive Context Orchestrator (ACO).

Evaluates candidate context items across 8 explicit, deterministic signals:
1. semantic_relevance: dense vector similarity or concept alignment
2. lexical_relevance: exact token and symbol name overlap
3. graph_relevance: relationship path proximity, hop decay, and edge type importance
4. freshness: recency and age decay based on timestamps or session activity
5. confidence: structural, parser, or cross-linking extraction confidence
6. repository_match: alignment with target repository scope
7. source_reliability: provenance authority (AST code vs RFC spec vs heuristic)
8. token_cost: efficiency/budget footprint penalty

Provides configurable weighted scoring formulas and exposes all individual signal
scores on every candidate for debugging and research evaluation.
"""

from __future__ import annotations

import datetime
import logging
import math
from typing import Any

from .models import (
    CandidateRankingScore,
    HybridRetrievalResult,
    IntentAnalysisResult,
    QueryIntent,
    RankingWeights,
)
from .normalization import tokenize_name

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default Provenance Authority Map
# ---------------------------------------------------------------------------

DEFAULT_SOURCE_RELIABILITY_MAP: dict[str, float] = {
    "code": 1.0,               # Ground-truth AST parsed implementation
    "rfc": 0.95,               # Approved technical RFC or specification
    "architecture_spec": 0.95, # Verified system architecture documentation
    "architecture_decision": 0.95, # Formal engineering architecture decision
    "spec": 0.95,              # Formal requirements / spec
    "team_memory": 0.90,       # Verified team engineering knowledge card
    "session_memory": 0.90,    # Distilled AI development session memory
    "documentation": 0.85,     # General developer documentation
    "docstring": 0.85,         # Code docstrings and inline docblocks
    "comment": 0.75,           # Code comments
    "knowledge_graph": 0.75,   # Inferred graph edge
    "heuristic": 0.65,         # Heuristic match
    "synthetic": 0.60,         # Synthetic or auto-generated entity
    "external": 0.50,          # Third-party or unverified source
}

# Relationship type importance in graph relevance
RELATIONSHIP_IMPORTANCE_MAP: dict[str, float] = {
    "SPECIFIES": 1.0,
    "CALLS": 0.95,
    "DEFINES": 0.90,
    "DEPENDS_ON": 0.90,
    "INHERITS": 0.85,
    "IMPORTS": 0.85,
    "MOTIVATES": 0.80,
}


# ---------------------------------------------------------------------------
# Signal Calculation Functions
# ---------------------------------------------------------------------------

def compute_semantic_relevance(candidate: HybridRetrievalResult) -> float:
    """Extract semantic relevance in [0.0, 1.0]."""
    score = candidate.scores_breakdown.get("semantic_score")
    if score is not None:
        return max(0.0, min(1.0, float(score)))

    meta_sim = candidate.metadata.get("semantic_similarity")
    if meta_sim is not None:
        return max(0.0, min(1.0, float(meta_sim)))

    # Fallback to general score if channel includes semantic
    if "semantic" in candidate.relevance_information.lower():
        return max(0.0, min(1.0, float(candidate.score)))

    return 0.0


def compute_lexical_relevance(candidate: HybridRetrievalResult, query: str) -> float:
    """Compute lexical relevance in [0.0, 1.0] using exact match and token overlap."""
    breakdown_lex = candidate.scores_breakdown.get("lexical_score")
    base_lex = float(breakdown_lex) if breakdown_lex is not None else 0.0

    if not query:
        return base_lex

    q_lower = query.lower().strip()
    ent_lower = candidate.entity.lower().strip()
    file_lower = candidate.file_or_document.lower().strip()

    # Exact entity match
    if ent_lower == q_lower:
        overlap_score = 1.0
    elif ent_lower in q_lower or q_lower in ent_lower:
        overlap_score = 0.90
    else:
        # Tokenize on original case to properly split camelCase / PascalCase
        q_tokens = set(t.lower() for t in tokenize_name(query))
        ent_tokens = set(t.lower() for t in tokenize_name(candidate.entity))
        file_tokens = set(t.lower() for t in tokenize_name(candidate.file_or_document))
        all_candidate_tokens = ent_tokens.union(file_tokens)

        # Match tokens directly or check substring overlap
        matched = {t for t in q_tokens if t in all_candidate_tokens or t in ent_lower or t in file_lower}
        overlap_score = len(matched) / len(q_tokens) if q_tokens else 0.0

    return max(base_lex, overlap_score)


def compute_graph_relevance(candidate: HybridRetrievalResult) -> float:
    """Compute graph relevance in [0.0, 1.0] based on path length and relationship types."""
    if not candidate.relationship_path:
        breakdown_graph = candidate.scores_breakdown.get("graph_score")
        return float(breakdown_graph) if breakdown_graph is not None else 0.0

    path = candidate.relationship_path
    hops = len(path)

    # Hop distance factor (exponential decay)
    hop_factor = max(0.2, 0.85 ** (hops - 1))

    # Relationship type factor
    rel_weights = [
        RELATIONSHIP_IMPORTANCE_MAP.get(h.relation.upper(), 0.70)
        for h in path
    ]
    avg_rel_weight = sum(rel_weights) / len(rel_weights)

    # Path confidence factor
    conf_values = [h.confidence for h in path if h.confidence is not None]
    avg_conf = (sum(conf_values) / len(conf_values)) if conf_values else 0.90

    return max(0.0, min(1.0, hop_factor * avg_rel_weight * avg_conf))


def compute_freshness(
    candidate: HybridRetrievalResult,
    reference_time: datetime.datetime | None = None,
) -> float:
    """Compute freshness in [0.0, 1.0] from creation timestamps or recency scores."""
    # Check breakdown first
    recency_score = candidate.scores_breakdown.get("recency_score")
    if recency_score is not None:
        return max(0.0, min(1.0, float(recency_score)))

    # Inspect timestamps
    created_at = candidate.metadata.get("created_at") or candidate.metadata.get("updated_at")
    if created_at:
        try:
            created_dt = datetime.datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            ref_dt = reference_time or datetime.datetime.now(datetime.timezone.utc)
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=datetime.timezone.utc)
            if ref_dt.tzinfo is None:
                ref_dt = ref_dt.replace(tzinfo=datetime.timezone.utc)

            age_days = max(0, (ref_dt - created_dt).days)
            if age_days <= 7:
                return 1.0
            elif age_days <= 30:
                return 0.85
            elif age_days <= 90:
                return 0.70
            elif age_days <= 180:
                return 0.55
            else:
                return max(0.20, 1.0 - (age_days / 365.0))
        except Exception:
            pass

    # Neutral baseline
    if "recent" in candidate.relevance_information.lower() or "recent" in candidate.entity.lower():
        return 0.85
    return 0.50


def compute_confidence(candidate: HybridRetrievalResult) -> float:
    """Extract or calculate extraction/cross-linking confidence in [0.0, 1.0]."""
    if candidate.confidence is not None:
        return max(0.0, min(1.0, float(candidate.confidence)))

    meta_conf = candidate.metadata.get("confidence")
    if meta_conf is not None:
        return max(0.0, min(1.0, float(meta_conf)))

    # Code symbols extracted from AST have highest intrinsic confidence
    if candidate.source == "code" or "CodeSymbol" in candidate.type:
        return 1.0
    if candidate.source == "documentation" or "DocumentEntity" in candidate.type:
        return 0.95

    return 0.80


def compute_repository_match(
    candidate: HybridRetrievalResult,
    target_repository_id: str | None,
) -> float:
    """Compute repository match in [0.0, 1.0]."""
    if not target_repository_id:
        return 1.0

    target = target_repository_id.strip()
    item_repo = (candidate.repository or "").strip()

    if not item_repo:
        return 0.50  # Unspecified repository

    if item_repo == target:
        return 1.0   # Exact match

    # Sub-repository or dependency namespace match
    if item_repo.startswith(target) or target.startswith(item_repo):
        return 0.75

    return 0.10      # Foreign repository mismatch


def compute_source_reliability(
    candidate: HybridRetrievalResult,
    reliability_map: dict[str, float] | None = None,
) -> float:
    """Compute source reliability in [0.0, 1.0] from provenance and entity type."""
    r_map = reliability_map or DEFAULT_SOURCE_RELIABILITY_MAP

    # 1. Check entity type specifics (e.g. RFC, ArchitectureSpec)
    type_lower = candidate.type.lower()
    for k, v in r_map.items():
        if k in type_lower:
            return v

    # 2. Check source channel
    src_lower = candidate.source.lower()
    if src_lower in r_map:
        return r_map[src_lower]

    # 3. Check metadata provenance
    provenance_channel = candidate.provenance.channel.lower() if candidate.provenance else ""
    if provenance_channel in r_map:
        return r_map[provenance_channel]

    return 0.70


def compute_token_cost_efficiency(
    candidate: HybridRetrievalResult,
    reference_tokens: int = 400,
) -> float:
    """Compute token cost efficiency score in [0.0, 1.0].

    High score (close to 1.0) indicates compact, high-density items that consume few tokens.
    Low score (close to 0.0) indicates bloated items with heavy token consumption.
    """
    tokens = candidate.estimate_tokens()
    if tokens <= 0:
        return 1.0

    # Exponential decay with half-life around reference_tokens
    # 50 tokens -> 0.88, 200 tokens -> 0.60, 400 tokens -> 0.36, 1000 tokens -> 0.08
    efficiency = math.exp(-tokens / max(100, reference_tokens))
    return max(0.05, min(1.0, round(efficiency, 4)))


# ---------------------------------------------------------------------------
# Intent-Adaptive Weights
# ---------------------------------------------------------------------------

def adapt_weights_for_intent(
    base_weights: RankingWeights,
    intent: QueryIntent,
) -> RankingWeights:
    """Adapt ranking weights based on user query intent."""
    w = base_weights.to_dict()

    if intent == QueryIntent.CODE_NAVIGATION:
        w["lexical_relevance"] *= 1.8
        w["confidence"] *= 1.4
        w["semantic_relevance"] *= 0.8

    elif intent == QueryIntent.EXPLANATION:
        w["semantic_relevance"] *= 1.6
        w["source_reliability"] *= 1.4

    elif intent == QueryIntent.DEBUGGING:
        w["lexical_relevance"] *= 1.4
        w["graph_relevance"] *= 1.5
        w["confidence"] *= 1.3

    elif intent == QueryIntent.FEATURE_IMPLEMENTATION:
        w["semantic_relevance"] *= 1.3
        w["graph_relevance"] *= 1.3

    elif intent == QueryIntent.REFACTORING:
        w["graph_relevance"] *= 2.2
        w["lexical_relevance"] *= 1.2

    elif intent == QueryIntent.ARCHITECTURE:
        w["graph_relevance"] *= 1.8
        w["source_reliability"] *= 1.6
        w["semantic_relevance"] *= 1.2

    elif intent == QueryIntent.DOCUMENTATION:
        w["source_reliability"] *= 1.8
        w["lexical_relevance"] *= 1.3
        w["semantic_relevance"] *= 1.3

    elif intent == QueryIntent.SESSION_CONTINUATION:
        w["freshness"] *= 2.5
        w["semantic_relevance"] *= 1.2

    adapted = RankingWeights.from_dict(w)
    return adapted.normalized()


from abc import ABC, abstractmethod

# ---------------------------------------------------------------------------
# Base Context Ranker Interface
# ---------------------------------------------------------------------------

class BaseContextRanker(ABC):
    """Abstract interface for re-ranking retrieved knowledge results."""

    @abstractmethod
    def rank(
        self,
        results: list[HybridRetrievalResult],
        intent_result: IntentAnalysisResult,
        query: str,
        repository_id: str | None = None,
    ) -> list[HybridRetrievalResult]:
        """Rank and return ordered results."""
        pass


# ---------------------------------------------------------------------------
# Configurable Context Ranker
# ---------------------------------------------------------------------------

class ConfigurableContextRanker(BaseContextRanker):
    """Deterministic, configurable ranking engine for Adaptive Context Orchestrator.

    Scores each candidate across all 8 signals:
    - semantic_relevance
    - lexical_relevance
    - graph_relevance
    - freshness
    - confidence
    - repository_match
    - source_reliability
    - token_cost

    Exposes all individual signal scores and weights for transparency and research evaluation.
    """

    def __init__(
        self,
        weights: RankingWeights | None = None,
        intent_adaptive: bool = True,
        token_cost_reference: int = 400,
        source_reliability_map: dict[str, float] | None = None,
        reference_time: datetime.datetime | None = None,
    ) -> None:
        self.weights = weights.normalized() if weights else RankingWeights().normalized()
        self.intent_adaptive = intent_adaptive
        self.token_cost_reference = token_cost_reference
        self.source_reliability_map = source_reliability_map or DEFAULT_SOURCE_RELIABILITY_MAP
        self.reference_time = reference_time

    def score_candidate(
        self,
        candidate: HybridRetrievalResult,
        query: str,
        target_repository_id: str | None = None,
        active_weights: RankingWeights | None = None,
    ) -> CandidateRankingScore:
        """Compute all 8 individual scores and the weighted composite total score for a candidate."""
        w = active_weights or self.weights

        s_sem = compute_semantic_relevance(candidate)
        s_lex = compute_lexical_relevance(candidate, query)
        s_graph = compute_graph_relevance(candidate)
        s_fresh = compute_freshness(candidate, self.reference_time)
        s_conf = compute_confidence(candidate)
        s_repo = compute_repository_match(candidate, target_repository_id)
        s_src = compute_source_reliability(candidate, self.source_reliability_map)
        s_cost = compute_token_cost_efficiency(candidate, self.token_cost_reference)

        total = (
            w.semantic_relevance * s_sem
            + w.lexical_relevance * s_lex
            + w.graph_relevance * s_graph
            + w.freshness * s_fresh
            + w.confidence * s_conf
            + w.repository_match * s_repo
            + w.source_reliability * s_src
            + w.token_cost * s_cost
        )

        explanation = (
            f"Score: {total:.4f} | "
            f"sem={s_sem:.2f}, lex={s_lex:.2f}, graph={s_graph:.2f}, "
            f"fresh={s_fresh:.2f}, conf={s_conf:.2f}, repo={s_repo:.2f}, "
            f"src={s_src:.2f}, cost={s_cost:.2f}"
        )

        return CandidateRankingScore(
            total_score=round(total, 4),
            semantic_relevance=round(s_sem, 4),
            lexical_relevance=round(s_lex, 4),
            graph_relevance=round(s_graph, 4),
            freshness=round(s_fresh, 4),
            confidence=round(s_conf, 4),
            repository_match=round(s_repo, 4),
            source_reliability=round(s_src, 4),
            token_cost=round(s_cost, 4),
            weights=w.to_dict(),
            explanation=explanation,
        )

    def rank(
        self,
        results: list[HybridRetrievalResult],
        intent_result: IntentAnalysisResult,
        query: str,
        repository_id: str | None = None,
    ) -> list[HybridRetrievalResult]:
        """Rank candidates using the configurable weighted multi-factor scoring formula."""
        if not results:
            return []

        # Determine active weights
        if self.intent_adaptive and intent_result is not None:
            active_weights = adapt_weights_for_intent(self.weights, intent_result.primary_intent)
        else:
            active_weights = self.weights

        scored_results: list[HybridRetrievalResult] = []

        for item in results:
            target_repo = repository_id or item.repository
            score_obj = self.score_candidate(
                candidate=item,
                query=query,
                target_repository_id=target_repo,
                active_weights=active_weights,
            )

            # Update scores_breakdown to expose all 8 individual scores
            updated_breakdown = dict(item.scores_breakdown)
            updated_breakdown.update({
                "semantic_relevance": score_obj.semantic_relevance,
                "lexical_relevance": score_obj.lexical_relevance,
                "graph_relevance": score_obj.graph_relevance,
                "freshness": score_obj.freshness,
                "confidence": score_obj.confidence,
                "repository_match": score_obj.repository_match,
                "source_reliability": score_obj.source_reliability,
                "token_cost": score_obj.token_cost,
                "total_score": score_obj.total_score,
            })

            # Expose complete score object and explanation in metadata
            updated_metadata = dict(item.metadata)
            updated_metadata["ranking_score"] = score_obj.to_dict()
            updated_metadata["ranking_explanation"] = score_obj.explanation

            ranked_item = HybridRetrievalResult(
                entity=item.entity,
                type=item.type,
                source=item.source,
                relationship_path=item.relationship_path,
                relevance_information=f"{item.relevance_information} [{score_obj.explanation}]",
                repository=item.repository,
                file_or_document=item.file_or_document,
                confidence=score_obj.confidence,
                score=score_obj.total_score,
                scores_breakdown=updated_breakdown,
                provenance=item.provenance,
                content_snippet=item.content_snippet,
                node_id=item.node_id,
                metadata=updated_metadata,
            )
            scored_results.append(ranked_item)

        # Sort descending by composite total score
        scored_results.sort(key=lambda r: r.score, reverse=True)
        return scored_results
