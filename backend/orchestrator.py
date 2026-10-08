"""Adaptive Context Orchestrator (ACO) for Adaptive Engineering Memory.

The ACO receives:
- user query
- user identity
- repository context
- optional token budget

And executes a 7-step orchestrated pipeline:
1. Analyze query intent (deterministic rule-based classification across 8 core intents).
2. Select appropriate knowledge sources and retrieval strategy based on intent.
3. Execute retrieval using the hybrid retrieval layer (lexical, semantic, graph).
4. Rank retrieved information with intent-affinity boosts.
5. Remove duplicate and redundant items while preserving rich multi-hop provenance.
6. Construct an optimized, budgeted context suitable for LLM prompt ingestion.
7. Return the context plus detailed retrieval metadata.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from typing import Any

from .hybrid_retrieval import HybridRetrievalService
from .models import (
    CandidateRankingScore,
    CodeSymbol,
    DocumentEntity,
    GraphEdge,
    HybridRetrievalResult,
    IntentAnalysisResult,
    Memory,
    OptimizationMode,
    OrchestratedContext,
    QueryIntent,
    RankingWeights,
    RelationshipHop,
)
from .normalization import tokenize_name
from .ranking import (
    BaseContextRanker,
    ConfigurableContextRanker,
    DEFAULT_SOURCE_RELIABILITY_MAP,
    compute_confidence,
    compute_freshness,
    compute_graph_relevance,
    compute_lexical_relevance,
    compute_repository_match,
    compute_semantic_relevance,
    compute_source_reliability,
    compute_token_cost_efficiency,
)
from .token_optimizer import BaseLLMCompressor, TokenOptimizationEngine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default Intent Strategy Matrix
# ---------------------------------------------------------------------------

DEFAULT_INTENT_STRATEGIES: dict[QueryIntent, dict[str, Any]] = {
    QueryIntent.CODE_NAVIGATION: {
        "lexical_weight": 0.65,
        "semantic_weight": 0.25,
        "graph_weight": 0.10,
        "recency_weight": 0.05,
        "max_hops": 1,
        "limit": 10,
        "preferred_sources": ["code_symbols", "modules", "file_locations"],
        "reasoning": "Prioritizing exact identifier lexical search and definition locations.",
    },
    QueryIntent.EXPLANATION: {
        "lexical_weight": 0.30,
        "semantic_weight": 0.45,
        "graph_weight": 0.25,
        "recency_weight": 0.10,
        "max_hops": 2,
        "limit": 12,
        "preferred_sources": ["document_entities", "code_symbols", "specifications"],
        "reasoning": "Balancing semantic conceptual search with specifications and implementation symbols.",
    },
    QueryIntent.DEBUGGING: {
        "lexical_weight": 0.45,
        "semantic_weight": 0.30,
        "graph_weight": 0.25,
        "recency_weight": 0.15,
        "max_hops": 2,
        "limit": 12,
        "preferred_sources": ["code_symbols", "call_graphs", "error_specs"],
        "reasoning": "Focusing on error tokens, execution call paths, and failing symbol dependencies.",
    },
    QueryIntent.FEATURE_IMPLEMENTATION: {
        "lexical_weight": 0.35,
        "semantic_weight": 0.40,
        "graph_weight": 0.25,
        "recency_weight": 0.10,
        "max_hops": 2,
        "limit": 12,
        "preferred_sources": ["code_symbols", "document_entities", "specifications", "dependencies"],
        "reasoning": "Retrieving domain patterns, design specifications, and extensible interfaces.",
    },
    QueryIntent.REFACTORING: {
        "lexical_weight": 0.30,
        "semantic_weight": 0.25,
        "graph_weight": 0.45,
        "recency_weight": 0.10,
        "max_hops": 2,
        "limit": 15,
        "preferred_sources": ["call_graphs", "dependencies", "code_symbols"],
        "reasoning": "Emphasizing graph traversal to analyze callers, importers, and downstream blast radius.",
    },
    QueryIntent.ARCHITECTURE: {
        "lexical_weight": 0.20,
        "semantic_weight": 0.45,
        "graph_weight": 0.35,
        "recency_weight": 0.10,
        "max_hops": 3,
        "limit": 15,
        "preferred_sources": ["document_entities", "modules", "specifications", "structural_hierarchy"],
        "reasoning": "Broad traversal across module hierarchies, architecture specs, and dependency graphs.",
    },
    QueryIntent.DOCUMENTATION: {
        "lexical_weight": 0.40,
        "semantic_weight": 0.40,
        "graph_weight": 0.20,
        "recency_weight": 0.10,
        "max_hops": 1,
        "limit": 12,
        "preferred_sources": ["document_entities", "docstrings", "code_symbols", "specifications"],
        "reasoning": "Prioritizing existing documentation chunks, docstrings, and SPECIFIES links.",
    },
    QueryIntent.SESSION_CONTINUATION: {
        "lexical_weight": 0.25,
        "semantic_weight": 0.30,
        "graph_weight": 0.20,
        "recency_weight": 0.35,
        "max_hops": 2,
        "limit": 10,
        "preferred_sources": ["recent_memory", "document_entities", "code_symbols"],
        "reasoning": "Boosting recent modifications, session decisions, and active work context.",
    },
}


# ---------------------------------------------------------------------------
# 1. Intent Classification Interface & Implementation
# ---------------------------------------------------------------------------

class BaseIntentClassifier(ABC):
    """Abstract interface for query intent classification."""

    @abstractmethod
    def classify(self, query: str, context: dict[str, Any] | None = None) -> IntentAnalysisResult:
        """Classify user query into an IntentAnalysisResult."""
        pass


class RuleBasedIntentClassifier(BaseIntentClassifier):
    """Deterministic, rule-based intent classifier with pattern heuristics and entity extraction."""

    # Patterns for each of the 8 supported intents
    _INTENT_PATTERNS: dict[QueryIntent, list[re.Pattern]] = {
        QueryIntent.CODE_NAVIGATION: [
            re.compile(r"\b(?:where\s+(?:is|are|does)|locate|find\s+(?:the\s+)?(?:symbol|definition|function|class|method|file|code)|go\s*to|goto|jump\s*to|definition\s*of|declaration\s*of|path\s+(?:of|to))\b", re.IGNORECASE),
            re.compile(r"\bwhere\s+.*?\s+(?:lives?|defined|located)\b", re.IGNORECASE),
            re.compile(r"\bfile\s+for\b", re.IGNORECASE),
        ],
        QueryIntent.EXPLANATION: [
            re.compile(r"\b(?:how\s+does\s+.*?work|explain(?:\s+me)?|what\s+does\s+.*?do|how\s+is\s+.*?implemented|walk\s*(?:me\s*)?through|tell\s+me\s+about|why\s+does|overview\s+of|understand|concept\s+of)\b", re.IGNORECASE),
        ],
        QueryIntent.DEBUGGING: [
            re.compile(r"\b(?:fix|bug|error|exception|traceback|crash|stack\s*trace|failing|broken|why\s+is\s+.*?failing|debug|issue\s+with|nullpointer|typeerror|valueerror|runtimeerror|syntaxerror|assertionerror|timeout)\b", re.IGNORECASE),
            re.compile(r"\b(?:throws?|failed\s+with|not\s+working)\b", re.IGNORECASE),
        ],
        QueryIntent.FEATURE_IMPLEMENTATION: [
            re.compile(r"\b(?:implement|add\s+(?:a\s+|new\s+)?feature|create\s+(?:a\s+|new\s+)?(?:endpoint|service|class|function|module|component)|build|new\s+endpoint|support\s+for|write\s+(?:a\s+|new\s+)|integrate|develop)\b", re.IGNORECASE),
            re.compile(r"\b(?:how\s+to\s+add|how\s+can\s+i\s+implement)\b", re.IGNORECASE),
        ],
        QueryIntent.REFACTORING: [
            re.compile(r"\b(?:refactor|clean\s*up|restructure|rename|extract\s+(?:method|class|function)|simplify|decouple|modernize|rewrite|dead\s+code|code\s+smell)\b", re.IGNORECASE),
            re.compile(r"\b(?:deprecate|remove\s+dependency|reorganize)\b", re.IGNORECASE),
        ],
        QueryIntent.ARCHITECTURE: [
            re.compile(r"\b(?:architecture|system\s+design|high[\s\-]level(?:\s+design)?|topology|components\s+overview|modules\s+overview|data\s+flow|overall\s+structure|dependency\s+diagram|layered\s+architecture|subsystems)\b", re.IGNORECASE),
            re.compile(r"\b(?:design\s+pattern|system\s+overview)\b", re.IGNORECASE),
        ],
        QueryIntent.DOCUMENTATION: [
            re.compile(r"\b(?:document|documentation|docstring|readme|api\s+docs|swagger|openapi|generate\s+docs|specs?\s+(?:for|of)|specification\s+(?:for|of))\b", re.IGNORECASE),
            re.compile(r"\b(?:how\s+to\s+document|write\s+docs)\b", re.IGNORECASE),
        ],
        QueryIntent.SESSION_CONTINUATION: [
            re.compile(r"\b(?:continue|next\s+step|resume|what\s+was\s+i\s+doing|what\s+were\s+we\s+doing|as\s+we\s+discussed|carry\s+on|last\s+time|previous\s+task|keep\s+going|where\s+did\s+we\s+leave\s+off)\b", re.IGNORECASE),
            re.compile(r"\b(?:what's\s+next|next\s+action)\b", re.IGNORECASE),
        ],
    }

    # Entity detection regexes
    _PASCAL_CAMEL_RE = re.compile(r"\b(?:[A-Z][a-zA-Z0-9]+|[a-z]+[A-Z][a-zA-Z0-9]*)\b")
    _SNAKE_CASE_RE = re.compile(r"\b[a-z0-9]+_[a-z0-9_]+\b")
    _FILE_PATH_RE = re.compile(r"\b[\w\-\.\/]+\.(?:py|ts|js|jsx|tsx|json|md|yaml|yml|html|css)\b")
    _STOP_WORDS = {
        "how", "what", "where", "when", "why", "which", "who", "does", "do", "is", "are",
        "the", "a", "an", "in", "on", "for", "with", "about", "to", "from", "of", "and", "or"
    }

    def __init__(self, strategies: dict[QueryIntent, dict[str, Any]] | None = None) -> None:
        self.strategies = strategies or DEFAULT_INTENT_STRATEGIES

    def extract_entities(self, query: str) -> list[str]:
        """Extract candidate symbol names, file paths, and identifiers from query text."""
        entities: list[str] = []
        # 1. File paths
        for m in self._FILE_PATH_RE.findall(query):
            if m not in entities:
                entities.append(m)

        # 2. PascalCase & camelCase
        for m in self._PASCAL_CAMEL_RE.findall(query):
            if m.lower() not in self._STOP_WORDS and m not in entities:
                entities.append(m)

        # 3. snake_case
        for m in self._SNAKE_CASE_RE.findall(query):
            if m.lower() not in self._STOP_WORDS and m not in entities:
                entities.append(m)

        return entities

    def classify(self, query: str, context: dict[str, Any] | None = None) -> IntentAnalysisResult:
        """Classify query intent deterministically."""
        clean_query = query.strip()
        if not clean_query:
            strategy = self.strategies[QueryIntent.EXPLANATION]
            return IntentAnalysisResult(
                primary_intent=QueryIntent.EXPLANATION,
                confidence=0.5,
                secondary_intents=[],
                extracted_entities=[],
                reasoning="Empty query defaulted to explanation.",
                selected_sources=strategy.get("preferred_sources", []),
                retrieval_strategy=dict(strategy),
            )

        extracted_entities = self.extract_entities(clean_query)
        intent_scores: dict[QueryIntent, float] = {intent: 0.0 for intent in QueryIntent}

        # 1. Evaluate regex patterns with domain specificity weighting
        weights = {
            QueryIntent.ARCHITECTURE: 1.5,
            QueryIntent.REFACTORING: 1.5,
            QueryIntent.DEBUGGING: 1.5,
            QueryIntent.FEATURE_IMPLEMENTATION: 1.5,
            QueryIntent.CODE_NAVIGATION: 1.4,
            QueryIntent.DOCUMENTATION: 1.4,
            QueryIntent.SESSION_CONTINUATION: 1.4,
            QueryIntent.EXPLANATION: 1.0,
        }

        for intent, patterns in self._INTENT_PATTERNS.items():
            base_w = weights.get(intent, 1.0)
            for pat in patterns:
                matches = pat.findall(clean_query)
                if matches:
                    intent_scores[intent] += base_w * len(matches)

        # 2. Structural heuristics
        # Single symbol query (e.g. "AuthService" or "user_repository.py")
        tokens = tokenize_name(clean_query)
        if len(tokens) <= 2 and extracted_entities:
            intent_scores[QueryIntent.CODE_NAVIGATION] += 1.6

        # Question without explicit domain pattern ("How does ...", "What is ...")
        if re.match(r"^\s*(?:how|what|why)\b", clean_query, re.IGNORECASE) and max(intent_scores.values()) == 0.0:
            intent_scores[QueryIntent.EXPLANATION] += 1.0

        # 3. Sort scored intents
        scored_intents = sorted(intent_scores.items(), key=lambda item: item[1], reverse=True)
        top_intent, top_raw_score = scored_intents[0]

        if top_raw_score <= 0.0:
            # Fallback when no pattern matched
            if extracted_entities:
                top_intent = QueryIntent.CODE_NAVIGATION
                confidence = 0.55
                reasoning = f"No explicit command verb found; classified as code navigation based on detected entity '{extracted_entities[0]}'."
            else:
                top_intent = QueryIntent.EXPLANATION
                confidence = 0.50
                reasoning = "General query with no explicit intent trigger; defaulted to explanation."
            secondary_intents = []
        else:
            # Normalize confidence: scale top score to [0.65, 0.95]
            confidence = min(0.95, 0.65 + (top_raw_score * 0.10))
            reasoning = f"Detected '{top_intent.value}' based on pattern matches (score: {top_raw_score:.1f})."
            if extracted_entities:
                reasoning += f" Extracted candidate entities: {', '.join(extracted_entities)}."

            # Calculate secondary intents (relative threshold)
            secondary_intents = []
            for intent, raw_score in scored_intents[1:]:
                if raw_score >= 0.8:
                    sec_conf = round(min(0.85, 0.40 + (raw_score * 0.10)), 4)
                    secondary_intents.append((intent, sec_conf))

        strategy = self.strategies.get(top_intent, self.strategies[QueryIntent.EXPLANATION])
        selected_sources = strategy.get("preferred_sources", [])

        return IntentAnalysisResult(
            primary_intent=top_intent,
            confidence=round(confidence, 4),
            secondary_intents=secondary_intents,
            extracted_entities=extracted_entities,
            reasoning=reasoning,
            selected_sources=selected_sources,
            retrieval_strategy=dict(strategy),
        )


# ---------------------------------------------------------------------------
# 2. Ranking & Intent Affinity Boosting Implementation
# ---------------------------------------------------------------------------

class IntentAwareContextRanker(BaseContextRanker):
    """Re-ranks hybrid retrieval candidates applying intent-affinity boosts."""

    def rank(
        self,
        results: list[HybridRetrievalResult],
        intent_result: IntentAnalysisResult,
        query: str,
        repository_id: str | None = None,
    ) -> list[HybridRetrievalResult]:
        """Apply intent-specific scoring bonuses and sort results."""
        if not results:
            return []

        intent = intent_result.primary_intent
        extracted = set(e.lower() for e in intent_result.extracted_entities)
        query_lower = query.lower()

        re_scored: list[HybridRetrievalResult] = []

        for r in results:
            boost = 0.0
            entity_name_lower = r.entity.lower()

            # Exact symbol name match boost
            if entity_name_lower in extracted or entity_name_lower in query_lower:
                boost += 0.25

            # Intent-specific affinity boosts
            if intent == QueryIntent.CODE_NAVIGATION:
                if r.source == "code" or "CodeSymbol" in r.type:
                    boost += 0.25
                if r.file_or_document and any(ext in r.file_or_document for ext in (".py", ".ts", ".js")):
                    boost += 0.10

            elif intent == QueryIntent.EXPLANATION:
                if r.source == "documentation" or "DocumentEntity" in r.type:
                    boost += 0.20
                if any(h.relation == "SPECIFIES" for h in r.relationship_path):
                    boost += 0.25

            elif intent == QueryIntent.DEBUGGING:
                if r.source == "code":
                    boost += 0.15
                if any(h.relation in ("CALLS", "DEPENDS_ON") for h in r.relationship_path):
                    boost += 0.30

            elif intent == QueryIntent.FEATURE_IMPLEMENTATION:
                if any(h.relation == "SPECIFIES" for h in r.relationship_path):
                    boost += 0.20
                if r.source == "code":
                    boost += 0.15

            elif intent == QueryIntent.REFACTORING:
                # Heavy boost for items with active graph relationships (callers/dependents)
                if len(r.relationship_path) > 0:
                    boost += 0.35
                if any(h.relation in ("CALLS", "IMPORTS", "DEPENDS_ON") for h in r.relationship_path):
                    boost += 0.20

            elif intent == QueryIntent.ARCHITECTURE:
                if "Document" in r.type or "Module" in r.type:
                    boost += 0.30
                if any(h.relation in ("DEPENDS_ON", "IMPORTS", "SPECIFIES") for h in r.relationship_path):
                    boost += 0.25

            elif intent == QueryIntent.DOCUMENTATION:
                if r.source == "documentation" or "DocumentEntity" in r.type:
                    boost += 0.30
                if any(h.relation == "SPECIFIES" for h in r.relationship_path):
                    boost += 0.25

            elif intent == QueryIntent.SESSION_CONTINUATION:
                # Recency or memory boost
                if r.scores_breakdown.get("recency_score", 0.0) > 0.0:
                    boost += 0.35
                if "RFC" in r.entity or "recent" in r.relevance_information.lower():
                    boost += 0.20

            capped_boost = min(boost, 0.75)
            adjusted_score = round(r.score * (1.0 + capped_boost), 4)

            # Record breakdown of intent boost
            updated_breakdown = dict(r.scores_breakdown)
            updated_breakdown["intent_boost"] = round(capped_boost, 4)

            updated_result = HybridRetrievalResult(
                entity=r.entity,
                type=r.type,
                source=r.source,
                relationship_path=r.relationship_path,
                relevance_information=r.relevance_information,
                repository=r.repository,
                file_or_document=r.file_or_document,
                confidence=r.confidence,
                score=adjusted_score,
                scores_breakdown=updated_breakdown,
                provenance=r.provenance,
                content_snippet=r.content_snippet,
                node_id=r.node_id,
                metadata=r.metadata,
            )
            re_scored.append(updated_result)

        # Sort descending by adjusted score
        re_scored.sort(key=lambda item: item.score, reverse=True)
        return re_scored


# ---------------------------------------------------------------------------
# 3. Deduplication Logic
# ---------------------------------------------------------------------------

def deduplicate_results(results: list[HybridRetrievalResult]) -> list[HybridRetrievalResult]:
    """Remove duplicates while preserving richest provenance, paths, and highest scores."""
    if not results:
        return []

    deduped: dict[str, HybridRetrievalResult] = {}

    for r in results:
        # Determine unique key
        if r.node_id:
            key = f"nid:{r.node_id}"
        else:
            key = f"ent:{r.entity.lower()}:{r.type.lower()}:{r.file_or_document.lower()}"

        if key not in deduped:
            deduped[key] = r
        else:
            existing = deduped[key]
            # Merge paths, keeping unique hops
            combined_paths: list[RelationshipHop] = list(existing.relationship_path)
            seen_hops = {
                (h.source_name, h.relation, h.target_name) for h in combined_paths
            }
            for hop in r.relationship_path:
                hop_key = (hop.source_name, hop.relation, hop.target_name)
                if hop_key not in seen_hops:
                    combined_paths.append(hop)
                    seen_hops.add(hop_key)

            # Choose highest score
            best_score = max(existing.score, r.score)
            best_conf = (
                max(existing.confidence or 0.0, r.confidence or 0.0)
                if (existing.confidence or r.confidence)
                else None
            )

            # Choose longest/richest content snippet
            best_snippet = (
                r.content_snippet
                if len(r.content_snippet or "") > len(existing.content_snippet or "")
                else existing.content_snippet
            )

            # Merge score breakdowns
            merged_breakdown = dict(existing.scores_breakdown)
            for k, v in r.scores_breakdown.items():
                merged_breakdown[k] = max(merged_breakdown.get(k, 0.0), v)

            merged_meta = dict(existing.metadata)
            merged_meta.update(r.metadata)
            merged_meta["merged_occurrences"] = merged_meta.get("merged_occurrences", 1) + 1

            deduped[key] = HybridRetrievalResult(
                entity=existing.entity,
                type=existing.type,
                source=existing.source,
                relationship_path=combined_paths,
                relevance_information=existing.relevance_information or r.relevance_information,
                repository=existing.repository or r.repository,
                file_or_document=existing.file_or_document or r.file_or_document,
                confidence=best_conf,
                score=best_score,
                scores_breakdown=merged_breakdown,
                provenance=existing.provenance,
                content_snippet=best_snippet,
                node_id=existing.node_id or r.node_id,
                metadata=merged_meta,
            )

    return list(deduped.values())


# ---------------------------------------------------------------------------
# 4. Context Packing & Token Budgeting Interface & Implementation
# ---------------------------------------------------------------------------

class BaseContextPacker(ABC):
    """Abstract interface for constructing optimized prompt context."""

    @abstractmethod
    def pack(
        self,
        results: list[HybridRetrievalResult],
        intent_result: IntentAnalysisResult,
        token_budget: int | None = None,
    ) -> tuple[str, int]:
        """Construct structured markdown context respecting token budget. Returns (text, estimated_tokens)."""
        pass


class AdaptiveContextPacker(BaseContextPacker):
    """Assembles structured, sectioned Markdown context budgeted to token limits."""

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Estimate token count (approx. 4 characters per token)."""
        if not text:
            return 0
        return max(1, len(text) // 4)

    def pack(
        self,
        results: list[HybridRetrievalResult],
        intent_result: IntentAnalysisResult,
        token_budget: int | None = None,
    ) -> tuple[str, int]:
        """Construct organized prompt context under token budget."""
        intent = intent_result.primary_intent.value.replace("_", " ").title()

        # Build header
        header_lines = [
            f"### Engineering Memory Context (Intent: {intent})",
            f"> Confidence: {intent_result.confidence:.2f} | Strategy: {intent_result.reasoning}",
        ]
        if intent_result.secondary_intents:
            sec_str = ", ".join(f"{si[0].value} ({si[1]:.2f})" for si in intent_result.secondary_intents)
            header_lines.append(f"> Secondary Intents: {sec_str}")

        header_text = "\n".join(header_lines)
        header_tokens = self.estimate_tokens(header_text)

        # Categorize results into sections
        specs_and_docs: list[HybridRetrievalResult] = []
        code_symbols: list[HybridRetrievalResult] = []
        session_and_team_memories: list[HybridRetrievalResult] = []
        graph_relationships: list[HybridRetrievalResult] = []
        other_results: list[HybridRetrievalResult] = []

        for r in results:
            if r.relationship_path:
                graph_relationships.append(r)
            elif r.source in ("session_memory", "team_memory") or r.type.startswith("Memory"):
                session_and_team_memories.append(r)
            elif r.source == "documentation" or "Document" in r.type:
                specs_and_docs.append(r)
            elif r.source == "code" or "CodeSymbol" in r.type:
                code_symbols.append(r)
            else:
                other_results.append(r)

        sections: list[tuple[str, list[HybridRetrievalResult]]] = [
            ("Engineering Decisions & AI Session Memory", session_and_team_memories),
            ("Architecture & Specifications", specs_and_docs),
            ("Code Symbols & Interfaces", code_symbols),
            ("System Relationships & Traversal Paths", graph_relationships),
            ("Additional Engineering Context", other_results),
        ]

        # Assemble content respecting token budget
        context_blocks = [header_text]
        used_tokens = header_tokens

        for section_title, section_items in sections:
            if not section_items:
                continue

            section_header = f"\n#### {section_title}"
            section_header_tokens = self.estimate_tokens(section_header)

            if token_budget is not None and (used_tokens + section_header_tokens >= token_budget):
                break

            items_in_section: list[str] = []

            for item in section_items:
                block = item.to_context_str()
                block_tokens = self.estimate_tokens(block)

                if token_budget is not None:
                    if used_tokens + block_tokens > token_budget:
                        # Attempt compact one-line representation
                        compact = f"- `{item.entity}` ({item.type}) in `{item.file_or_document}` [score: {item.score:.2f}]"
                        compact_tokens = self.estimate_tokens(compact)
                        if used_tokens + compact_tokens <= token_budget:
                            items_in_section.append(compact)
                            used_tokens += compact_tokens
                        break
                    else:
                        items_in_section.append(block)
                        used_tokens += block_tokens
                else:
                    items_in_section.append(block)
                    used_tokens += block_tokens

            if items_in_section:
                context_blocks.append(section_header)
                used_tokens += section_header_tokens
                context_blocks.extend(items_in_section)

        final_context = "\n\n".join(context_blocks).strip()
        final_tokens = self.estimate_tokens(final_context)
        return final_context, final_tokens


class TokenOptimizedContextPacker(BaseContextPacker):
    """Context packer leveraging Synapse Token Optimization Engine for multi-tier knapsack packing or LLM compression."""

    def __init__(
        self,
        optimizer: TokenOptimizationEngine | None = None,
        mode: OptimizationMode | str = OptimizationMode.DETERMINISTIC,
        llm_compressor: BaseLLMCompressor | None = None,
    ) -> None:
        self.optimizer = optimizer or TokenOptimizationEngine()
        self.mode = mode
        self.llm_compressor = llm_compressor

    def pack(
        self,
        results: list[HybridRetrievalResult],
        intent_result: IntentAnalysisResult,
        token_budget: int | None = None,
    ) -> tuple[str, int]:
        """Pack context using the TokenOptimizationEngine in the configured mode."""
        budget = token_budget if token_budget is not None else 4000
        query_text = (
            ", ".join(intent_result.extracted_entities)
            if intent_result.extracted_entities
            else intent_result.primary_intent.value
        )
        opt_res = self.optimizer.optimize(
            user_query=query_text,
            ranked_context_items=results,
            max_token_budget=budget,
            intent_result=intent_result,
            mode=self.mode,
            llm_compressor=self.llm_compressor,
        )
        return opt_res.optimized_context, opt_res.estimated_input_tokens


# ---------------------------------------------------------------------------
# 5. Adaptive Context Orchestrator Service
# ---------------------------------------------------------------------------

class AdaptiveContextOrchestrator:
    """The Adaptive Context Orchestrator coordinates intent analysis, hybrid retrieval,

    intent-affinity ranking, deduplication, and token-budgeted prompt context packing.
    """

    def __init__(
        self,
        retrieval_service: HybridRetrievalService | None = None,
        classifier: BaseIntentClassifier | None = None,
        ranker: BaseContextRanker | None = None,
        packer: BaseContextPacker | None = None,
        token_optimizer: TokenOptimizationEngine | None = None,
        session_memory_service: Any | None = None,
    ) -> None:
        self.retrieval_service = retrieval_service or HybridRetrievalService()
        self.classifier = classifier or RuleBasedIntentClassifier()
        self.ranker = ranker or ConfigurableContextRanker()
        self.packer = packer or AdaptiveContextPacker()
        self.token_optimizer = token_optimizer or TokenOptimizationEngine()
        self.session_memory_service = session_memory_service

    def orchestrate(
        self,
        query: str,
        user_id: str,
        repository_id: str,
        *,
        max_tokens: int | None = None,
        symbols: list[CodeSymbol] | None = None,
        entities: list[DocumentEntity] | None = None,
        edges: list[GraphEdge] | None = None,
        memories: list[Memory] | None = None,
        session_memory_service: Any | None = None,
        optimization_mode: OptimizationMode | str = OptimizationMode.DETERMINISTIC,
        llm_compressor: BaseLLMCompressor | None = None,
    ) -> OrchestratedContext:
        """Execute full ACO pipeline:

        1. Analyze query intent
        2. Select knowledge sources & strategy
        3. Execute hybrid retrieval (including AI session memories)
        4. Rank retrieved information
        5. Remove duplicates
        6. Construct optimized context
        7. Return context plus metadata
        """
        # Step 1: Analyze query intent
        intent_result = self.classifier.classify(query)

        # Step 2: Select retrieval strategy from intent
        strategy = intent_result.retrieval_strategy

        # Step 3: Execute hybrid retrieval
        query_response = self.retrieval_service.retrieve(
            user_id=user_id,
            repository_id=repository_id,
            query=query,
            limit=strategy.get("limit", 12),
            max_hops=strategy.get("max_hops", 2),
            lexical_weight=strategy.get("lexical_weight", 0.35),
            semantic_weight=strategy.get("semantic_weight", 0.40),
            graph_weight=strategy.get("graph_weight", 0.25),
            recency_weight=strategy.get("recency_weight", 0.10),
            symbols=symbols,
            entities=entities,
            edges=edges,
        )

        session_mem_results: list[HybridRetrievalResult] = []
        if memories is not None:
            query_terms = [t.lower() for t in query.split() if len(t) > 2]
            for m in memories:
                m_text = f"{m.title} {m.content}".lower()
                matches = sum(1 for t in query_terms if t in m_text)
                term_score = matches / max(1, len(query_terms))
                score = round(0.55 + 0.45 * term_score * m.confidence, 4)
                session_mem_results.append(m.to_hybrid_result(score=score))
        else:
            svc = session_memory_service or self.session_memory_service
            if svc is not None and hasattr(svc, "retrieve_for_aco"):
                session_mem_results = svc.retrieve_for_aco(
                    query=query,
                    user_id=user_id,
                    repository_id=repository_id,
                    limit=strategy.get("limit", 10),
                )

        combined_candidates = [*query_response.results, *session_mem_results]
        total_retrieved = len(combined_candidates)

        # Step 4: Rank retrieved information with configurable ranking
        try:
            ranked_results = self.ranker.rank(
                results=combined_candidates,
                intent_result=intent_result,
                query=query,
                repository_id=repository_id,
            )
        except TypeError:
            ranked_results = self.ranker.rank(
                results=combined_candidates,
                intent_result=intent_result,
                query=query,
            )

        # Step 5: Remove duplicates
        deduped_results = deduplicate_results(ranked_results)
        total_deduped = len(deduped_results)

        # Step 6: Construct optimized context
        token_opt_dict: dict[str, Any] | None = None
        if max_tokens is not None and self.token_optimizer is not None:
            opt_res = self.token_optimizer.optimize(
                user_query=query,
                ranked_context_items=deduped_results,
                max_token_budget=max_tokens,
                intent_result=intent_result,
                mode=optimization_mode,
                llm_compressor=llm_compressor,
            )
            token_opt_dict = opt_res.to_dict()

        context_text, estimated_tokens = self.packer.pack(
            results=deduped_results,
            intent_result=intent_result,
            token_budget=max_tokens,
        )

        # Step 7: Return context plus detailed retrieval metadata
        ranking_weights = (
            self.ranker.weights.to_dict()
            if hasattr(self.ranker, "weights") and hasattr(self.ranker.weights, "to_dict")
            else {}
        )

        retrieval_metadata: dict[str, Any] = {
            "intent_detected": intent_result.primary_intent.value,
            "intent_confidence": intent_result.confidence,
            "intent_reasoning": intent_result.reasoning,
            "secondary_intents": [
                {"intent": si[0].value, "confidence": si[1]}
                for si in intent_result.secondary_intents
            ],
            "extracted_entities": intent_result.extracted_entities,
            "selected_sources": intent_result.selected_sources,
            "retrieval_strategy": strategy,
            "ranking_weights": ranking_weights,
            "total_candidates_retrieved": total_retrieved,
            "total_candidates_after_dedup": total_deduped,
            "token_budget": max_tokens,
            "estimated_tokens": estimated_tokens,
            "channel_stats": query_response.retrieval_stats,
            "token_optimization": token_opt_dict,
        }

        return OrchestratedContext(
            context_text=context_text,
            query=query,
            user_id=user_id,
            repository_id=repository_id,
            intent=intent_result,
            token_budget=max_tokens,
            estimated_tokens=estimated_tokens,
            retrieved_results=deduped_results,
            retrieval_metadata=retrieval_metadata,
        )


# ---------------------------------------------------------------------------
# Convenience Standalone Function
# ---------------------------------------------------------------------------

def orchestrate_context(
    query: str,
    user_id: str,
    repository_id: str,
    *,
    max_tokens: int | None = None,
    symbols: list[CodeSymbol] | None = None,
    entities: list[DocumentEntity] | None = None,
    edges: list[GraphEdge] | None = None,
    retrieval_service: HybridRetrievalService | None = None,
    classifier: BaseIntentClassifier | None = None,
    ranker: BaseContextRanker | None = None,
    packer: BaseContextPacker | None = None,
    token_optimizer: TokenOptimizationEngine | None = None,
    memories: list[Memory] | None = None,
    session_memory_service: Any | None = None,
    optimization_mode: OptimizationMode | str = OptimizationMode.DETERMINISTIC,
    llm_compressor: BaseLLMCompressor | None = None,
) -> OrchestratedContext:
    """Convenience function to run the Adaptive Context Orchestrator."""
    orchestrator = AdaptiveContextOrchestrator(
        retrieval_service=retrieval_service,
        classifier=classifier,
        ranker=ranker,
        packer=packer,
        token_optimizer=token_optimizer,
        session_memory_service=session_memory_service,
    )
    return orchestrator.orchestrate(
        query=query,
        user_id=user_id,
        repository_id=repository_id,
        max_tokens=max_tokens,
        symbols=symbols,
        entities=entities,
        edges=edges,
        memories=memories,
        session_memory_service=session_memory_service,
        optimization_mode=optimization_mode,
        llm_compressor=llm_compressor,
    )
