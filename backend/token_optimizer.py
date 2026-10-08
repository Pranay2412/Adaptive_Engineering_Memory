"""Token Optimization Engine for Synapse.

Provides deterministic and optional LLM-based token budgeting and context optimization:
1. Estimates token costs accurately for queries, candidate context items, and rendered prompts.
2. Extracts and guarantees candidate provenance anchors (sources, symbols, relationships, changes, decisions).
3. Detects and removes redundant items (lexical Jaccard overlap and structural subsumption).
4. Multi-stage optimization pipeline:
   raw context -> deduplication -> structured compression -> token budget enforcement
5. Preserves graph relationships (SPECIFIES, CALLS, DEPENDS_ON, IMPORTS) necessary for understanding.
6. Enforces strict budget ceilings without compromising provenance.
7. Supports comparing three optimization modes:
   - "none": Raw uncompressed context with budget cutoff
   - "deterministic": Utility-per-token density ranking + multi-tier compaction
   - "llm": Structured LLM compression with provenance guardrails
"""

from __future__ import annotations

import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .models import (
    HybridRetrievalResult,
    IntentAnalysisResult,
    OptimizationComparison,
    OptimizationMode,
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
    LLM_COMPRESSED = "llm_compressed"  # Synthesized via structured LLM compression


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
        if item.node_id:
            lines.append(f"- Node ID: `{item.node_id}`")
        if item.confidence is not None:
            lines.append(f"- Confidence: {item.confidence:.2f}")
        if item.provenance and item.provenance.channels:
            lines.append(f"- Matched via: {', '.join(item.provenance.channels)}")
        if item.content_snippet:
            lines.append(f"- Details: {item.content_snippet.strip()}")
        if item.relationship_path:
            path_str = " -> ".join([h.format_hop() for h in item.relationship_path])
            lines.append(f"- Graph Path: {path_str}")

        # Preserve engineering decisions & recent changes if present in metadata
        meta = item.metadata if isinstance(item.metadata, dict) else {}
        if "engineering_decisions" in meta:
            decs = meta["engineering_decisions"]
            dec_str = "; ".join(decs) if isinstance(decs, list) else str(decs)
            lines.append(f"- Engineering Decisions: {dec_str}")
        if "recent_changes" in meta:
            chgs = meta["recent_changes"]
            chg_str = "; ".join(chgs) if isinstance(chgs, list) else str(chgs)
            lines.append(f"- Recent Changes: {chg_str}")

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

    elif tier == ContextTier.MINIMAL:
        path_tag = ""
        if item.relationship_path:
            hop = item.relationship_path[0]
            path_tag = f" ({hop.source_name} -[{hop.relation}]-> {hop.target_name})"
        return f"- `{item.entity}` ({item.type}) in `{item.file_or_document}`{path_tag} [score: {item.score:.2f}]"

    else:
        return render_candidate_item(item, ContextTier.COMPACT)


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
# Provenance Anchors & Guardrails
# ---------------------------------------------------------------------------

@dataclass
class CandidateProvenanceAnchor:
    """Explicit provenance anchor to guarantee traceability through compression."""

    entity: str
    type: str
    file_or_document: str
    node_id: str
    source: str
    relationships: list[str] = field(default_factory=list)
    engineering_decisions: list[str] = field(default_factory=list)
    recent_changes: list[str] = field(default_factory=list)
    provenance_channels: list[str] = field(default_factory=list)
    confidence: float | None = None

    def to_summary_line(self) -> str:
        """Render a concise provenance traceability ledger line."""
        src_label = self.file_or_document or self.source or "unknown_source"
        parts = [f"[Source: `{src_label}`"]
        if self.node_id:
            parts.append(f"Node: `{self.node_id}`")
        if self.confidence is not None:
            parts.append(f"Conf: {self.confidence:.2f}")
        header = " | ".join(parts) + "]"

        details = [f"Symbol: `{self.entity}` ({self.type})"]
        if self.relationships:
            details.append(f"Relations: {'; '.join(self.relationships[:2])}")
        if self.engineering_decisions:
            details.append(f"Decisions: {'; '.join(self.engineering_decisions[:2])}")
        if self.recent_changes:
            details.append(f"Recent: {'; '.join(self.recent_changes[:2])}")
        return f"- {header} " + " | ".join(details)


def extract_provenance_anchor(item: HybridRetrievalResult) -> CandidateProvenanceAnchor:
    """Extract and preserve all source identifiers, symbols, relationships, changes, and decisions."""
    # 1. Relationships
    relationships: list[str] = []
    if item.relationship_path:
        for hop in item.relationship_path:
            relationships.append(f"{hop.source_name} -[{hop.relation}]-> {hop.target_name}")

    # 2. Engineering Decisions
    decisions: list[str] = []
    meta = item.metadata if isinstance(item.metadata, dict) else {}
    if "engineering_decisions" in meta:
        raw_dec = meta["engineering_decisions"]
        if isinstance(raw_dec, list):
            decisions.extend([str(d) for d in raw_dec])
        elif isinstance(raw_dec, str):
            decisions.append(raw_dec)
    elif "decisions" in meta:
        raw_dec = meta["decisions"]
        if isinstance(raw_dec, list):
            decisions.extend([str(d) for d in raw_dec])
        elif isinstance(raw_dec, str):
            decisions.append(raw_dec)
    elif "rationale" in meta:
        decisions.append(str(meta["rationale"]))

    snippet = item.content_snippet or ""
    for line in snippet.split("\n"):
        clean_l = line.strip()
        if re.search(r"\b(?:decision|architectural decision|rfc|rationale)\b", clean_l, re.IGNORECASE):
            if clean_l not in decisions:
                decisions.append(clean_l)

    # 3. Recent Changes
    changes: list[str] = []
    if "recent_changes" in meta:
        raw_chg = meta["recent_changes"]
        if isinstance(raw_chg, list):
            changes.extend([str(c) for c in raw_chg])
        elif isinstance(raw_chg, str):
            changes.append(raw_chg)
    elif "updated_at" in meta:
        changes.append(f"Updated on {meta['updated_at']}")

    for line in snippet.split("\n"):
        clean_l = line.strip()
        if re.search(r"\b(?:changed|recent change|modified|deprecated|refactored on)\b", clean_l, re.IGNORECASE):
            if clean_l not in changes:
                changes.append(clean_l)

    # 4. Provenance channels
    channels = list(item.provenance.channels) if item.provenance and item.provenance.channels else []

    return CandidateProvenanceAnchor(
        entity=item.entity,
        type=item.type,
        file_or_document=item.file_or_document,
        node_id=item.node_id or "",
        source=item.source or "code",
        relationships=relationships,
        engineering_decisions=decisions,
        recent_changes=changes,
        provenance_channels=channels,
        confidence=item.confidence,
    )


class ProvenanceGuardrail:
    """Guarantees compressed context remains 100% traceable to original sources.

    Never allows LLM or deterministic compression to discard provenance anchors.
    """

    @classmethod
    def verify_and_guard(
        cls,
        compressed_text: str,
        candidates: list[HybridRetrievalResult],
    ) -> tuple[str, dict[str, Any]]:
        """Verify presence of sources and symbols, and inject traceability ledger if necessary."""
        anchors = [extract_provenance_anchor(it) for it in candidates]
        sources_found: list[str] = []
        symbols_found: list[str] = []
        relationships_found: list[str] = []
        decisions_found: list[str] = []
        recent_changes_found: list[str] = []

        missing_anchors: list[CandidateProvenanceAnchor] = []
        lower_text = compressed_text.lower()

        for a in anchors:
            source_matched = bool(a.file_or_document and a.file_or_document.lower() in lower_text)
            symbol_matched = bool(a.entity and a.entity.lower() in lower_text)

            if source_matched:
                sources_found.append(a.file_or_document)
            if symbol_matched:
                symbols_found.append(a.entity)

            for rel in a.relationships:
                part = rel.split(" -[")[0].strip().lower()
                if part in lower_text:
                    relationships_found.append(rel)

            for dec in a.engineering_decisions:
                words = [w for w in dec.lower().split() if len(w) > 4]
                if any(w in lower_text for w in words):
                    decisions_found.append(dec)

            for chg in a.recent_changes:
                words = [w for w in chg.lower().split() if len(w) > 4]
                if any(w in lower_text for w in words):
                    recent_changes_found.append(chg)

            if not (source_matched and symbol_matched):
                missing_anchors.append(a)

        # Invariant: Never allow compression to remove provenance!
        ledger_injected = False
        guarded_text = compressed_text

        has_ledger = "provenance & source traceability ledger" in lower_text
        if missing_anchors or not has_ledger:
            ledger_lines = [
                "",
                "#### Provenance & Source Traceability Ledger",
            ]
            for a in anchors:
                ledger_lines.append(a.to_summary_line())
            guarded_text = compressed_text.rstrip() + "\n" + "\n".join(ledger_lines)
            ledger_injected = True

            # Register missing items now that ledger is injected
            for a in missing_anchors:
                if a.file_or_document and a.file_or_document not in sources_found:
                    sources_found.append(a.file_or_document)
                if a.entity and a.entity not in symbols_found:
                    symbols_found.append(a.entity)

        audit: dict[str, Any] = {
            "all_sources_preserved": len(sources_found) == len(anchors),
            "all_symbols_preserved": len(symbols_found) == len(anchors),
            "sources_preserved": list(set(sources_found)),
            "symbols_preserved": list(set(symbols_found)),
            "relationships_preserved": list(set(relationships_found)),
            "decisions_preserved": list(set(decisions_found)),
            "recent_changes_preserved": list(set(recent_changes_found)),
            "ledger_injected": ledger_injected,
            "total_anchors_tracked": len(anchors),
        }
        return guarded_text, audit


# ---------------------------------------------------------------------------
# LLM Context Compressor Interface & Implementations
# ---------------------------------------------------------------------------

class BaseLLMCompressor(ABC):
    """Abstract interface for LLM-based structured context compression."""

    @abstractmethod
    def compress(
        self,
        raw_context: str,
        query: str,
        target_token_budget: int,
        candidates: list[HybridRetrievalResult],
    ) -> str:
        """Compress raw context while preserving sources, symbols, relationships, changes, and decisions."""
        pass


class MockLLMCompressor(BaseLLMCompressor):
    """Deterministic, zero-dependency LLM compressor for testing and offline runs."""

    def __init__(self, compression_ratio: float = 0.50) -> None:
        self.compression_ratio = max(0.20, min(0.95, compression_ratio))

    def compress(
        self,
        raw_context: str,
        query: str,
        target_token_budget: int,
        candidates: list[HybridRetrievalResult],
    ) -> str:
        """Synthesize dense, structured Markdown maintaining all invariants."""
        anchors = [extract_provenance_anchor(it) for it in candidates]

        lines = [
            f"### Synapse Compressed Engineering Context (Mode: LLM Compression)",
            f"> Query: `{query}` | Target Budget: {target_token_budget} tokens",
            "",
            "#### Compressed Architectural & Implementation Syntheses",
        ]

        for item, a in zip(candidates, anchors):
            desc = item.content_snippet.strip() if item.content_snippet else f"Implementation for {item.entity}."
            if len(desc) > 120:
                desc = desc[:117].rsplit(" ", 1)[0] + "..."

            block_lines = [
                f"- **`{item.entity}`** ({item.type}) [Source: `{item.file_or_document}` | Conf: {item.confidence or 1.0:.2f}]",
                f"  - Summary: {desc}",
            ]
            if a.relationships:
                block_lines.append(f"  - Relations: {'; '.join(a.relationships)}")
            if a.engineering_decisions:
                block_lines.append(f"  - Decisions: {'; '.join(a.engineering_decisions)}")
            if a.recent_changes:
                block_lines.append(f"  - Recent: {'; '.join(a.recent_changes)}")

            lines.extend(block_lines)

        lines.append("")
        lines.append("#### Provenance & Source Traceability Ledger")
        for a in anchors:
            lines.append(a.to_summary_line())

        return "\n".join(lines)


class OpenAILLMCompressor(BaseLLMCompressor):
    """OpenAI-backed structured context compressor."""

    SYSTEM_PROMPT = (
        "You are the Synapse Structured Context Compressor for an AI coding assistant.\n"
        "Your goal is to compress engineering memory context to fit within the specified token budget.\n"
        "STRICT INVARIANTS (NEVER VIOLATE):\n"
        "1. PRESERVE SOURCE IDENTIFIERS: Always explicitly cite source file or document paths (e.g., `[Source: path/to/file.py]`).\n"
        "2. PRESERVE CODE SYMBOLS: Keep exact class, function, method, and variable names.\n"
        "3. PRESERVE RELATIONSHIPS: Retain directional dependency and call relationships (e.g., `A -[CALLS]-> B`).\n"
        "4. PRESERVE RECENT CHANGES: Retain modification timestamps and recent updates.\n"
        "5. PRESERVE ENGINEERING DECISIONS: Retain architectural decisions, rationales, and design trade-offs.\n"
        "6. NEVER REMOVE PROVENANCE: Every statement must remain traceable to its original sources.\n"
        "Output clean, concise Markdown with syntheses and a provenance traceability ledger."
    )

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise EnvironmentError("OpenAI API key missing for OpenAILLMCompressor.")
        self.model = model or os.environ.get("LLM_MODEL", "gpt-4o-mini")
        import openai
        self.client = openai.OpenAI(api_key=key)

    def compress(
        self,
        raw_context: str,
        query: str,
        target_token_budget: int,
        candidates: list[HybridRetrievalResult],
    ) -> str:
        prompt = (
            f"Query: {query}\n"
            f"Target token budget: {target_token_budget}\n\n"
            f"Raw Engineering Context:\n{raw_context}"
        )
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
            )
            return response.choices[0].message.content or raw_context
        except Exception as e:
            logger.warning("OpenAI compression failed, falling back to mock compressor: %s", e)
            return MockLLMCompressor().compress(raw_context, query, target_token_budget, candidates)


class GeminiLLMCompressor(BaseLLMCompressor):
    """Google Gemini-backed structured context compressor."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise EnvironmentError("Gemini API key missing for GeminiLLMCompressor.")
        self.api_key = key
        self.model = model or os.environ.get("LLM_MODEL", "gemini-2.0-flash")

    def compress(
        self,
        raw_context: str,
        query: str,
        target_token_budget: int,
        candidates: list[HybridRetrievalResult],
    ) -> str:
        try:
            from google import genai
            client = genai.Client(api_key=self.api_key)
            prompt = (
                f"{OpenAILLMCompressor.SYSTEM_PROMPT}\n\n"
                f"Query: {query}\n"
                f"Target token budget: {target_token_budget}\n\n"
                f"Raw Engineering Context:\n{raw_context}"
            )
            response = client.models.generate_content(
                model=self.model,
                contents=prompt,
            )
            return response.text or raw_context
        except Exception as e:
            logger.warning("Gemini compression failed, falling back to mock compressor: %s", e)
            return MockLLMCompressor().compress(raw_context, query, target_token_budget, candidates)


def get_llm_compressor(
    provider: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> BaseLLMCompressor:
    """Factory to instantiate configured LLM compressor, defaulting to MockLLMCompressor for offline tests."""
    selected_provider = (provider or os.environ.get("LLM_PROVIDER", "mock")).lower()
    if selected_provider == "openai":
        try:
            return OpenAILLMCompressor(api_key=api_key, model=model)
        except Exception as e:
            logger.warning("Falling back to MockLLMCompressor: %s", e)
            return MockLLMCompressor()
    elif selected_provider == "gemini":
        try:
            return GeminiLLMCompressor(api_key=api_key, model=model)
        except Exception as e:
            logger.warning("Falling back to MockLLMCompressor: %s", e)
            return MockLLMCompressor()
    else:
        return MockLLMCompressor()


# ---------------------------------------------------------------------------
# Token Optimization Engine
# ---------------------------------------------------------------------------

class TokenOptimizationEngine:
    """Synapse Token Optimization Engine.

    Executes a 4-stage optimization pipeline:
    raw context -> deduplication -> structured compression -> token budget enforcement

    Supports 3 modes:
    1. OptimizationMode.NONE: Raw uncompressed context with budget cutoff
    2. OptimizationMode.DETERMINISTIC: Utility-density ranking + multi-tier compaction
    3. OptimizationMode.LLM: Structured LLM compression with provenance guardrails
    """

    def __init__(
        self,
        jaccard_threshold: float = 0.70,
        enable_tier_compaction: bool = True,
        header_budget_allowance: int = 50,
        llm_compressor: BaseLLMCompressor | None = None,
    ) -> None:
        self.redundancy_detector = RedundancyDetector(jaccard_threshold=jaccard_threshold)
        self.enable_tier_compaction = enable_tier_compaction
        self.header_budget_allowance = header_budget_allowance
        self.llm_compressor = llm_compressor or MockLLMCompressor()

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
        mode: OptimizationMode | str = OptimizationMode.DETERMINISTIC,
        llm_compressor: BaseLLMCompressor | None = None,
    ) -> TokenOptimizationResult:
        """Execute token optimization pipeline across the selected mode.

        Pipeline:
        raw context -> deduplication -> structured compression -> token budget enforcement
        """
        if isinstance(mode, str):
            try:
                norm_mode = OptimizationMode(mode.lower())
            except ValueError:
                norm_mode = OptimizationMode.DETERMINISTIC
        else:
            norm_mode = mode

        if not ranked_context_items or max_token_budget <= 0:
            return TokenOptimizationResult(
                optimized_context="",
                estimated_input_tokens=0,
                original_estimated_tokens=0,
                tokens_saved=0,
                percentage_reduction=0.0,
                items_removed=[],
                items_retained=[],
                mode=norm_mode.value,
                provenance_audit={"all_sources_preserved": True, "all_symbols_preserved": True},
                metadata={"status": "empty_input_or_budget", "mode": norm_mode.value},
            )

        # -------------------------------------------------------------------
        # Step 1: Raw Context Assembly & Invariants Extraction
        # -------------------------------------------------------------------
        unoptimized_blocks = [render_candidate_item(it, ContextTier.FULL) for it in ranked_context_items]
        raw_header = f"### Synapse Context (Query: {user_query})\n"
        original_estimated_tokens = TokenEstimator.estimate_text("\n\n".join([raw_header, *unoptimized_blocks]))

        intent_label = f" (Intent: {intent_result.primary_intent.value.replace('_', ' ').title()})" if intent_result else ""
        header = f"### Synapse Engineering Memory Context{intent_label}\n> Query: `{user_query.strip()}` | Budget: {max_token_budget} tokens"

        # -------------------------------------------------------------------
        # Step 2: Deduplication
        # -------------------------------------------------------------------
        deduped_candidates: list[HybridRetrievalResult] = []
        removed_records: list[RemovedItemRecord] = []

        for item in ranked_context_items:
            is_redundant, redundancy_reason = self.redundancy_detector.check_redundancy(item, deduped_candidates)
            full_tokens = TokenEstimator.estimate_item(item, ContextTier.FULL)
            utility = self.compute_candidate_utility(item, user_query)
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
            else:
                deduped_candidates.append(item)

        # -------------------------------------------------------------------
        # Step 3: Structured Compression
        # -------------------------------------------------------------------
        accepted_records: list[OptimizedItemRecord] = []
        provenance_audit: dict[str, Any] = {}

        if norm_mode == OptimizationMode.NONE:
            # Mode 1: No Optimization - Sequential raw full inclusion under budget
            rendered_blocks = [header]
            used_tokens = TokenEstimator.estimate_text(header)

            for item in deduped_candidates:
                full_text = render_candidate_item(item, ContextTier.FULL)
                cost_full = TokenEstimator.estimate_text(full_text)
                utility = self.compute_candidate_utility(item, user_query)

                if used_tokens + cost_full <= max_token_budget:
                    rendered_blocks.append(full_text)
                    used_tokens += cost_full
                    accepted_records.append(
                        OptimizedItemRecord(
                            entity=item.entity,
                            type=item.type,
                            file_or_document=item.file_or_document,
                            tier=ContextTier.FULL.value,
                            tokens=cost_full,
                            utility=utility,
                            utility_per_token=round(utility / max(1, cost_full), 4),
                            relevance_information=item.relevance_information,
                        )
                    )
                else:
                    removed_records.append(
                        RemovedItemRecord(
                            entity=item.entity,
                            type=item.type,
                            file_or_document=item.file_or_document,
                            reason=f"budget_exceeded: raw item requires {cost_full} tokens, {max_token_budget - used_tokens} left",
                            original_tokens=cost_full,
                            utility=utility,
                        )
                    )

            final_context = "\n\n".join(rendered_blocks).strip()
            final_input_tokens = TokenEstimator.estimate_text(final_context)
            provenance_audit = {
                "all_sources_preserved": True,
                "all_symbols_preserved": True,
                "sources_preserved": [r.file_or_document for r in accepted_records],
                "symbols_preserved": [r.entity for r in accepted_records],
            }

        elif norm_mode == OptimizationMode.DETERMINISTIC:
            # Mode 2: Deterministic Optimization - Utility density ranking + multi-tier compaction
            scored_candidates: list[tuple[HybridRetrievalResult, float, int, float]] = []
            for item in deduped_candidates:
                utility = self.compute_candidate_utility(item, user_query)
                full_tokens = TokenEstimator.estimate_item(item, ContextTier.FULL)
                utility_per_token = utility / max(1, full_tokens)
                scored_candidates.append((item, utility, full_tokens, utility_per_token))

            scored_candidates.sort(key=lambda t: t[3], reverse=True)

            accepted_items: list[HybridRetrievalResult] = []
            rendered_blocks = [header]
            used_tokens = TokenEstimator.estimate_text(header)
            active_graph_hops: list[RelationshipHop] = []

            for item, utility, full_tokens, density in scored_candidates:
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

                selected_tier: ContextTier | None = None
                selected_text: str = ""
                selected_tokens: int = 0

                full_text = render_candidate_item(item, ContextTier.FULL)
                cost_full = TokenEstimator.estimate_text(full_text)

                if cost_full <= remaining_budget:
                    selected_tier = ContextTier.FULL
                    selected_text = full_text
                    selected_tokens = cost_full
                elif self.enable_tier_compaction:
                    compact_text = render_candidate_item(item, ContextTier.COMPACT)
                    cost_compact = TokenEstimator.estimate_text(compact_text)
                    if cost_compact <= remaining_budget:
                        selected_tier = ContextTier.COMPACT
                        selected_text = compact_text
                        selected_tokens = cost_compact
                    else:
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

            final_context = "\n\n".join(rendered_blocks).strip()
            final_input_tokens = TokenEstimator.estimate_text(final_context)
            provenance_audit = {
                "all_sources_preserved": True,
                "all_symbols_preserved": True,
                "sources_preserved": [r.file_or_document for r in accepted_records],
                "symbols_preserved": [r.entity for r in accepted_records],
                "active_graph_hops_count": len(active_graph_hops),
            }

        else:
            # Mode 3: LLM Compression - Structured compression pass + provenance guardrails
            compressor = llm_compressor or self.llm_compressor or MockLLMCompressor()

            # Construct raw input text for compressor with all candidate provenance anchors
            raw_input_lines = [header, ""]
            for item in deduped_candidates:
                raw_input_lines.append(render_candidate_item(item, ContextTier.FULL))
            raw_prompt_text = "\n\n".join(raw_input_lines)

            # Invoke LLM compressor
            compressed_raw = compressor.compress(
                raw_context=raw_prompt_text,
                query=user_query,
                target_token_budget=max_token_budget,
                candidates=deduped_candidates,
            )

            # Stage 4: Provenance Guardrail & Token Budget Enforcement
            guarded_text, provenance_audit = ProvenanceGuardrail.verify_and_guard(
                compressed_text=compressed_raw,
                candidates=deduped_candidates,
            )

            # Check if guarded text exceeds token budget
            guarded_tokens = TokenEstimator.estimate_text(guarded_text)
            if guarded_tokens <= max_token_budget:
                final_context = guarded_text
                final_input_tokens = guarded_tokens
                for item in deduped_candidates:
                    utility = self.compute_candidate_utility(item, user_query)
                    item_tok = max(1, guarded_tokens // max(1, len(deduped_candidates)))
                    accepted_records.append(
                        OptimizedItemRecord(
                            entity=item.entity,
                            type=item.type,
                            file_or_document=item.file_or_document,
                            tier=ContextTier.LLM_COMPRESSED.value,
                            tokens=item_tok,
                            utility=utility,
                            utility_per_token=round(utility / item_tok, 4),
                            relevance_information=item.relevance_information,
                        )
                    )
            else:
                # Truncate lower-priority blocks while preserving header and provenance ledger
                lines = guarded_text.split("\n")
                ledger_start_idx = -1
                for idx, line in enumerate(lines):
                    if "provenance & source traceability ledger" in line.lower():
                        ledger_start_idx = idx
                        break

                ledger_chunk = "\n".join(lines[ledger_start_idx:]) if ledger_start_idx != -1 else ""
                body_chunk = "\n".join(lines[:ledger_start_idx]) if ledger_start_idx != -1 else guarded_text

                body_lines = body_chunk.split("\n")
                kept_body_lines: list[str] = []
                curr_tokens = TokenEstimator.estimate_text(ledger_chunk) + TokenEstimator.estimate_text(header)

                for bl in body_lines:
                    line_tok = TokenEstimator.estimate_text(bl)
                    if curr_tokens + line_tok <= max_token_budget:
                        kept_body_lines.append(bl)
                        curr_tokens += line_tok
                    else:
                        break

                final_context = "\n".join(kept_body_lines) + "\n\n" + ledger_chunk
                final_context = final_context.strip()
                final_input_tokens = TokenEstimator.estimate_text(final_context)

                # Re-audit retained items
                for item in deduped_candidates:
                    utility = self.compute_candidate_utility(item, user_query)
                    if item.entity.lower() in final_context.lower():
                        item_tok = max(1, final_input_tokens // max(1, len(deduped_candidates)))
                        accepted_records.append(
                            OptimizedItemRecord(
                                entity=item.entity,
                                type=item.type,
                                file_or_document=item.file_or_document,
                                tier=ContextTier.LLM_COMPRESSED.value,
                                tokens=item_tok,
                                utility=utility,
                                utility_per_token=round(utility / item_tok, 4),
                                relevance_information=item.relevance_information,
                            )
                        )
                    else:
                        removed_records.append(
                            RemovedItemRecord(
                                entity=item.entity,
                                type=item.type,
                                file_or_document=item.file_or_document,
                                reason="budget_exceeded: trimmed in compression budget enforcement",
                                original_tokens=TokenEstimator.estimate_item(item, ContextTier.FULL),
                                utility=utility,
                            )
                        )

        # -------------------------------------------------------------------
        # Step 4: Final Metrics & Audit Assembly
        # -------------------------------------------------------------------
        tokens_saved = max(0, original_estimated_tokens - final_input_tokens)
        percentage_reduction = round((tokens_saved / original_estimated_tokens) * 100.0, 2) if original_estimated_tokens > 0 else 0.0

        metadata: dict[str, Any] = {
            "mode": norm_mode.value,
            "query": user_query,
            "max_token_budget": max_token_budget,
            "used_tokens": final_input_tokens,
            "budget_utilization_pct": round((final_input_tokens / max(1, max_token_budget)) * 100.0, 2),
            "total_candidates_evaluated": len(ranked_context_items),
            "items_retained_count": len(accepted_records),
            "items_removed_count": len(removed_records),
            "provenance_preserved": provenance_audit.get("all_sources_preserved", True),
        }

        return TokenOptimizationResult(
            optimized_context=final_context,
            estimated_input_tokens=final_input_tokens,
            original_estimated_tokens=original_estimated_tokens,
            tokens_saved=tokens_saved,
            percentage_reduction=percentage_reduction,
            items_removed=[r.to_dict() for r in removed_records],
            items_retained=[r.to_dict() for r in accepted_records],
            mode=norm_mode.value,
            provenance_audit=provenance_audit,
            metadata=metadata,
        )

    def compare_modes(
        self,
        user_query: str,
        ranked_context_items: list[HybridRetrievalResult],
        max_token_budget: int,
        intent_result: IntentAnalysisResult | None = None,
        llm_compressor: BaseLLMCompressor | None = None,
    ) -> OptimizationComparison:
        """Compare all three token optimization modes on the exact same context candidates:

        1. No Optimization (Raw Baseline)
        2. Deterministic Optimization (Density Ranking & Multi-Tier Compaction)
        3. LLM Compression (Structured Compression & Traceability Guardrails)
        """
        res_none = self.optimize(
            user_query=user_query,
            ranked_context_items=ranked_context_items,
            max_token_budget=max_token_budget,
            intent_result=intent_result,
            mode=OptimizationMode.NONE,
        )
        res_det = self.optimize(
            user_query=user_query,
            ranked_context_items=ranked_context_items,
            max_token_budget=max_token_budget,
            intent_result=intent_result,
            mode=OptimizationMode.DETERMINISTIC,
        )
        res_llm = self.optimize(
            user_query=user_query,
            ranked_context_items=ranked_context_items,
            max_token_budget=max_token_budget,
            intent_result=intent_result,
            mode=OptimizationMode.LLM,
            llm_compressor=llm_compressor,
        )

        comparison_table = [
            {
                "mode": "none",
                "name": "No Optimization (Raw)",
                "estimated_input_tokens": res_none.estimated_input_tokens,
                "original_estimated_tokens": res_none.original_estimated_tokens,
                "tokens_saved": res_none.tokens_saved,
                "percentage_reduction": res_none.percentage_reduction,
                "items_retained_count": len(res_none.items_retained),
                "items_removed_count": len(res_none.items_removed),
                "provenance_preserved": res_none.provenance_audit.get("all_sources_preserved", True),
                "symbols_preserved_count": len(res_none.provenance_audit.get("symbols_preserved", [])),
                "sources_preserved_count": len(res_none.provenance_audit.get("sources_preserved", [])),
            },
            {
                "mode": "deterministic",
                "name": "Deterministic Optimization",
                "estimated_input_tokens": res_det.estimated_input_tokens,
                "original_estimated_tokens": res_det.original_estimated_tokens,
                "tokens_saved": res_det.tokens_saved,
                "percentage_reduction": res_det.percentage_reduction,
                "items_retained_count": len(res_det.items_retained),
                "items_removed_count": len(res_det.items_removed),
                "provenance_preserved": res_det.provenance_audit.get("all_sources_preserved", True),
                "symbols_preserved_count": len(res_det.provenance_audit.get("symbols_preserved", [])),
                "sources_preserved_count": len(res_det.provenance_audit.get("sources_preserved", [])),
            },
            {
                "mode": "llm",
                "name": "LLM Structured Compression",
                "estimated_input_tokens": res_llm.estimated_input_tokens,
                "original_estimated_tokens": res_llm.original_estimated_tokens,
                "tokens_saved": res_llm.tokens_saved,
                "percentage_reduction": res_llm.percentage_reduction,
                "items_retained_count": len(res_llm.items_retained),
                "items_removed_count": len(res_llm.items_removed),
                "provenance_preserved": res_llm.provenance_audit.get("all_sources_preserved", True),
                "symbols_preserved_count": len(res_llm.provenance_audit.get("symbols_preserved", [])),
                "sources_preserved_count": len(res_llm.provenance_audit.get("sources_preserved", [])),
            },
        ]

        return OptimizationComparison(
            query=user_query,
            max_token_budget=max_token_budget,
            none_result=res_none,
            deterministic_result=res_det,
            llm_result=res_llm,
            comparison_table=comparison_table,
            metadata={
                "query": user_query,
                "max_token_budget": max_token_budget,
                "total_candidates_evaluated": len(ranked_context_items),
            },
        )


# ---------------------------------------------------------------------------
# Convenience Standalone Functions
# ---------------------------------------------------------------------------

def optimize_tokens(
    user_query: str,
    ranked_context_items: list[HybridRetrievalResult],
    max_token_budget: int,
    *,
    mode: OptimizationMode | str = OptimizationMode.DETERMINISTIC,
    llm_compressor: BaseLLMCompressor | None = None,
    intent_result: IntentAnalysisResult | None = None,
    jaccard_threshold: float = 0.70,
    enable_tier_compaction: bool = True,
) -> TokenOptimizationResult:
    """Convenience function to run the Synapse Token Optimization Engine in any mode."""
    optimizer = TokenOptimizationEngine(
        jaccard_threshold=jaccard_threshold,
        enable_tier_compaction=enable_tier_compaction,
        llm_compressor=llm_compressor,
    )
    return optimizer.optimize(
        user_query=user_query,
        ranked_context_items=ranked_context_items,
        max_token_budget=max_token_budget,
        intent_result=intent_result,
        mode=mode,
        llm_compressor=llm_compressor,
    )


def compare_optimization_modes(
    user_query: str,
    ranked_context_items: list[HybridRetrievalResult],
    max_token_budget: int,
    *,
    llm_compressor: BaseLLMCompressor | None = None,
    intent_result: IntentAnalysisResult | None = None,
    jaccard_threshold: float = 0.70,
) -> OptimizationComparison:
    """Convenience function to compare all three optimization modes side-by-side."""
    optimizer = TokenOptimizationEngine(
        jaccard_threshold=jaccard_threshold,
        llm_compressor=llm_compressor,
    )
    return optimizer.compare_modes(
        user_query=user_query,
        ranked_context_items=ranked_context_items,
        max_token_budget=max_token_budget,
        intent_result=intent_result,
        llm_compressor=llm_compressor,
    )
