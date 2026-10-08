"""Token Optimization Engine for Synapse.

Provides deterministic, rule-based token budgeting and context optimization:
1. Estimates token costs accurately for queries, candidate context items, and rendered prompts.
2. Ranks context candidates by utility per token (density).
3. Detects and removes redundant items (lexical Jaccard overlap and structural subsumption).
4. Prefers concise, high-value context representations across multi-tier compaction.
5. Preserves graph relationships (SPECIFIES, CALLS, DEPENDS_ON, IMPORTS) necessary for understanding.
6. Strictly halts packing when the token budget is reached.

Returns comprehensive metrics including tokens saved, percentage reduction, and itemized logs.
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Any

from .models import (
    HybridRetrievalResult,
    OptimizedItemRecord,
    RelationshipHop,
    RemovedItemRecord,
    TokenOptimizationResult,
)
from .normalization import tokenize_name

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Context Representation Tiers
# ---------------------------------------------------------------------------

class ContextTier(str, Enum):
    """Level of detail for rendering a context item into prompt context."""

    FULL = "full"         # Complete details, file location, confidence, full snippet, graph paths
    COMPACT = "compact"   # Condensed summary, location, core signature, concise path (~40-50% tokens)
    MINIMAL = "minimal"   # Single-line bullet with essential symbol signature (~15-25% tokens)


# ---------------------------------------------------------------------------
# Token Estimator
# ---------------------------------------------------------------------------

class TokenEstimator:
    """Deterministic token estimation utility."""

    @staticmethod
    def estimate_text(text: str) -> int:
        """Estimate token count for a text string (approx. 4 characters per token)."""
        if not text:
            return 0
        clean = text.strip()
        if not clean:
            return 0
        # Blend character length and word count for accurate heuristic estimation
        char_tokens = len(clean) // 4
        word_tokens = int(len(clean.split()) * 1.25)
        return max(1, max(char_tokens, word_tokens))

    @classmethod
    def estimate_item(cls, item: HybridRetrievalResult, tier: ContextTier = ContextTier.FULL) -> int:
        """Estimate token cost of a candidate item at a given representation tier."""
        rendered = render_candidate_item(item, tier)
        return cls.estimate_text(rendered)


# ---------------------------------------------------------------------------
# Multi-Tier Item Rendering
# ---------------------------------------------------------------------------

def render_candidate_item(item: HybridRetrievalResult, tier: ContextTier = ContextTier.FULL) -> str:
    """Render a HybridRetrievalResult into Markdown according to the requested tier."""
    if tier == ContextTier.FULL:
        lines = [f"### [{item.type}] {item.entity}"]
        if item.file_or_document:
            lines.append(f"- Location: `{item.file_or_document}`")
        if item.confidence is not None:
            lines.append(f"- Confidence: {item.confidence:.2f}")
        if item.provenance and item.provenance.channels:
            lines.append(f"- Matched via: {', '.join(item.provenance.channels)}")
        if item.content_snippet:
            lines.append(f"- Details: {item.content_snippet.strip()}")
        if item.relationship_path:
            path_str = " -> ".join([h.format_hop() for h in item.relationship_path])
            lines.append(f"- Graph Path: {path_str}")
        return "\n".join(lines)

    elif tier == ContextTier.COMPACT:
        summary = item.content_snippet.strip()
        if len(summary) > 140:
            summary = summary[:137].rsplit(" ", 1)[0] + "..."
        lines = [f"- **[{item.type}] {item.entity}** (`{item.file_or_document}`)[score: {item.score:.2f}]"]
        if summary:
            lines.append(f"  - Summary: {summary}")
        if item.relationship_path:
            concise_hops = [f"{h.source_name} -[{h.relation}]-> {h.target_name}" for h in item.relationship_path[:2]]
            lines.append(f"  - Connections: {'; '.join(concise_hops)}")
        return "\n".join(lines)

    else:  # ContextTier.MINIMAL
        path_tag = ""
        if item.relationship_path:
            hop = item.relationship_path[0]
            path_tag = f" ({hop.source_name} -[{hop.relation}]-> {hop.target_name})"
        return f"- `{item.entity}` ({item.type}) in `{item.file_or_document}`{path_tag} [score: {item.score:.2f}]"


# ---------------------------------------------------------------------------
# Redundancy Detection
# ---------------------------------------------------------------------------

def calculate_jaccard_similarity(text1: str, text2: str) -> float:
    """Compute token-level Jaccard similarity between two text snippets."""
    if not text1 or not text2:
        return 0.0
    tokens1 = set(tokenize_name(text1.lower()))
    tokens2 = set(tokenize_name(text2.lower()))
    if not tokens1 or not tokens2:
        return 0.0
    intersection = tokens1.intersection(tokens2)
    union = tokens1.union(tokens2)
    return len(intersection) / len(union)


class RedundancyDetector:
    """Identifies duplicate or subsumed context candidates."""

    def __init__(self, jaccard_threshold: float = 0.70) -> None:
        self.jaccard_threshold = jaccard_threshold

    def check_redundancy(
        self,
        candidate: HybridRetrievalResult,
        accepted_items: list[HybridRetrievalResult],
    ) -> tuple[bool, str]:
        """Check if candidate is redundant relative to already accepted items."""
        cand_entity_lower = candidate.entity.lower().strip()
        cand_file_lower = candidate.file_or_document.lower().strip()
        cand_snippet = candidate.content_snippet or ""

        for existing in accepted_items:
            exist_entity_lower = existing.entity.lower().strip()
            exist_file_lower = existing.file_or_document.lower().strip()
            exist_snippet = existing.content_snippet or ""

            # 1. Exact node_id or entity/location duplicate
            if candidate.node_id and existing.node_id and candidate.node_id == existing.node_id:
                return True, f"duplicate_node_id: already covered by '{existing.entity}'"

            if cand_entity_lower == exist_entity_lower and cand_file_lower == exist_file_lower:
                return True, f"identical_entity_and_location: already covered by '{existing.entity}'"

            # 2. Structural Subsumption:
            # If an existing item is a class/module in the same file and candidate is a method/sub-element
            if cand_file_lower == exist_file_lower and ("class" in existing.type.lower() or "module" in existing.type.lower()):
                if cand_entity_lower in exist_snippet.lower():
                    return True, f"structural_subsumption: method/symbol contained in class '{existing.entity}'"

            # 3. High Lexical Content Overlap
            if cand_snippet and exist_snippet:
                sim = calculate_jaccard_similarity(cand_snippet, exist_snippet)
                if sim >= self.jaccard_threshold:
                    return True, f"high_content_overlap: {sim:.2f} Jaccard similarity with '{existing.entity}'"

        return False, ""


# ---------------------------------------------------------------------------
# Token Optimization Engine
# ---------------------------------------------------------------------------

class TokenOptimizationEngine:
    """Synapse Token Optimization Engine.

    Maximizes knowledge utility under token constraints by ranking candidates by
    utility-per-token density, removing redundancies, applying multi-tier compaction,
    preserving relational graph paths, and enforcing strict budget stopping.
    """

    def __init__(
        self,
        jaccard_threshold: float = 0.70,
        enable_tier_compaction: bool = True,
        header_budget_allowance: int = 50,
    ) -> None:
        self.redundancy_detector = RedundancyDetector(jaccard_threshold=jaccard_threshold)
        self.enable_tier_compaction = enable_tier_compaction
        self.header_budget_allowance = header_budget_allowance

    def compute_candidate_utility(
        self,
        item: HybridRetrievalResult,
        query: str,
    ) -> float:
        """Compute base utility score in [0.05, 1.25]."""
        base = max(0.05, float(item.score))

        # Direct query token match boost
        q_tokens = set(tokenize_name(query.lower()))
        item_tokens = set(tokenize_name(item.entity.lower()))
        if q_tokens.intersection(item_tokens):
            base *= 1.20

        # Graph connectivity boost
        if item.relationship_path:
            base *= 1.10

        return round(base, 4)

    def optimize(
        self,
        user_query: str,
        ranked_context_items: list[HybridRetrievalResult],
        max_token_budget: int,
        intent_result: IntentAnalysisResult | None = None,
    ) -> TokenOptimizationResult:
        """Execute deterministic token optimization.

        Args:
            user_query: The natural-language query or instruction.
            ranked_context_items: Pre-ranked context candidates from ACO retrieval.
            max_token_budget: Strict maximum allowed input tokens.
            intent_result: Optional analyzed intent result.

        Returns:
            TokenOptimizationResult with optimized context and audit metrics.
        """
        if not ranked_context_items or max_token_budget <= 0:
            return TokenOptimizationResult(
                optimized_context="",
                estimated_input_tokens=0,
                original_estimated_tokens=0,
                tokens_saved=0,
                percentage_reduction=0.0,
                items_removed=[],
                items_retained=[],
                metadata={"status": "empty_input_or_budget"},
            )

        # -------------------------------------------------------------------
        # Step 1: Calculate Original Unoptimized Tokens
        # -------------------------------------------------------------------
        unoptimized_blocks = [render_candidate_item(it, ContextTier.FULL) for it in ranked_context_items]
        raw_header = f"### Synapse Context (Query: {user_query})\n"
        original_estimated_tokens = TokenEstimator.estimate_text("\n\n".join([raw_header, *unoptimized_blocks]))

        # -------------------------------------------------------------------
        # Step 2: Rank by Utility-Per-Token Density
        # -------------------------------------------------------------------
        scored_candidates: list[tuple[HybridRetrievalResult, float, int, float]] = []
        for item in ranked_context_items:
            utility = self.compute_candidate_utility(item, user_query)
            full_tokens = TokenEstimator.estimate_item(item, ContextTier.FULL)
            utility_per_token = utility / max(1, full_tokens)
            scored_candidates.append((item, utility, full_tokens, utility_per_token))

        # Sort descending by utility per token density
        scored_candidates.sort(key=lambda t: t[3], reverse=True)

        # -------------------------------------------------------------------
        # Step 3: Redundancy Filtering & Knapsack Packing
        # -------------------------------------------------------------------
        accepted_items: list[HybridRetrievalResult] = []
        accepted_records: list[OptimizedItemRecord] = []
        removed_records: list[RemovedItemRecord] = []
        rendered_blocks: list[str] = []

        intent_label = f" (Intent: {intent_result.primary_intent.value.replace('_', ' ').title()})" if intent_result else ""
        header = f"### Synapse Engineering Memory Context{intent_label}\n> Query: `{user_query.strip()}` | Budget: {max_token_budget} tokens"
        used_tokens = TokenEstimator.estimate_text(header)
        rendered_blocks.append(header)

        # Track graph relationships across accepted items
        active_graph_hops: list[RelationshipHop] = []

        for item, utility, full_tokens, density in scored_candidates:
            # Check redundancy against accepted items
            is_redundant, redundancy_reason = self.redundancy_detector.check_redundancy(item, accepted_items)
            if is_redundant:
                removed_records.append(
                    RemovedItemRecord(
                        entity=item.entity,
                        type=item.type,
                        file_or_document=item.file_or_document,
                        reason=redundancy_reason,
                        original_tokens=full_tokens,
                        utility=utility,
                    )
                )
                continue

            # Remaining budget
            remaining_budget = max_token_budget - used_tokens
            if remaining_budget <= 5:
                removed_records.append(
                    RemovedItemRecord(
                        entity=item.entity,
                        type=item.type,
                        file_or_document=item.file_or_document,
                        reason="budget_exceeded: token budget reached",
                        original_tokens=full_tokens,
                        utility=utility,
                    )
                )
                continue

            # Evaluate multi-tier selection
            selected_tier: ContextTier | None = None
            selected_text: str = ""
            selected_tokens: int = 0

            # Tier 1: FULL representation
            full_text = render_candidate_item(item, ContextTier.FULL)
            cost_full = TokenEstimator.estimate_text(full_text)

            if cost_full <= remaining_budget:
                selected_tier = ContextTier.FULL
                selected_text = full_text
                selected_tokens = cost_full
            elif self.enable_tier_compaction:
                # Tier 2: COMPACT representation
                compact_text = render_candidate_item(item, ContextTier.COMPACT)
                cost_compact = TokenEstimator.estimate_text(compact_text)

                if cost_compact <= remaining_budget:
                    selected_tier = ContextTier.COMPACT
                    selected_text = compact_text
                    selected_tokens = cost_compact
                else:
                    # Tier 3: MINIMAL representation
                    minimal_text = render_candidate_item(item, ContextTier.MINIMAL)
                    cost_minimal = TokenEstimator.estimate_text(minimal_text)

                    if cost_minimal <= remaining_budget:
                        selected_tier = ContextTier.MINIMAL
                        selected_text = minimal_text
                        selected_tokens = cost_minimal

            if selected_tier is not None:
                accepted_items.append(item)
                rendered_blocks.append(selected_text)
                used_tokens += selected_tokens

                # Collect graph edges
                if item.relationship_path:
                    for h in item.relationship_path:
                        if not any(
                            ah.source_id == h.source_id and ah.relation == h.relation and ah.target_id == h.target_id
                            for ah in active_graph_hops
                        ):
                            active_graph_hops.append(h)

                item_upt = round(utility / max(1, selected_tokens), 4)
                accepted_records.append(
                    OptimizedItemRecord(
                        entity=item.entity,
                        type=item.type,
                        file_or_document=item.file_or_document,
                        tier=selected_tier.value,
                        tokens=selected_tokens,
                        utility=utility,
                        utility_per_token=item_upt,
                        relevance_information=item.relevance_information,
                    )
                )
            else:
                removed_records.append(
                    RemovedItemRecord(
                        entity=item.entity,
                        type=item.type,
                        file_or_document=item.file_or_document,
                        reason=f"budget_exceeded: required {cost_full} tokens, only {remaining_budget} left",
                        original_tokens=full_tokens,
                        utility=utility,
                    )
                )

        # -------------------------------------------------------------------
        # Step 4: Include Inter-Entity Graph Relationships
        # -------------------------------------------------------------------
        # If active relationships exist between accepted items and budget remains
        if active_graph_hops:
            remaining_for_graph = max_token_budget - used_tokens
            if remaining_for_graph >= 15:
                graph_lines = ["\n#### Relational Context & Traversal Paths"]
                for h in active_graph_hops:
                    hop_str = f"- `{h.source_name}` -[{h.relation}]-> `{h.target_name}`"
                    if h.confidence is not None:
                        hop_str += f" (conf: {h.confidence:.2f})"
                    graph_lines.append(hop_str)

                graph_section = "\n".join(graph_lines)
                graph_tokens = TokenEstimator.estimate_text(graph_section)
                if graph_tokens <= remaining_for_graph:
                    rendered_blocks.append(graph_section)
                    used_tokens += graph_tokens

        # -------------------------------------------------------------------
        # Step 5: Final Packaging & Metrics
        # -------------------------------------------------------------------
        optimized_context = "\n\n".join(rendered_blocks).strip()
        final_input_tokens = TokenEstimator.estimate_text(optimized_context)
        tokens_saved = max(0, original_estimated_tokens - final_input_tokens)

        if original_estimated_tokens > 0:
            percentage_reduction = round((tokens_saved / original_estimated_tokens) * 100.0, 2)
        else:
            percentage_reduction = 0.0

        metadata: dict[str, Any] = {
            "query": user_query,
            "max_token_budget": max_token_budget,
            "used_tokens": final_input_tokens,
            "budget_utilization_pct": round((final_input_tokens / max(1, max_token_budget)) * 100.0, 2),
            "total_candidates_evaluated": len(ranked_context_items),
            "items_retained_count": len(accepted_records),
            "items_removed_count": len(removed_records),
            "active_graph_hops_count": len(active_graph_hops),
        }

        return TokenOptimizationResult(
            optimized_context=optimized_context,
            estimated_input_tokens=final_input_tokens,
            original_estimated_tokens=original_estimated_tokens,
            tokens_saved=tokens_saved,
            percentage_reduction=percentage_reduction,
            items_removed=[r.to_dict() for r in removed_records],
            items_retained=[r.to_dict() for r in accepted_records],
            metadata=metadata,
        )


# ---------------------------------------------------------------------------
# Convenience Standalone Function
# ---------------------------------------------------------------------------

def optimize_tokens(
    user_query: str,
    ranked_context_items: list[HybridRetrievalResult],
    max_token_budget: int,
    *,
    intent_result: IntentAnalysisResult | None = None,
    jaccard_threshold: float = 0.70,
    enable_tier_compaction: bool = True,
) -> TokenOptimizationResult:
    """Convenience function to run the Synapse Token Optimization Engine."""
    optimizer = TokenOptimizationEngine(
        jaccard_threshold=jaccard_threshold,
        enable_tier_compaction=enable_tier_compaction,
    )
    return optimizer.optimize(
        user_query=user_query,
        ranked_context_items=ranked_context_items,
        max_token_budget=max_token_budget,
        intent_result=intent_result,
    )
