"""Hybrid Engineering Memory Retrieval Layer.

Combines:
1. Lexical search (field, token, and keyword matching)
2. Semantic / dense vector search (embedding cosine similarity)
3. Neo4j graph traversal (SPECIFIES, CALLS, IMPORTS, DEPENDS_ON multi-hop)

Fuses and deduplicates results into a unified structure with complete provenance,
context formatting, and token budgeting for the Adaptive Context Orchestrator.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from typing import Any

from .db_loader import Neo4jMemoryStore
from .models import (
    CodeSymbol,
    DocumentEntity,
    GraphEdge,
    HybridQueryResponse,
    HybridRetrievalResult,
    RelationshipHop,
    ResultProvenance,
    RetrievalResult,
    scoped_id,
)
from .normalization import tokenize_name
from .retrieval import DEFAULT_TRAVERSAL_RELATIONS, MemoryRetrievalService
from .semantic_matcher import (
    BaseEmbeddingProvider,
    code_symbol_to_text,
    cosine_similarity,
    document_entity_to_text,
    get_embedding_provider,
    normalize_vector,
)

logger = logging.getLogger(__name__)


class SemanticKnowledgeIndex:
    """In-memory dense vector index for code symbols and documentation entities."""

    def __init__(
        self,
        symbols: list[CodeSymbol] | None = None,
        entities: list[DocumentEntity] | None = None,
        provider: BaseEmbeddingProvider | None = None,
        batch_size: int = 128,
    ) -> None:
        self.provider = provider or get_embedding_provider()
        self.symbols = symbols or []
        self.entities = entities or []
        self.symbol_vectors: list[list[float]] = []
        self.entity_vectors: list[list[float]] = []

        self._build_index(batch_size=batch_size)

    def _build_index(self, batch_size: int = 128) -> None:
        """Batch-generate normalized embeddings for all symbols and entities."""
        # 1. Embed symbols
        if self.symbols:
            sym_texts = [code_symbol_to_text(s) for s in self.symbols]
            for i in range(0, len(sym_texts), batch_size):
                chunk = sym_texts[i : i + batch_size]
                vecs = self.provider.embed_batch(chunk)
                for vec in vecs:
                    self.symbol_vectors.append(normalize_vector(vec))

        # 2. Embed entities
        if self.entities:
            ent_texts = [document_entity_to_text(e) for e in self.entities]
            for i in range(0, len(ent_texts), batch_size):
                chunk = ent_texts[i : i + batch_size]
                vecs = self.provider.embed_batch(chunk)
                for vec in vecs:
                    self.entity_vectors.append(normalize_vector(vec))

    def query_symbols(
        self,
        query_vector: list[float],
        top_n: int = 10,
        min_similarity: float = 0.35,
    ) -> list[tuple[CodeSymbol, float]]:
        """Retrieve top-N code symbols matching query vector."""
        if not self.symbols or not self.symbol_vectors or not query_vector:
            return []

        norm_q = normalize_vector(query_vector)
        scored: list[tuple[CodeSymbol, float]] = []
        for sym, vec in zip(self.symbols, self.symbol_vectors):
            sim = cosine_similarity(norm_q, vec)
            if sim >= min_similarity:
                scored.append((sym, round(sim, 4)))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_n]

    def query_entities(
        self,
        query_vector: list[float],
        top_n: int = 10,
        min_similarity: float = 0.35,
    ) -> list[tuple[DocumentEntity, float]]:
        """Retrieve top-N document entities matching query vector."""
        if not self.entities or not self.entity_vectors or not query_vector:
            return []

        norm_q = normalize_vector(query_vector)
        scored: list[tuple[DocumentEntity, float]] = []
        for ent, vec in zip(self.entities, self.entity_vectors):
            sim = cosine_similarity(norm_q, vec)
            if sim >= min_similarity:
                scored.append((ent, round(sim, 4)))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_n]


class HybridRetrievalService:
    """Unified hybrid retrieval orchestrator for Adaptive Engineering Memory.

    Integrates:
    - Channel 1: Lexical search (code & docs)
    - Channel 2: Semantic / vector search (code & docs)
    - Channel 3: Neo4j / graph traversal (multi-hop expansion)
    - Channel 4: Recency & engineering knowledge scoring
    """

    def __init__(
        self,
        retrieval_service: MemoryRetrievalService | None = None,
        store: Neo4jMemoryStore | None = None,
        embedding_provider: BaseEmbeddingProvider | None = None,
        driver: Any | None = None,
    ) -> None:
        if retrieval_service is not None:
            self.retrieval_service = retrieval_service
            self._owns_retrieval = False
        elif store is not None or driver is not None:
            self.retrieval_service = MemoryRetrievalService(store=store, driver=driver)
            self._owns_retrieval = True
        else:
            try:
                self.retrieval_service = MemoryRetrievalService()
                self._owns_retrieval = True
            except Exception:
                self.retrieval_service = None
                self._owns_retrieval = False

        self.embedding_provider = embedding_provider or get_embedding_provider()
        self.vector_index: SemanticKnowledgeIndex | None = None

    def initialize_index(
        self,
        symbols: list[CodeSymbol] | None = None,
        entities: list[DocumentEntity] | None = None,
    ) -> None:
        """Initialize or update the in-memory semantic vector index."""
        self.vector_index = SemanticKnowledgeIndex(
            symbols=symbols,
            entities=entities,
            provider=self.embedding_provider,
        )

    def _traverse_in_memory_graph(
        self,
        seed_ids: set[str],
        symbols_by_id: dict[str, CodeSymbol],
        entities_by_id: dict[str, DocumentEntity],
        edges: list[GraphEdge],
        max_hops: int = 2,
        min_confidence: float = 0.0,
    ) -> list[tuple[Any, list[RelationshipHop], float]]:
        """Traverse relationship edges in memory when offline or edges are passed directly."""
        adj: dict[str, list[GraphEdge]] = {}
        for edge in edges:
            if edge.relation.upper() in DEFAULT_TRAVERSAL_RELATIONS:
                adj.setdefault(edge.source_id, []).append(edge)
                adj.setdefault(edge.target_id, []).append(edge)

        visited: set[str] = set(seed_ids)
        frontier: list[tuple[str, list[RelationshipHop], float]] = [
            (sid, [], 1.0) for sid in seed_ids
        ]
        traversed: list[tuple[Any, list[RelationshipHop], float]] = []

        for hop_num in range(1, max_hops + 1):
            next_frontier: list[tuple[str, list[RelationshipHop], float]] = []
            for curr_id, curr_path, parent_score in frontier:
                for edge in adj.get(curr_id, []):
                    is_outgoing = edge.source_id == curr_id
                    target_id = edge.target_id if is_outgoing else edge.source_id
                    if target_id in visited:
                        continue

                    # Lookup source and target object
                    source_obj = symbols_by_id.get(curr_id) or entities_by_id.get(curr_id)
                    target_obj = symbols_by_id.get(target_id) or entities_by_id.get(target_id)
                    if not target_obj:
                        continue

                    source_name = getattr(source_obj, "name", curr_id)
                    target_name = getattr(target_obj, "name", target_id)

                    meta = edge.metadata or {}
                    conf = float(meta.get("confidence", 1.0 if edge.relation != "SPECIFIES" else 0.85))
                    if conf < min_confidence:
                        continue

                    hop = RelationshipHop(
                        source_id=curr_id if is_outgoing else target_id,
                        source_name=source_name if is_outgoing else target_name,
                        relation=edge.relation,
                        target_id=target_id if is_outgoing else curr_id,
                        target_name=target_name if is_outgoing else source_name,
                        confidence=conf,
                        matching_method=meta.get("matching_method"),
                        explanation=meta.get("explanation"),
                        metadata=meta,
                    )

                    new_path = [*curr_path, hop]
                    visited.add(target_id)
                    decay = 0.8 ** hop_num
                    hop_score = parent_score * decay * conf

                    traversed.append((target_obj, new_path, hop_score))
                    next_frontier.append((target_id, new_path, hop_score))

            frontier = next_frontier
        return traversed

    def retrieve(
        self,
        user_id: str,
        repository_id: str,
        query: str,
        *,
        limit: int = 10,
        max_hops: int = 2,
        lexical_weight: float = 0.35,
        semantic_weight: float = 0.40,
        graph_weight: float = 0.25,
        recency_weight: float = 0.10,
        min_score: float = 0.0,
        min_confidence: float = 0.0,
        symbols: list[CodeSymbol] | None = None,
        entities: list[DocumentEntity] | None = None,
        edges: list[GraphEdge] | None = None,
    ) -> HybridQueryResponse:
        """Execute hybrid retrieval combining lexical, semantic, and graph channels."""
        if not user_id or not repository_id or not query or not query.strip():
            return HybridQueryResponse(
                query=query or "",
                user_id=user_id or "",
                repository_id=repository_id or "",
                results=[],
                total_results=0,
                query_terms=[],
                retrieval_stats={"status": "empty_query"},
            )

        clean_query = query.strip()
        if self.retrieval_service is not None:
            query_terms = self.retrieval_service.extract_query_terms(clean_query)
        else:
            raw_tokens = tokenize_name(clean_query)
            query_terms = sorted(list(set(t.lower() for t in raw_tokens if len(t) >= 2)), key=lambda x: -len(x))

        # Maps from node_id -> (object, score)
        lexical_map: dict[str, tuple[Any, float]] = {}
        semantic_map: dict[str, tuple[Any, float]] = {}
        graph_map: dict[str, tuple[Any, float, list[RelationshipHop]]] = {}
        candidate_objects: dict[str, Any] = {}

        # ---------------------------------------------------------
        # 1. Lexical Search Channel
        # ---------------------------------------------------------
        if self.retrieval_service is not None:
            lexical_results = self.retrieval_service.query(
                user_id=user_id,
                repository_id=repository_id,
                query=clean_query,
                limit=limit * 2,
                max_hops=0,  # Only direct matches for the pure lexical channel
                include_traversal=False,
            )
            for r in lexical_results:
                if r.node_id:
                    lexical_map[r.node_id] = (r, r.score)
                    candidate_objects[r.node_id] = r

        # If in-memory items are passed, ensure they are also scored lexically
        if symbols:
            for s in symbols:
                # Simple in-memory token scoring fallback if not found in DB
                if s.id not in lexical_map:
                    s_lower = s.name.lower()
                    if clean_query.lower() in s_lower:
                        lexical_map[s.id] = (s, 0.95)
                        candidate_objects[s.id] = s
                    elif any(t in s_lower for t in query_terms):
                        lexical_map[s.id] = (s, 0.75)
                        candidate_objects[s.id] = s

        if entities:
            for e in entities:
                if e.id not in lexical_map:
                    e_lower = e.name.lower()
                    if clean_query.lower() in e_lower:
                        lexical_map[e.id] = (e, 0.95)
                        candidate_objects[e.id] = e
                    elif any(t in e_lower for t in query_terms):
                        lexical_map[e.id] = (e, 0.75)
                        candidate_objects[e.id] = e

        # ---------------------------------------------------------
        # 2. Semantic / Dense Vector Search Channel
        # ---------------------------------------------------------
        index_to_use = self.vector_index
        if index_to_use is None and (symbols is not None or entities is not None):
            index_to_use = SemanticKnowledgeIndex(
                symbols=symbols,
                entities=entities,
                provider=self.embedding_provider,
            )

        if index_to_use is not None:
            query_vec = self.embedding_provider.embed_query(clean_query)
            if query_vec:
                top_syms = index_to_use.query_symbols(query_vec, top_n=limit * 2, min_similarity=0.30)
                for sym, sim in top_syms:
                    semantic_map[sym.id] = (sym, sim)
                    candidate_objects[sym.id] = sym

                top_ents = index_to_use.query_entities(query_vec, top_n=limit * 2, min_similarity=0.30)
                for ent, sim in top_ents:
                    semantic_map[ent.id] = (ent, sim)
                    candidate_objects[ent.id] = ent

        # ---------------------------------------------------------
        # 3. Graph Traversal Channel
        # ---------------------------------------------------------
        # Form seeds from the top lexical and semantic matches
        seed_candidates = sorted(
            candidate_objects.keys(),
            key=lambda nid: (
                lexical_map.get(nid, (None, 0.0))[1] + semantic_map.get(nid, (None, 0.0))[1]
            ),
            reverse=True,
        )[:8]

        seed_ids = set(seed_candidates)

        if max_hops >= 1 and seed_ids:
            if edges is not None and (symbols is not None or entities is not None):
                # Offline in-memory graph traversal
                syms_by_id = {s.id: s for s in (symbols or [])}
                ents_by_id = {e.id: e for e in (entities or [])}
                traversed_items = self._traverse_in_memory_graph(
                    seed_ids=seed_ids,
                    symbols_by_id=syms_by_id,
                    entities_by_id=ents_by_id,
                    edges=edges,
                    max_hops=max_hops,
                    min_confidence=min_confidence,
                )
                for obj, path, g_score in traversed_items:
                    nid = obj.id
                    graph_map[nid] = (obj, g_score, path)
                    candidate_objects[nid] = obj
            elif self.retrieval_service is not None:
                # Neo4j graph traversal via MemoryRetrievalService
                seeds_as_results = []
                for sid in seed_ids:
                    obj = candidate_objects[sid]
                    if isinstance(obj, RetrievalResult):
                        seeds_as_results.append(obj)
                    elif isinstance(obj, CodeSymbol):
                        seeds_as_results.append(
                            RetrievalResult(
                                entity=obj.name,
                                type=f"CodeSymbol ({obj.type})",
                                source="code",
                                node_id=obj.id,
                                score=lexical_map.get(obj.id, (None, 0.8))[1],
                                repository=repository_id,
                                file_or_document=obj.source_file or obj.file,
                            )
                        )
                    elif isinstance(obj, DocumentEntity):
                        seeds_as_results.append(
                            RetrievalResult(
                                entity=obj.name,
                                type=f"DocumentEntity ({obj.type})",
                                source="documentation",
                                node_id=obj.id,
                                score=lexical_map.get(obj.id, (None, 0.8))[1],
                                repository=repository_id,
                                file_or_document=obj.document_path or obj.source_document or "",
                            )
                        )

                traversed_results = self.retrieval_service.traverse_relationships(
                    user_id=user_id,
                    repository_id=repository_id,
                    seed_results=seeds_as_results,
                    max_hops=max_hops,
                    min_confidence=min_confidence,
                    hop_limit=limit * 3,
                )
                for tr in traversed_results:
                    nid = tr.node_id
                    graph_map[nid] = (tr, tr.score, tr.relationship_path)
                    candidate_objects[nid] = tr

        # ---------------------------------------------------------
        # 4. Multi-Signal Fusion, Provenance & Deduplication
        # ---------------------------------------------------------
        all_node_ids = set(candidate_objects.keys())
        fused_results: list[HybridRetrievalResult] = []

        # Compute rank maps for Reciprocal Rank Fusion (RRF)
        lex_ranks = {
            nid: idx + 1
            for idx, nid in enumerate(
                sorted(lexical_map.keys(), key=lambda k: lexical_map[k][1], reverse=True)
            )
        }
        sem_ranks = {
            nid: idx + 1
            for idx, nid in enumerate(
                sorted(semantic_map.keys(), key=lambda k: semantic_map[k][1], reverse=True)
            )
        }
        graph_ranks = {
            nid: idx + 1
            for idx, nid in enumerate(
                sorted(graph_map.keys(), key=lambda k: graph_map[k][1], reverse=True)
            )
        }

        k_rrf = 60

        for nid in all_node_ids:
            raw_obj = candidate_objects[nid]

            # Determine entity attributes
            if isinstance(raw_obj, CodeSymbol):
                entity_name = raw_obj.name
                entity_type = f"CodeSymbol ({raw_obj.type})"
                source = "code"
                file_or_doc = raw_obj.source_file or raw_obj.file
                content_snippet = raw_obj.signature or raw_obj.documentation or ""
                metadata = raw_obj.to_dict()
                created_at = None
                line_start = raw_obj.line_start
                line_end = raw_obj.line_end
                section = None
                doc_path = None
                code_path = file_or_doc
                dataset_id = None
            elif isinstance(raw_obj, DocumentEntity):
                entity_name = raw_obj.name
                entity_type = f"DocumentEntity ({raw_obj.type})"
                source = "documentation"
                file_or_doc = raw_obj.document_path or raw_obj.source_document or ""
                content_snippet = raw_obj.description or raw_obj.source_chunk or ""
                metadata = raw_obj.to_dict()
                created_at = raw_obj.metadata.get("created_at")
                line_start = None
                line_end = None
                section = raw_obj.section
                doc_path = file_or_doc
                code_path = None
                dataset_id = raw_obj.dataset_id
            elif isinstance(raw_obj, RetrievalResult):
                entity_name = raw_obj.entity
                entity_type = raw_obj.type
                source = raw_obj.source
                file_or_doc = raw_obj.file_or_document
                content_snippet = raw_obj.metadata.get("signature") or raw_obj.metadata.get("description") or ""
                metadata = raw_obj.metadata
                created_at = raw_obj.metadata.get("created_at")
                line_start = raw_obj.metadata.get("line_start")
                line_end = raw_obj.metadata.get("line_end")
                section = raw_obj.metadata.get("section")
                doc_path = file_or_doc if source == "documentation" else None
                code_path = file_or_doc if source == "code" else None
                dataset_id = raw_obj.metadata.get("dataset_id")
            else:
                entity_name = getattr(raw_obj, "name", str(raw_obj))
                entity_type = "KnowledgeEntity"
                source = "knowledge_graph"
                file_or_doc = getattr(raw_obj, "file", "") or getattr(raw_obj, "path", "")
                content_snippet = ""
                metadata = {}
                created_at = None
                line_start = None
                line_end = None
                section = None
                doc_path = None
                code_path = None
                dataset_id = None

            # Channel scores
            s_lex = lexical_map.get(nid, (None, 0.0))[1]
            s_sem = semantic_map.get(nid, (None, 0.0))[1]

            graph_entry = graph_map.get(nid)
            s_graph = graph_entry[1] if graph_entry else 0.0
            rel_path = graph_entry[2] if graph_entry else []

            # Determine retrieval channels present
            channels = []
            if nid in lexical_map and s_lex > 0:
                channels.append("lexical")
            if nid in semantic_map and s_sem > 0:
                channels.append("semantic")
            if nid in graph_map and s_graph > 0:
                channels.append("graph")

            if not channels:
                continue

            # Matched query terms
            matched_terms = [t for t in query_terms if t in entity_name.lower()]

            # Recency scoring: inspect created_at metadata
            s_recency = 0.5  # Neutral baseline
            if created_at:
                try:
                    # Parse ISO timestamp if available
                    created_dt = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    now = datetime.datetime.now(datetime.timezone.utc)
                    age_days = max(0, (now - created_dt).days)
                    s_recency = max(0.2, 1.0 - (age_days / 180.0))
                except Exception:
                    s_recency = 0.7
            elif "recent" in clean_query.lower():
                s_recency = 0.6

            # Compute RRF score
            rrf_score = 0.0
            if nid in lex_ranks:
                rrf_score += 1.0 / (k_rrf + lex_ranks[nid])
            if nid in sem_ranks:
                rrf_score += 1.0 / (k_rrf + sem_ranks[nid])
            if nid in graph_ranks:
                rrf_score += 1.0 / (k_rrf + graph_ranks[nid])

            # Weighted linear fusion
            weighted_score = (
                lexical_weight * s_lex
                + semantic_weight * s_sem
                + graph_weight * s_graph
                + recency_weight * s_recency
            )

            # Channel synergy boost: if corroborated by multiple independent channels
            synergy_multiplier = 1.0 + 0.15 * (len(channels) - 1)
            final_hybrid_score = min(1.0, weighted_score * synergy_multiplier)

            if final_hybrid_score < min_score:
                continue

            # Confidence determination
            conf_val: float | None = None
            if rel_path:
                conf_val = rel_path[-1].confidence
            elif "semantic" in channels and s_sem > 0:
                conf_val = round(s_sem, 2)
            elif "lexical" in channels and s_lex > 0:
                conf_val = round(s_lex, 2)

            # Construct relevance justification
            relevance_reasons = []
            if "lexical" in channels:
                relevance_reasons.append(f"lexical match ({s_lex:.2f})")
            if "semantic" in channels:
                relevance_reasons.append(f"semantic similarity ({s_sem:.2f})")
            if "graph" in channels:
                relevance_reasons.append(f"graph traversal ({len(rel_path)} hop{'s' if len(rel_path) > 1 else ''})")
            if s_recency > 0.6:
                relevance_reasons.append("recent engineering knowledge")

            rel_info = f"Matched via {', '.join(relevance_reasons)}"

            # Format traversal path string for provenance
            path_str = " -> ".join([h.format_hop() for h in rel_path]) if rel_path else None

            provenance = ResultProvenance(
                source_type=source,
                repository_id=repository_id,
                file_path=code_path,
                document_path=doc_path,
                section=section,
                line_start=line_start,
                line_end=line_end,
                dataset_id=dataset_id,
                created_at=created_at,
                channels=channels,
                matched_terms=matched_terms,
                graph_hops_count=len(rel_path),
                traversal_path=path_str,
            )

            scores_breakdown = {
                "lexical": s_lex,
                "semantic": s_sem,
                "graph": s_graph,
                "recency": s_recency,
                "rrf": rrf_score,
                "hybrid": final_hybrid_score,
            }

            fused_results.append(
                HybridRetrievalResult(
                    entity=entity_name,
                    type=entity_type,
                    source=source,
                    relationship_path=rel_path,
                    relevance_information=rel_info,
                    repository=repository_id,
                    file_or_document=file_or_doc,
                    confidence=conf_val,
                    score=round(final_hybrid_score, 4),
                    scores_breakdown=scores_breakdown,
                    provenance=provenance,
                    content_snippet=content_snippet,
                    node_id=nid,
                    metadata=metadata,
                )
            )

        # Sort descending by fused score, then confidence
        fused_results.sort(
            key=lambda r: (-r.score, -(r.confidence or 0.0), r.entity)
        )
        final_selected = fused_results[:limit]

        retrieval_stats = {
            "query": clean_query,
            "total_candidates": len(all_node_ids),
            "lexical_matches_count": len(lexical_map),
            "semantic_matches_count": len(semantic_map),
            "graph_traversed_count": len(graph_map),
            "returned_count": len(final_selected),
            "active_channels": ["lexical", "semantic", "graph"],
        }

        return HybridQueryResponse(
            query=clean_query,
            user_id=user_id,
            repository_id=repository_id,
            results=final_selected,
            total_results=len(final_selected),
            query_terms=query_terms,
            retrieval_stats=retrieval_stats,
        )

    def close(self) -> None:
        """Close resources if owned."""
        if self._owns_retrieval:
            self.retrieval_service.close()

    def __enter__(self) -> HybridRetrievalService:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()


def hybrid_retrieve_engineering_memory(
    user_id: str,
    repository_id: str,
    query: str,
    *,
    retrieval_service: MemoryRetrievalService | None = None,
    store: Neo4jMemoryStore | None = None,
    driver: Any | None = None,
    embedding_provider: BaseEmbeddingProvider | None = None,
    limit: int = 10,
    max_hops: int = 2,
    symbols: list[CodeSymbol] | None = None,
    entities: list[DocumentEntity] | None = None,
    edges: list[GraphEdge] | None = None,
) -> HybridQueryResponse:
    """Convenience functional interface for multi-channel hybrid engineering memory retrieval."""
    service = HybridRetrievalService(
        retrieval_service=retrieval_service,
        store=store,
        driver=driver,
        embedding_provider=embedding_provider,
    )
    return service.retrieve(
        user_id=user_id,
        repository_id=repository_id,
        query=query,
        limit=limit,
        max_hops=max_hops,
        symbols=symbols,
        entities=entities,
        edges=edges,
    )
