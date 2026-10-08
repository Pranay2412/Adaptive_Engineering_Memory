"""Adaptive Engineering Memory Retrieval Service.

Executes natural language queries over the unified Graphify-Cognee memory graph
in Neo4j with multi-entity search, relationship resolution, and multi-hop traversal.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from typing import Any

from .db_loader import RELATION_TYPES, Neo4jMemoryStore
from .models import (
    MemoryQueryResponse,
    RelationshipHop,
    RetrievalResult,
)
from .normalization import tokenize_name

logger = logging.getLogger(__name__)

DEFAULT_TRAVERSAL_RELATIONS = {"SPECIFIES", "CALLS", "IMPORTS", "DEPENDS_ON"}

STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "do",
    "does",
    "for",
    "from",
    "how",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
}


def _get_val(record: Any, key: str, default: Any = None) -> Any:
    """Extract a property value from a Neo4j Record or dictionary safely."""
    if isinstance(record, dict):
        return record.get(key, default)
    if hasattr(record, "get"):
        val = record.get(key)
        return val if val is not None else default
    if hasattr(record, "__getitem__"):
        try:
            return record[key]
        except (KeyError, IndexError, TypeError):
            pass
    return default


def _extract_properties(obj: Any) -> dict[str, Any]:
    """Extract properties dictionary from a Neo4j Node/Relationship or dict."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "_properties"):
        return dict(obj._properties)
    if hasattr(obj, "items"):
        return dict(obj.items())
    return {}


class MemoryRetrievalService:
    """First retrieval layer for Adaptive Engineering Memory.

    Supports:
    - Code symbol search
    - Document entity search
    - SPECIFIES cross-linking resolution
    - CALLS / IMPORTS / DEPENDS_ON relationship graph expansion
    - Multi-hop traversal with path reconstruction
    """

    def __init__(
        self,
        store: Neo4jMemoryStore | None = None,
        driver: Any | None = None,
    ) -> None:
        if store is not None:
            self.store = store
            self.driver = store.driver
            self._owns_store = False
        else:
            self.store = Neo4jMemoryStore(driver=driver)
            self.driver = self.store.driver
            self._owns_store = True

    def extract_query_terms(self, query: str) -> list[str]:
        """Extract significant terms and identifier sub-tokens from a natural-language query."""
        if not query or not query.strip():
            return []

        cleaned = query.strip()
        words = re.findall(r"[A-Za-z0-9_\-\.\:\/]+", cleaned)

        terms_set: set[str] = set()
        for word in words:
            word_lower = word.lower().strip(".:-_/")
            if not word_lower or word_lower in STOP_WORDS or len(word_lower) < 2:
                continue

            terms_set.add(word_lower)

            # Tokenize CamelCase / snake_case into sub-identifiers
            sub_tokens = tokenize_name(word)
            for sub in sub_tokens:
                sub_lower = sub.lower()
                if sub_lower not in STOP_WORDS and len(sub_lower) >= 2:
                    terms_set.add(sub_lower)

        # Sort longer, more specific terms first
        terms_list = sorted(terms_set, key=lambda t: (-len(t), t))
        return terms_list

    def search_code_symbols(
        self,
        user_id: str,
        repository_id: str,
        query: str,
        terms: list[str],
        *,
        limit: int = 10,
    ) -> list[RetrievalResult]:
        """Search code symbols matching query text and terms."""
        cypher = (
            "MATCH (c:CodeSymbol) "
            "WHERE c.user_id = $user_id AND c.repository_id = $repository_id "
            "  AND ( "
            "    toLower(c.name) CONTAINS toLower($query_text) "
            "    OR any(t IN $terms WHERE toLower(c.name) CONTAINS t) "
            "    OR (c.qualified_name IS NOT NULL AND (toLower(c.qualified_name) CONTAINS toLower($query_text) OR any(t IN $terms WHERE toLower(c.qualified_name) CONTAINS t))) "
            "    OR (c.documentation IS NOT NULL AND any(t IN $terms WHERE toLower(c.documentation) CONTAINS t)) "
            "    OR (c.signature IS NOT NULL AND any(t IN $terms WHERE toLower(c.signature) CONTAINS t)) "
            "    OR (c.file IS NOT NULL AND any(t IN $terms WHERE toLower(c.file) CONTAINS t)) "
            "  ) "
            "RETURN c, labels(c) AS labels "
            "LIMIT $limit"
        )
        params = {
            "user_id": user_id,
            "repository_id": repository_id,
            "query_text": query,
            "terms": terms,
            "limit": limit,
        }

        results: list[RetrievalResult] = []
        with self.driver.session() as session:
            try:
                records = session.run(cypher, **params)
                for record in records:
                    raw_node = _get_val(record, "c")
                    props = _extract_properties(raw_node)
                    name = str(props.get("name") or "UnnamedSymbol")
                    sym_type = str(props.get("type") or "symbol")
                    node_id = str(props.get("id") or "")
                    file_path = str(props.get("file") or props.get("source_file") or "")
                    qual_name = props.get("qualified_name")
                    doc = props.get("documentation")

                    # Calculate lexical relevance score
                    name_lower = name.lower()
                    query_lower = query.lower()
                    score = 0.5
                    relevance_details = []

                    if name_lower == query_lower:
                        score = 1.0
                        relevance_details.append(f"Exact name match '{name}'")
                    elif query_lower in name_lower:
                        score = 0.9
                        relevance_details.append(f"Name contains query phrase '{query}'")
                    else:
                        matched_terms = [t for t in terms if t in name_lower]
                        if matched_terms:
                            score = max(score, 0.7 + 0.05 * len(matched_terms))
                            relevance_details.append(f"Name matched terms {matched_terms}")

                    if qual_name and any(t in qual_name.lower() for t in terms):
                        score = max(score, 0.65)
                        relevance_details.append(f"Qualified name: {qual_name}")

                    if doc and any(t in doc.lower() for t in terms):
                        score = max(score, 0.55)
                        relevance_details.append("Matched docstring/comments")

                    relevance_info = "; ".join(relevance_details) if relevance_details else f"Code symbol match for '{query}'"

                    res = RetrievalResult(
                        entity=name,
                        type=f"CodeSymbol ({sym_type})",
                        source="code",
                        relationship_path=[],
                        relevance_information=f"Direct code symbol: {relevance_info}",
                        repository=repository_id,
                        file_or_document=file_path,
                        confidence=1.0,
                        node_id=node_id,
                        score=round(score, 4),
                        metadata=props,
                    )
                    results.append(res)
            except Exception as e:
                logger.warning("Code symbol search query failed: %s", e)

        results.sort(key=lambda r: -r.score)
        return results

    def search_document_entities(
        self,
        user_id: str,
        repository_id: str,
        query: str,
        terms: list[str],
        *,
        limit: int = 10,
    ) -> list[RetrievalResult]:
        """Search document entities matching query text and terms."""
        cypher = (
            "MATCH (d:DocumentEntity) "
            "WHERE d.user_id = $user_id AND d.repository_id = $repository_id "
            "  AND ( "
            "    toLower(d.name) CONTAINS toLower($query_text) "
            "    OR any(t IN $terms WHERE toLower(d.name) CONTAINS t) "
            "    OR (d.description IS NOT NULL AND any(t IN $terms WHERE toLower(d.description) CONTAINS t)) "
            "    OR (d.source_chunk IS NOT NULL AND any(t IN $terms WHERE toLower(d.source_chunk) CONTAINS t)) "
            "    OR (d.section IS NOT NULL AND any(t IN $terms WHERE toLower(d.section) CONTAINS t)) "
            "    OR (d.source_document IS NOT NULL AND any(t IN $terms WHERE toLower(d.source_document) CONTAINS t)) "
            "    OR (d.document_path IS NOT NULL AND any(t IN $terms WHERE toLower(d.document_path) CONTAINS t)) "
            "  ) "
            "RETURN d, labels(d) AS labels "
            "LIMIT $limit"
        )
        params = {
            "user_id": user_id,
            "repository_id": repository_id,
            "query_text": query,
            "terms": terms,
            "limit": limit,
        }

        results: list[RetrievalResult] = []
        with self.driver.session() as session:
            try:
                records = session.run(cypher, **params)
                for record in records:
                    raw_node = _get_val(record, "d")
                    props = _extract_properties(raw_node)
                    name = str(props.get("name") or "UnnamedEntity")
                    ent_type = str(props.get("type") or "concept")
                    node_id = str(props.get("id") or "")
                    doc_path = str(
                        props.get("document_path")
                        or props.get("source_document")
                        or ""
                    )
                    desc = props.get("description")
                    section = props.get("section")
                    chunk = props.get("source_chunk")

                    name_lower = name.lower()
                    query_lower = query.lower()
                    score = 0.5
                    relevance_details = []

                    if name_lower == query_lower:
                        score = 1.0
                        relevance_details.append(f"Exact concept match '{name}'")
                    elif query_lower in name_lower:
                        score = 0.9
                        relevance_details.append(f"Concept name contains query phrase '{query}'")
                    else:
                        matched_terms = [t for t in terms if t in name_lower]
                        if matched_terms:
                            score = max(score, 0.7 + 0.05 * len(matched_terms))
                            relevance_details.append(f"Concept matched terms {matched_terms}")

                    if desc and any(t in desc.lower() for t in terms):
                        score = max(score, 0.6)
                        relevance_details.append("Matched description")

                    if section and any(t in section.lower() for t in terms):
                        score = max(score, 0.55)
                        relevance_details.append(f"Section: {section}")

                    if chunk and any(t in chunk.lower() for t in terms):
                        score = max(score, 0.5)
                        relevance_details.append("Matched source text context")

                    relevance_info = "; ".join(relevance_details) if relevance_details else f"Documentation entity match for '{query}'"

                    res = RetrievalResult(
                        entity=name,
                        type=f"DocumentEntity ({ent_type})",
                        source="documentation",
                        relationship_path=[],
                        relevance_information=f"Direct documentation concept: {relevance_info}",
                        repository=repository_id,
                        file_or_document=doc_path,
                        confidence=1.0,
                        node_id=node_id,
                        score=round(score, 4),
                        metadata=props,
                    )
                    results.append(res)
            except Exception as e:
                logger.warning("Document entity search query failed: %s", e)

        results.sort(key=lambda r: -r.score)
        return results

    def traverse_relationships(
        self,
        user_id: str,
        repository_id: str,
        seed_results: list[RetrievalResult],
        *,
        max_hops: int = 2,
        rel_types: set[str] | None = None,
        min_confidence: float = 0.0,
        hop_limit: int = 25,
    ) -> list[RetrievalResult]:
        """Perform multi-hop traversal from seed results along supported relationships."""
        if max_hops < 1 or not seed_results:
            return []

        active_relations = list(rel_types or DEFAULT_TRAVERSAL_RELATIONS)
        visited_ids: set[str] = {r.node_id for r in seed_results if r.node_id}
        current_frontier: list[tuple[RetrievalResult, list[RelationshipHop]]] = [
            (seed, []) for seed in seed_results if seed.node_id
        ]
        all_traversed: list[RetrievalResult] = []

        cypher = (
            "MATCH (seed)-[r]-(target) "
            "WHERE seed.id IN $seed_ids "
            "  AND type(r) IN $rel_types "
            "  AND target.user_id = $user_id "
            "  AND target.repository_id = $repository_id "
            "  AND target.id <> seed.id "
            "RETURN seed.id AS seed_id, "
            "       seed.name AS seed_name, "
            "       labels(seed) AS seed_labels, "
            "       type(r) AS rel_type, "
            "       r AS rel, "
            "       startNode(r).id = seed.id AS is_outgoing, "
            "       target.id AS target_id, "
            "       target.name AS target_name, "
            "       labels(target) AS target_labels, "
            "       target AS target_node "
            "LIMIT $limit"
        )

        with self.driver.session() as session:
            for current_hop in range(1, max_hops + 1):
                if not current_frontier:
                    break

                frontier_map = {res.node_id: (res, path) for res, path in current_frontier}
                frontier_ids = list(frontier_map.keys())

                params = {
                    "seed_ids": frontier_ids,
                    "rel_types": active_relations,
                    "user_id": user_id,
                    "repository_id": repository_id,
                    "limit": hop_limit,
                }

                next_frontier: list[tuple[RetrievalResult, list[RelationshipHop]]] = []

                try:
                    records = session.run(cypher, **params)
                    for record in records:
                        seed_id = str(_get_val(record, "seed_id") or "")
                        rel_type = str(_get_val(record, "rel_type") or "")
                        raw_rel = _get_val(record, "rel")
                        rel_props = _extract_properties(raw_rel)
                        target_id = str(_get_val(record, "target_id") or "")
                        target_name = str(_get_val(record, "target_name") or "UnnamedNode")
                        raw_target = _get_val(record, "target_node")
                        target_props = _extract_properties(raw_target)
                        target_labels = list(_get_val(record, "target_labels") or [])
                        is_outgoing = bool(_get_val(record, "is_outgoing", True))

                        parent_tuple = frontier_map.get(seed_id)
                        if not parent_tuple:
                            continue
                        parent_res, parent_path = parent_tuple

                        # Confidence extraction
                        conf_val = rel_props.get("confidence")
                        if conf_val is None and "metadata_json" in rel_props:
                            try:
                                meta_parsed = json.loads(rel_props["metadata_json"])
                                conf_val = meta_parsed.get("confidence")
                            except Exception:
                                pass

                        confidence = float(conf_val) if conf_val is not None else (1.0 if rel_type != "SPECIFIES" else 0.85)
                        if confidence < min_confidence:
                            continue

                        matching_method = rel_props.get("matching_method")
                        explanation = rel_props.get("explanation")

                        # Determine hop source/target based on direction
                        if is_outgoing:
                            hop = RelationshipHop(
                                source_id=seed_id,
                                source_name=parent_res.entity,
                                relation=rel_type,
                                target_id=target_id,
                                target_name=target_name,
                                confidence=confidence,
                                matching_method=matching_method,
                                explanation=explanation,
                                metadata=rel_props,
                            )
                        else:
                            hop = RelationshipHop(
                                source_id=target_id,
                                source_name=target_name,
                                relation=rel_type,
                                target_id=seed_id,
                                target_name=parent_res.entity,
                                confidence=confidence,
                                matching_method=matching_method,
                                explanation=explanation,
                                metadata=rel_props,
                            )

                        new_path = [*parent_path, hop]

                        if target_id in visited_ids:
                            # Already visited: append path info if existing result doesn't have it
                            for existing in all_traversed:
                                if existing.node_id == target_id and not existing.relationship_path:
                                    existing.relationship_path = new_path
                            continue

                        visited_ids.add(target_id)

                        # Determine target type and source
                        if "CodeSymbol" in target_labels:
                            target_type = f"CodeSymbol ({target_props.get('type', 'symbol')})"
                            source = "code"
                            file_doc = target_props.get("file") or target_props.get("source_file") or ""
                        elif "DocumentEntity" in target_labels:
                            target_type = f"DocumentEntity ({target_props.get('type', 'concept')})"
                            source = "documentation"
                            file_doc = target_props.get("document_path") or target_props.get("source_document") or ""
                        elif "Module" in target_labels:
                            target_type = "Module"
                            source = "code"
                            file_doc = target_props.get("path") or ""
                        else:
                            target_type = "Entity"
                            source = "knowledge_graph"
                            file_doc = target_props.get("file") or target_props.get("path") or ""

                        # Score decay based on hops and confidence
                        decay = 0.8 ** current_hop
                        traversed_score = parent_res.score * decay * confidence

                        # Compose relevance explanation
                        if rel_type == "SPECIFIES":
                            if "DocumentEntity" in str(parent_res.type):
                                rel_info = (
                                    f"Code implementation specifying documentation concept '{parent_res.entity}' "
                                    f"via SPECIFIES [confidence: {confidence:.2f}]"
                                )
                            else:
                                rel_info = (
                                    f"Documentation concept specifying code symbol '{parent_res.entity}' "
                                    f"via SPECIFIES [confidence: {confidence:.2f}]"
                                )
                        elif rel_type == "CALLS":
                            rel_info = (
                                f"Connected via CALLS from '{parent_res.entity}' (hop {current_hop})"
                                if is_outgoing
                                else f"Caller of '{parent_res.entity}' via CALLS (hop {current_hop})"
                            )
                        elif rel_type == "IMPORTS":
                            rel_info = f"Import dependency connected via IMPORTS to '{parent_res.entity}' (hop {current_hop})"
                        elif rel_type == "DEPENDS_ON":
                            rel_info = f"Component dependency connected via DEPENDS_ON to '{parent_res.entity}' (hop {current_hop})"
                        else:
                            rel_info = f"Graph connection via {rel_type} from '{parent_res.entity}' (hop {current_hop})"

                        traversed_res = RetrievalResult(
                            entity=target_name,
                            type=target_type,
                            source=source,
                            relationship_path=new_path,
                            relevance_information=rel_info,
                            repository=repository_id,
                            file_or_document=str(file_doc),
                            confidence=confidence,
                            node_id=target_id,
                            score=round(traversed_score, 4),
                            metadata=target_props,
                        )

                        all_traversed.append(traversed_res)
                        next_frontier.append((traversed_res, new_path))

                except Exception as e:
                    logger.warning("Graph traversal query failed at hop %d: %s", current_hop, e)

                current_frontier = next_frontier

        return all_traversed

    def query(
        self,
        user_id: str,
        repository_id: str,
        query: str,
        *,
        limit: int = 10,
        max_hops: int = 2,
        include_traversal: bool = True,
        min_confidence: float = 0.0,
        relationship_types: list[str] | set[str] | None = None,
    ) -> list[RetrievalResult]:
        """Execute natural-language retrieval over the scoped engineering memory."""
        if not user_id or not repository_id or not query or not query.strip():
            return []

        clean_query = query.strip()
        terms = self.extract_query_terms(clean_query)

        # 1. Direct Search: Code Symbols & Document Entities (Seed nodes)
        code_matches = self.search_code_symbols(
            user_id=user_id,
            repository_id=repository_id,
            query=clean_query,
            terms=terms,
            limit=limit,
        )
        doc_matches = self.search_document_entities(
            user_id=user_id,
            repository_id=repository_id,
            query=clean_query,
            terms=terms,
            limit=limit,
        )

        seed_results = [*code_matches, *doc_matches]

        # 2. Graph Traversal: Expand along SPECIFIES, CALLS, IMPORTS, DEPENDS_ON
        traversed_results: list[RetrievalResult] = []
        if include_traversal and max_hops >= 1 and seed_results:
            rel_types = set(relationship_types) if relationship_types else DEFAULT_TRAVERSAL_RELATIONS
            traversed_results = self.traverse_relationships(
                user_id=user_id,
                repository_id=repository_id,
                seed_results=seed_results[:5],  # Expand top 5 most relevant seeds
                max_hops=max_hops,
                rel_types=rel_types,
                min_confidence=min_confidence,
                hop_limit=limit * 2,
            )

        # 3. Deduplicate and merge results
        merged_by_id: dict[str, RetrievalResult] = {}

        # Add seeds first (preserving direct match status and high scores)
        for res in seed_results:
            merged_by_id[res.node_id] = res

        # Merge traversed results
        for res in traversed_results:
            if res.node_id not in merged_by_id:
                merged_by_id[res.node_id] = res
            else:
                existing = merged_by_id[res.node_id]
                # If existing direct match lacked relationship path, attach the path
                if not existing.relationship_path and res.relationship_path:
                    existing.relationship_path = res.relationship_path
                # Keep maximum score
                existing.score = max(existing.score, res.score)

        final_results = list(merged_by_id.values())
        final_results.sort(key=lambda r: (-r.score, -(r.confidence or 0.0), r.entity))

        return final_results[:limit]

    def query_response(
        self,
        user_id: str,
        repository_id: str,
        query: str,
        *,
        limit: int = 10,
        max_hops: int = 2,
        include_traversal: bool = True,
        min_confidence: float = 0.0,
        relationship_types: list[str] | set[str] | None = None,
    ) -> MemoryQueryResponse:
        """Execute query and return wrapped MemoryQueryResponse container."""
        terms = self.extract_query_terms(query)
        results = self.query(
            user_id=user_id,
            repository_id=repository_id,
            query=query,
            limit=limit,
            max_hops=max_hops,
            include_traversal=include_traversal,
            min_confidence=min_confidence,
            relationship_types=relationship_types,
        )
        return MemoryQueryResponse(
            query=query,
            user_id=user_id,
            repository_id=repository_id,
            results=results,
            total_results=len(results),
            query_terms=terms,
        )

    def close(self) -> None:
        """Close the underlying Neo4j driver connection if owned."""
        if self._owns_store:
            self.store.close()

    def __enter__(self) -> MemoryRetrievalService:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()


def query_engineering_memory(
    user_id: str,
    repository_id: str,
    query: str,
    *,
    store: Neo4jMemoryStore | None = None,
    limit: int = 10,
    max_hops: int = 2,
    include_traversal: bool = True,
    min_confidence: float = 0.0,
    relationship_types: list[str] | set[str] | None = None,
) -> list[RetrievalResult]:
    """Convenience functional interface for engineering memory retrieval."""
    service = MemoryRetrievalService(store=store)
    try:
        return service.query(
            user_id=user_id,
            repository_id=repository_id,
            query=query,
            limit=limit,
            max_hops=max_hops,
            include_traversal=include_traversal,
            min_confidence=min_confidence,
            relationship_types=relationship_types,
        )
    finally:
        if store is None:
            service.close()
