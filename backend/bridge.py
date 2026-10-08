"""Multi-stage cross-linking engine connecting document concepts to code symbols."""

from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .models import CodeSymbol, DocumentEntity, GraphEdge
from .normalization import NormalizedEntity, normalize_entity, tokenize_name
from .semantic_matcher import (
    BaseEmbeddingProvider,
    SemanticCandidateRetriever,
    get_embedding_provider,
)

# Compatibility matrix mapping (entity_type, symbol_type) to compatibility score [0.0, 1.0]
TYPE_COMPATIBILITY_MATRIX: dict[tuple[str, str], float] = {
    ("requirement", "class"): 1.0,
    ("requirement", "function"): 0.95,
    ("requirement", "method"): 0.95,
    ("requirement", "module"): 0.85,
    ("specification", "class"): 1.0,
    ("specification", "function"): 0.95,
    ("specification", "method"): 0.95,
    ("specification", "module"): 0.90,
    ("architecture", "module"): 1.0,
    ("architecture", "package"): 1.0,
    ("architecture", "class"): 0.90,
    ("architecture", "function"): 0.60,
    ("datamodel", "class"): 1.0,
    ("datamodel", "struct"): 1.0,
    ("datamodel", "variable"): 0.70,
    ("constraint", "function"): 1.0,
    ("constraint", "method"): 1.0,
    ("constraint", "class"): 0.90,
    ("concept", "class"): 0.90,
    ("concept", "function"): 0.85,
    ("concept", "module"): 0.85,
}

PATH_NOISE_TOKENS: frozenset[str] = frozenset({
    "src", "lib", "backend", "tests", "test", "app", "main", "core",
    "py", "ts", "js", "md", "docs", "doc", "spec", "specs",
})


@dataclass
class BridgeConfig:
    """Configurable scoring weights and confidence thresholds for cross-linking."""

    high_threshold: float = 0.75
    low_threshold: float = 0.50
    min_token_length: int = 3
    top_n_semantic: int = 5
    min_semantic_similarity: float = 0.50
    semantic_dominant_threshold: float = 0.70
    weights_with_semantic: dict[str, float] = field(
        default_factory=lambda: {
            "lexical": 0.40,
            "semantic": 0.30,
            "context": 0.20,
            "type": 0.10,
        }
    )
    weights_without_semantic: dict[str, float] = field(
        default_factory=lambda: {
            "lexical": 0.55,
            "context": 0.30,
            "type": 0.15,
        }
    )


def compute_type_compatibility(entity_type: str, symbol_type: str) -> float:
    """Evaluate architectural and structural compatibility between entity and symbol types."""
    e_type = entity_type.strip().lower()
    s_type = symbol_type.strip().lower()

    if "req" in e_type or "spec" in e_type:
        e_norm = "requirement"
    elif "arch" in e_type or "system" in e_type or "comp" in e_type:
        e_norm = "architecture"
    elif "data" in e_type or "model" in e_type or "schema" in e_type:
        e_norm = "datamodel"
    elif "constrain" in e_type or "security" in e_type or "rule" in e_type:
        e_norm = "constraint"
    elif "concept" in e_type or "glossary" in e_type:
        e_norm = "concept"
    else:
        e_norm = e_type

    if "class" in s_type or "struct" in s_type or "interface" in s_type:
        s_norm = "class"
    elif "func" in s_type or "method" in s_type:
        s_norm = "function"
    elif "mod" in s_type or "pkg" in s_type or "package" in s_type or "file" in s_type:
        s_norm = "module"
    elif "var" in s_type or "const" in s_type:
        s_norm = "variable"
    else:
        s_norm = s_type

    return TYPE_COMPATIBILITY_MATRIX.get((e_norm, s_norm), 0.80)


def compute_context_score(
    entity: DocumentEntity,
    symbol: CodeSymbol,
    norm_e: NormalizedEntity,
    norm_s: NormalizedEntity,
) -> tuple[float, str | None]:
    """Compute context score using repository_id, file paths, and module keywords."""
    # 1. Repository-level isolation
    if entity.repository_id and symbol.repository_id:
        if entity.repository_id != symbol.repository_id:
            # Different repositories cannot be cross-linked
            return 0.0, None
        repo_score = 1.0
    else:
        repo_score = 0.75

    # 2. File and module proximity
    module_score = 0.65
    matched_module: str | None = None

    raw_file = (symbol.source_file or symbol.file or "").replace("\\", "/").strip()
    if raw_file and raw_file != ".":
        parts = [p.lower() for p in raw_file.split("/") if p and p != "."]
        dir_tokens: set[str] = set()
        stem_tokens: set[str] = set()

        if len(parts) > 1:
            for dir_part in parts[:-1]:
                dir_tokens.update(tokenize_name(dir_part))
            stem, _ = os.path.splitext(parts[-1])
            stem_tokens.update(tokenize_name(stem))
        elif parts:
            stem, _ = os.path.splitext(parts[0])
            stem_tokens.update(tokenize_name(stem))

        # Incorporate explicit module and package tokens if present
        if symbol.module:
            dir_tokens.update(tokenize_name(symbol.module))
        if symbol.package:
            dir_tokens.update(tokenize_name(symbol.package))

        # Filter out common framework / directory noise
        dir_tokens -= PATH_NOISE_TOKENS
        stem_tokens -= PATH_NOISE_TOKENS
        all_path_tokens = dir_tokens | stem_tokens

        # Check entity source file if available
        entity_source_file = getattr(entity, "source_file", "") or getattr(entity, "file", "")
        if not entity_source_file and isinstance(getattr(entity, "metadata", None), dict):
            entity_source_file = entity.metadata.get("source_file", "")

        if entity_source_file:
            e_parts = [p.lower() for p in entity_source_file.replace("\\", "/").split("/") if p]
            e_path_tokens: set[str] = set()
            for part in e_parts:
                stem, _ = os.path.splitext(part)
                e_path_tokens.update(tokenize_name(stem))
            e_path_tokens -= PATH_NOISE_TOKENS

            shared_dirs = all_path_tokens & e_path_tokens
            if shared_dirs:
                matched_module = sorted(shared_dirs)[0]
                module_score = 0.95

        # Check directory vs file stem overlap with entity name tokens
        if matched_module is None:
            shared_dirs = dir_tokens & set(norm_e.tokens)
            if shared_dirs:
                matched_module = sorted(shared_dirs)[0]
                module_score = 0.95
            else:
                shared_stem = stem_tokens & set(norm_e.tokens)
                if shared_stem:
                    matched_module = sorted(shared_stem)[0]
                    module_score = 0.75
                else:
                    module_score = 0.50

    final_context = round(0.40 * repo_score + 0.60 * module_score, 2)
    return final_context, matched_module


def compute_lexical_score(
    entity_name: str,
    symbol_name: str,
    norm_e: NormalizedEntity,
    norm_s: NormalizedEntity,
) -> tuple[float, str]:
    """Evaluate lexical overlap between entity and symbol names."""
    # 1. Exact case-sensitive match
    if entity_name == symbol_name:
        return 1.0, "exact name matching"

    # 2. Exact case-insensitive match
    if entity_name.lower() == symbol_name.lower():
        return 0.98, "case-insensitive exact name matching"

    # 3. Whole-word symbol mention in entity text
    mention_pattern = r"\b" + re.escape(symbol_name) + r"\b"
    if re.search(mention_pattern, entity_name):
        return 0.95, f"exact symbol mention '{symbol_name}'"
    if re.search(mention_pattern, entity_name, re.IGNORECASE):
        return 0.92, f"symbol mention '{symbol_name}'"

    # 4. Canonical match (CamelCase vs snake_case vs kebab-case vs dotted vs path)
    if norm_e.canonical and norm_e.canonical == norm_s.canonical:
        return 0.95, f"normalized identifier match ({norm_e.canonical})"

    # 5. Cross-scope canonical match (e.g. auth::service vs auth_service)
    if norm_e.canonical_match(norm_s):
        return 0.92, f"cross-scope normalized match ({norm_e.canonical})"

    # 6. Identical base token sequence
    if norm_e.tokens and norm_e.tokens == norm_s.tokens:
        return 0.90, f"identical tokens {norm_s.tokens}"

    # 7. Token containment (all symbol tokens appear in entity)
    t_s = set(norm_s.tokens)
    t_e = set(norm_e.tokens)
    if t_s and t_s <= t_e:
        if len(t_s) >= 2:
            ratio = len(t_s) / len(t_e)
            score = min(0.90, 0.75 + 0.15 * ratio)
            return round(score, 2), f"token subset match {tuple(sorted(t_s))}"
        # Single token match: require token length >= 4 and short entity to avoid false positives
        single_token = list(t_s)[0]
        if len(single_token) >= 4 and len(t_e) <= 3:
            return 0.65, f"single token match '{single_token}'"
        return 0.35, f"weak single token match '{single_token}'"

    # 8. Token Jaccard overlap
    jaccard = norm_e.token_jaccard(norm_s)
    if jaccard > 0:
        shared = sorted(list(t_e & t_s))
        score = round(0.70 * jaccard, 2)
        return score, f"partial token overlap {shared}"

    return 0.0, "no lexical overlap"


def _generate_candidate_pairs(
    entities: list[DocumentEntity],
    symbols: list[CodeSymbol],
    min_token_len: int = 3,
    semantic_retriever: SemanticCandidateRetriever | None = None,
    top_n_semantic: int = 5,
    min_semantic_similarity: float = 0.50,
) -> list[tuple[DocumentEntity, CodeSymbol, NormalizedEntity, NormalizedEntity]]:
    """Stage 1: Generate candidate pairs cheaply using inverted indexes and dense embeddings."""
    canonical_index: dict[str, list[tuple[CodeSymbol, NormalizedEntity]]] = defaultdict(list)
    token_index: dict[str, list[tuple[CodeSymbol, NormalizedEntity]]] = defaultdict(list)
    name_index: dict[str, list[tuple[CodeSymbol, NormalizedEntity]]] = defaultdict(list)
    symbol_norm_cache: dict[str, NormalizedEntity] = {}

    for symbol in symbols:
        norm_s = normalize_entity(symbol.name)
        symbol_norm_cache[symbol.id] = norm_s
        canonical_index[norm_s.canonical].append((symbol, norm_s))
        name_index[symbol.name.lower()].append((symbol, norm_s))
        for token in norm_s.tokens:
            if len(token) >= min_token_len:
                token_index[token].append((symbol, norm_s))

    candidates: list[tuple[DocumentEntity, CodeSymbol, NormalizedEntity, NormalizedEntity]] = []
    seen: set[tuple[str, str]] = set()

    for entity in entities:
        norm_e = normalize_entity(entity.name)
        matched_symbols: dict[str, tuple[CodeSymbol, NormalizedEntity]] = {}

        # 1. Exact name candidates
        for sym, norm_s in name_index.get(entity.name.lower(), []):
            matched_symbols[sym.id] = (sym, norm_s)

        # 2. Canonical name candidates
        for sym, norm_s in canonical_index.get(norm_e.canonical, []):
            matched_symbols[sym.id] = (sym, norm_s)

        # 3. Token index candidates
        for token in norm_e.tokens:
            if len(token) >= min_token_len:
                for sym, norm_s in token_index.get(token, []):
                    matched_symbols[sym.id] = (sym, norm_s)

        # 4. Whole-word symbol mentions in multi-word entity text
        for sym_name_lower, sym_list in name_index.items():
            if len(sym_name_lower) >= 3 and re.search(r"\b" + re.escape(sym_name_lower) + r"\b", entity.name.lower()):
                for sym, norm_s in sym_list:
                    matched_symbols[sym.id] = (sym, norm_s)

        for sym_id, (symbol, norm_s) in matched_symbols.items():
            pair_key = (entity.id, sym_id)
            if pair_key not in seen:
                seen.add(pair_key)
                candidates.append((entity, symbol, norm_e, norm_s))

    # 5. Semantic candidate retrieval (dense embeddings)
    if semantic_retriever is not None and entities and symbols:
        sem_results = semantic_retriever.retrieve_candidates(
            entities, symbols, top_n=top_n_semantic, min_similarity=min_semantic_similarity
        )
        for entity in entities:
            norm_e = normalize_entity(entity.name)
            for sym, _sim in sem_results.get(entity.id, []):
                pair_key = (entity.id, sym.id)
                if pair_key not in seen:
                    seen.add(pair_key)
                    norm_s = symbol_norm_cache.get(sym.id) or normalize_entity(sym.name)
                    candidates.append((entity, sym, norm_e, norm_s))

    return candidates


def build_cross_links(
    entities: list[DocumentEntity],
    symbols: list[CodeSymbol],
    *,
    semantic_scorer: Callable[[DocumentEntity, CodeSymbol], float] | None = None,
    semantic_retriever: SemanticCandidateRetriever | None = None,
    embedding_provider: BaseEmbeddingProvider | None = None,
    enable_semantic_search: bool = False,
    llm_verifier: Callable[[DocumentEntity, CodeSymbol], tuple[bool, float, str]] | None = None,
    code_edges: list[GraphEdge] | None = None,
    doc_edges: list[GraphEdge] | None = None,
    config: BridgeConfig | None = None,
    created_at: str | None = None,
) -> list[GraphEdge]:
    """Build multi-stage SPECIFIES cross-links between document entities and code symbols."""
    cfg = config or BridgeConfig()

    active_retriever = semantic_retriever
    if active_retriever is None and (embedding_provider is not None or enable_semantic_search):
        active_retriever = SemanticCandidateRetriever(provider=embedding_provider)

    candidates = _generate_candidate_pairs(
        entities,
        symbols,
        min_token_len=cfg.min_token_length,
        semantic_retriever=active_retriever,
        top_n_semantic=cfg.top_n_semantic,
        min_semantic_similarity=cfg.min_semantic_similarity,
    )

    links: list[GraphEdge] = []
    seen_links: set[tuple[str, str]] = set()

    for entity, symbol, norm_e, norm_s in candidates:
        pair_key = (entity.id, symbol.id)
        if pair_key in seen_links:
            continue

        # 1. Lexical Scoring
        lex_score, lex_reason = compute_lexical_score(entity.name, symbol.name, norm_e, norm_s)

        # 2. Semantic Scoring
        sem_score: float | None = None
        if semantic_scorer is not None:
            raw_sem = semantic_scorer(entity, symbol)
            if raw_sem is not None:
                sem_score = max(0.0, min(1.0, float(raw_sem)))
        elif active_retriever is not None:
            raw_sem = active_retriever.get_similarity(entity, symbol)
            if raw_sem is not None:
                sem_score = max(0.0, min(1.0, float(raw_sem)))

        # Gating: candidate must have either lexical overlap or sufficient semantic similarity
        if lex_score <= 0.0 and (sem_score is None or sem_score < cfg.min_semantic_similarity):
            continue

        # 3. Contextual Scoring
        ctx_score, matched_module = compute_context_score(entity, symbol, norm_e, norm_s)
        if ctx_score <= 0.0:
            # Explicit repository mismatch
            continue

        # 4. Entity-Type Compatibility
        type_score = compute_type_compatibility(entity.type, symbol.type)

        # 5. Preliminary Composite Scoring
        method_components: list[str] = []

        if sem_score is not None:
            if sem_score >= cfg.semantic_dominant_threshold and lex_score < 0.40:
                # Semantic-dominant match (different terminology like "authentication manager" vs "AuthService")
                weights = {
                    "semantic": 0.55,
                    "lexical": 0.10,
                    "context": 0.20,
                    "type": 0.15,
                }
                method_components.append("semantic")
                if lex_score > 0.0:
                    method_components.append("lexical")
            else:
                weights = cfg.weights_with_semantic
                if lex_score > 0.0:
                    method_components.append("lexical")
                method_components.append("semantic")

            preliminary_score = (
                weights["lexical"] * lex_score
                + weights["semantic"] * sem_score
                + weights["context"] * ctx_score
                + weights["type"] * type_score
            )
        else:
            weights = cfg.weights_without_semantic
            method_components.append("lexical")
            preliminary_score = (
                weights["lexical"] * lex_score
                + weights["context"] * ctx_score
                + weights["type"] * type_score
            )

        if ctx_score >= 0.75:
            method_components.append("context")

        # 6. Ambiguity Resolution & Optional LLM Verification
        final_confidence = preliminary_score
        llm_explanation: str | None = None

        if preliminary_score >= cfg.high_threshold:
            # High confidence: automatically accept without LLM call
            pass
        elif preliminary_score < cfg.low_threshold:
            # Low confidence: reject directly
            continue
        else:
            # Ambiguous band [low_threshold, high_threshold)
            if llm_verifier is not None:
                is_verified, llm_conf, llm_rationale = llm_verifier(entity, symbol)
                if is_verified:
                    final_confidence = min(1.0, preliminary_score + 0.20 * llm_conf)
                    if final_confidence >= 0.65:
                        method_components.append("llm")
                        llm_explanation = llm_rationale
                    else:
                        continue
                else:
                    # Refuted by LLM
                    continue
            else:
                # LLM verifier not supplied; require borderline threshold
                if preliminary_score >= 0.65:
                    final_confidence = preliminary_score
                else:
                    continue

        # 7. Compose Natural Language Explanation
        if sem_score is not None and sem_score >= cfg.semantic_dominant_threshold and lex_score < 0.40:
            explanation = f"Documentation entity {entity.name} semantically matches code {symbol.type} {symbol.name} (semantic similarity: {sem_score:.2f})"
        else:
            explanation = f"Documentation entity {entity.name} matches code {symbol.type} {symbol.name} via {lex_reason}"

        if matched_module:
            explanation += f" and occurs in the {matched_module} module."
        else:
            explanation += "."

        if llm_explanation:
            explanation += f" (LLM verification: {llm_explanation})"

        # 8. Construct Metadata & Edge
        now_iso = created_at or datetime.now(timezone.utc).isoformat()
        scores_dict = {
            "lexical": round(lex_score, 2),
            "semantic": round(sem_score, 2) if sem_score is not None else None,
            "context": round(ctx_score, 2) if ctx_score is not None else None,
            "type": round(type_score, 2),
            "final": round(final_confidence, 2),
        }
        metadata: dict[str, Any] = {
            "confidence": round(final_confidence, 2),
            "matching_method": "+".join(method_components),
            "lexical_score": round(lex_score, 2),
            "scores": scores_dict,
            "explanation": explanation,
            "created_at": now_iso,
            "source": "cross_link_bridge",
        }

        if sem_score is not None:
            metadata["semantic_score"] = round(sem_score, 2)
        if ctx_score is not None:
            metadata["context_score"] = round(ctx_score, 2)

        seen_links.add(pair_key)
        links.append(
            GraphEdge(
                source_id=entity.id,
                target_id=symbol.id,
                relation="SPECIFIES",
                metadata=metadata,
            )
        )

    # Sort links deterministically by source and target IDs
    links.sort(key=lambda edge: (edge.source_id, edge.target_id))
    return links