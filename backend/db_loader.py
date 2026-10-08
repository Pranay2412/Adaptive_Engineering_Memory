"""Idempotent Neo4j persistence for the unified memory graph."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import Any

from dotenv import load_dotenv

from .models import (
    CodeSymbol,
    Document,
    DocumentEntity,
    GraphEdge,
    Module,
    Repository,
    scoped_id,
)

RELATION_TYPES = {"CALLS", "IMPORTS", "INHERITS", "DEFINES", "MOTIVATES", "DEPENDS_ON", "SPECIFIES"}


class Neo4jMemoryStore:
    """Store memory nodes and relationships in a shared Neo4j database."""

    def __init__(
        self,
        uri: str | None = None,
        username: str | None = None,
        password: str | None = None,
        *,
        driver: Any | None = None,
    ) -> None:
        load_dotenv()
        if driver is None:
            from neo4j import GraphDatabase

            uri = uri or os.getenv("NEO4J_URI", "bolt://localhost:7687")
            username = username or os.getenv("NEO4J_USERNAME", "neo4j")
            password = password or os.getenv("NEO4J_PASSWORD", "password123")
            driver = GraphDatabase.driver(uri, auth=(username, password))
        self.driver = driver

    def initialize_schema(self) -> None:
        statements = (
            # CodeSymbol indexes
            "CREATE INDEX code_symbol_id IF NOT EXISTS FOR (c:CodeSymbol) ON (c.id)",
            "CREATE INDEX code_symbol_name IF NOT EXISTS FOR (c:CodeSymbol) ON (c.name)",
            "CREATE INDEX code_symbol_qualified_name IF NOT EXISTS FOR (c:CodeSymbol) ON (c.qualified_name)",
            "CREATE INDEX code_symbol_module IF NOT EXISTS FOR (c:CodeSymbol) ON (c.module)",
            "CREATE INDEX code_symbol_repo IF NOT EXISTS FOR (c:CodeSymbol) ON (c.repository_id)",
            # DocumentEntity indexes
            "CREATE INDEX document_entity_id IF NOT EXISTS FOR (d:DocumentEntity) ON (d.id)",
            "CREATE INDEX document_entity_name IF NOT EXISTS FOR (d:DocumentEntity) ON (d.name)",
            "CREATE INDEX document_entity_type IF NOT EXISTS FOR (d:DocumentEntity) ON (d.type)",
            "CREATE INDEX document_entity_document IF NOT EXISTS FOR (d:DocumentEntity) ON (d.source_document)",
            "CREATE INDEX document_entity_repo IF NOT EXISTS FOR (d:DocumentEntity) ON (d.repository_id)",
            # Repository indexes
            "CREATE INDEX repository_id IF NOT EXISTS FOR (r:Repository) ON (r.id)",
            "CREATE INDEX repository_name IF NOT EXISTS FOR (r:Repository) ON (r.name)",
            # Module indexes
            "CREATE INDEX module_id IF NOT EXISTS FOR (m:Module) ON (m.id)",
            "CREATE INDEX module_name IF NOT EXISTS FOR (m:Module) ON (m.name)",
            "CREATE INDEX module_repo IF NOT EXISTS FOR (m:Module) ON (m.repository_id)",
            # Document indexes
            "CREATE INDEX document_id IF NOT EXISTS FOR (doc:Document) ON (doc.id)",
            "CREATE INDEX document_path IF NOT EXISTS FOR (doc:Document) ON (doc.path)",
            "CREATE INDEX document_repo IF NOT EXISTS FOR (doc:Document) ON (doc.repository_id)",
        )
        with self.driver.session() as session:
            for statement in statements:
                session.run(statement).consume()

            # Relationship property index for SPECIFIES confidence (supported in Neo4j 4.3+)
            try:
                session.run(
                    "CREATE INDEX specifies_confidence IF NOT EXISTS FOR ()-[r:SPECIFIES]-() ON (r.confidence)"
                ).consume()
            except Exception:
                pass

    @staticmethod
    def _run_write(session: Any, query: str, rows: list[dict[str, Any]]) -> None:
        if rows:
            session.execute_write(lambda transaction: transaction.run(query, rows=rows).consume())

    def _write_rows(self, query: str, rows: list[dict[str, Any]]) -> None:
        if rows:
            with self.driver.session() as session:
                self._run_write(session, query, rows)

    def upsert_code_symbols(self, symbols: Iterable[CodeSymbol]) -> int:
        rows = [
            {
                "id": symbol.id,
                "name": symbol.name,
                "file": symbol.file,
                "type": symbol.type,
                "user_id": symbol.user_id,
                "repository_id": symbol.repository_id,
                "qualified_name": symbol.qualified_name,
                "module": symbol.module,
                "package": symbol.package,
                "source_file": symbol.source_file or symbol.file,
                "line_start": symbol.line_start,
                "line_end": symbol.line_end,
                "parent_symbol": symbol.parent_symbol,
                "signature": symbol.signature,
                "documentation": symbol.documentation,
                "language": symbol.language,
            }
            for symbol in symbols
        ]
        self._write_rows(
            "UNWIND $rows AS row MERGE (c:CodeSymbol {id: row.id}) "
            "SET c.name = row.name, c.file = row.file, c.type = row.type, "
            "c.user_id = row.user_id, c.repository_id = row.repository_id, "
            "c.qualified_name = row.qualified_name, c.module = row.module, "
            "c.package = row.package, c.source_file = row.source_file, "
            "c.line_start = row.line_start, c.line_end = row.line_end, "
            "c.parent_symbol = row.parent_symbol, c.signature = row.signature, "
            "c.documentation = row.documentation, c.language = row.language",
            rows,
        )
        return len(rows)

    def upsert_document_entities(self, entities: Iterable[DocumentEntity]) -> int:
        rows = [
            {
                "id": entity.id,
                "name": entity.name,
                "type": entity.type,
                "user_id": entity.user_id,
                "repository_id": entity.repository_id,
                "description": entity.description,
                "source_document": entity.source_document,
                "document_path": entity.document_path or entity.source_file,
                "section": entity.section,
                "source_chunk": entity.source_chunk,
                "dataset_id": entity.dataset_id,
                "metadata_json": json.dumps(entity.metadata, sort_keys=True) if entity.metadata else None,
            }
            for entity in entities
        ]
        self._write_rows(
            "UNWIND $rows AS row MERGE (d:DocumentEntity {id: row.id}) "
            "SET d.name = row.name, d.type = row.type, "
            "d.user_id = row.user_id, d.repository_id = row.repository_id, "
            "d.description = row.description, d.source_document = row.source_document, "
            "d.document_path = row.document_path, d.section = row.section, "
            "d.source_chunk = row.source_chunk, d.dataset_id = row.dataset_id, "
            "d.metadata_json = row.metadata_json",
            rows,
        )
        return len(rows)

    def upsert_repositories(self, repositories: Iterable[Repository]) -> int:
        rows = [
            {
                "id": repo.id,
                "name": repo.name,
                "user_id": repo.user_id,
                "repository_id": repo.repository_id or repo.id,
                "url": repo.url,
                "default_branch": repo.default_branch,
                "language": repo.language,
                "description": repo.description,
                "metadata_json": json.dumps(repo.metadata, sort_keys=True) if repo.metadata else None,
            }
            for repo in repositories
        ]
        self._write_rows(
            "UNWIND $rows AS row MERGE (r:Repository {id: row.id}) "
            "SET r.name = row.name, r.user_id = row.user_id, "
            "r.repository_id = row.repository_id, r.url = row.url, "
            "r.default_branch = row.default_branch, r.language = row.language, "
            "r.description = row.description, r.metadata_json = row.metadata_json",
            rows,
        )
        return len(rows)

    def upsert_modules(self, modules: Iterable[Module]) -> int:
        rows = [
            {
                "id": mod.id,
                "name": mod.name,
                "package": mod.package,
                "path": mod.path,
                "language": mod.language,
                "user_id": mod.user_id,
                "repository_id": mod.repository_id,
                "metadata_json": json.dumps(mod.metadata, sort_keys=True) if mod.metadata else None,
            }
            for mod in modules
        ]
        self._write_rows(
            "UNWIND $rows AS row MERGE (m:Module {id: row.id}) "
            "SET m.name = row.name, m.package = row.package, "
            "m.path = row.path, m.language = row.language, "
            "m.user_id = row.user_id, m.repository_id = row.repository_id, "
            "m.metadata_json = row.metadata_json",
            rows,
        )
        return len(rows)

    def upsert_documents(self, documents: Iterable[Document]) -> int:
        rows = [
            {
                "id": doc.id,
                "name": doc.name,
                "path": doc.path,
                "format": doc.format,
                "dataset_id": doc.dataset_id,
                "user_id": doc.user_id,
                "repository_id": doc.repository_id,
                "metadata_json": json.dumps(doc.metadata, sort_keys=True) if doc.metadata else None,
            }
            for doc in documents
        ]
        self._write_rows(
            "UNWIND $rows AS row MERGE (doc:Document {id: row.id}) "
            "SET doc.name = row.name, doc.path = row.path, "
            "doc.format = row.format, doc.dataset_id = row.dataset_id, "
            "doc.user_id = row.user_id, doc.repository_id = row.repository_id, "
            "doc.metadata_json = row.metadata_json",
            rows,
        )
        return len(rows)

    def upsert_relationships(self, edges: Iterable[GraphEdge]) -> int:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for edge in edges:
            relation = edge.relation.upper()
            if relation not in RELATION_TYPES:
                raise ValueError(f"Unsupported relationship type: {edge.relation}")

            meta = edge.metadata or {}
            row: dict[str, Any] = {
                "source_id": edge.source_id,
                "target_id": edge.target_id,
                "metadata_json": json.dumps(edge.metadata, sort_keys=True),
            }

            if relation == "SPECIFIES":
                confidence = float(meta.get("confidence", 0.0))
                matching_method = str(meta.get("matching_method") or "unknown")
                scores = meta.get("scores")
                if scores is None:
                    scores = {
                        "lexical": meta.get("lexical_score"),
                        "semantic": meta.get("semantic_score"),
                        "context": meta.get("context_score"),
                        "type": meta.get("type_score"),
                        "final": confidence,
                    }
                scores_json = json.dumps(scores, sort_keys=True) if isinstance(scores, dict) else str(scores)
                explanation = str(meta.get("explanation") or "")
                created_at = str(meta.get("created_at") or "")
                source = str(meta.get("source") or "cross_link_bridge")

                row["confidence"] = confidence
                row["matching_method"] = matching_method
                row["scores"] = scores_json
                row["explanation"] = explanation
                row["created_at"] = created_at
                row["source"] = source

            grouped.setdefault(relation, []).append(row)

        with self.driver.session() as session:
            for relation, rows in grouped.items():
                if relation == "SPECIFIES":
                    query = (
                        "UNWIND $rows AS row MATCH (source {id: row.source_id}) "
                        "MATCH (target {id: row.target_id}) "
                        "MERGE (source)-[r:SPECIFIES]->(target) "
                        "SET r.metadata_json = row.metadata_json, "
                        "r.confidence = row.confidence, "
                        "r.matching_method = row.matching_method, "
                        "r.scores = row.scores, "
                        "r.explanation = row.explanation, "
                        "r.created_at = row.created_at, "
                        "r.source = row.source"
                    )
                else:
                    query = (
                        "UNWIND $rows AS row MATCH (source {id: row.source_id}) "
                        "MATCH (target {id: row.target_id}) "
                        f"MERGE (source)-[r:{relation}]->(target) "
                        "SET r.metadata_json = row.metadata_json"
                    )
                self._run_write(session, query, rows)
        return sum(len(rows) for rows in grouped.values())

    def sync_structural_hierarchy(
        self,
        repository_id: str,
        user_id: str,
        repository_name: str | None = None,
        symbols: Iterable[CodeSymbol] = (),
        entities: Iterable[DocumentEntity] = (),
    ) -> dict[str, int]:
        """Derive and upsert Repository, Module, Document nodes and structural DEFINES edges."""
        # 1. Upsert Repository
        repo = Repository(
            id=repository_id,
            name=repository_name or repository_id,
            user_id=user_id,
            repository_id=repository_id,
        )
        repo_count = self.upsert_repositories([repo])

        # 2. Derive Modules from CodeSymbols
        modules_by_name: dict[str, Module] = {}
        module_edges: list[GraphEdge] = []
        for symbol in symbols:
            mod_name = symbol.module
            if mod_name:
                mod_id = scoped_id(user_id, repository_id, mod_name)
                if mod_name not in modules_by_name:
                    modules_by_name[mod_name] = Module(
                        id=mod_id,
                        name=mod_name,
                        package=symbol.package,
                        path=symbol.file,
                        language=symbol.language,
                        user_id=user_id,
                        repository_id=repository_id,
                    )
                    # Repository DEFINES Module
                    module_edges.append(
                        GraphEdge(
                            source_id=repository_id,
                            target_id=mod_id,
                            relation="DEFINES",
                            metadata={"type": "repository_module"},
                        )
                    )
                # Module DEFINES CodeSymbol
                module_edges.append(
                    GraphEdge(
                        source_id=mod_id,
                        target_id=symbol.id,
                        relation="DEFINES",
                        metadata={"type": "module_symbol"},
                    )
                )
            else:
                # Direct link: Repository DEFINES CodeSymbol
                module_edges.append(
                    GraphEdge(
                        source_id=repository_id,
                        target_id=symbol.id,
                        relation="DEFINES",
                        metadata={"type": "repository_symbol"},
                    )
                )

        mod_count = self.upsert_modules(modules_by_name.values())

        # 3. Derive Documents from DocumentEntities
        docs_by_path: dict[str, Document] = {}
        doc_edges: list[GraphEdge] = []
        for entity in entities:
            doc_path = entity.document_path or entity.source_file
            if doc_path:
                doc_id = scoped_id(user_id, repository_id, doc_path)
                if doc_path not in docs_by_path:
                    docs_by_path[doc_path] = Document(
                        id=doc_id,
                        name=entity.source_document or doc_path,
                        path=doc_path,
                        dataset_id=entity.dataset_id,
                        user_id=user_id,
                        repository_id=repository_id,
                    )
                    # Repository DEFINES Document
                    doc_edges.append(
                        GraphEdge(
                            source_id=repository_id,
                            target_id=doc_id,
                            relation="DEFINES",
                            metadata={"type": "repository_document"},
                        )
                    )
                # Document DEFINES DocumentEntity
                doc_edges.append(
                    GraphEdge(
                        source_id=doc_id,
                        target_id=entity.id,
                        relation="DEFINES",
                        metadata={"type": "document_entity"},
                    )
                )
            else:
                # Direct link: Repository DEFINES DocumentEntity
                doc_edges.append(
                    GraphEdge(
                        source_id=repository_id,
                        target_id=entity.id,
                        relation="DEFINES",
                        metadata={"type": "repository_entity"},
                    )
                )

        doc_count = self.upsert_documents(docs_by_path.values())
        structural_edge_count = self.upsert_relationships([*module_edges, *doc_edges])

        return {
            "repositories": repo_count,
            "modules": mod_count,
            "documents": doc_count,
            "structural_edges": structural_edge_count,
        }

    def migrate_legacy_schema(self) -> dict[str, int]:
        """Backfill legacy SPECIFIES relationship properties from metadata_json."""
        self.initialize_schema()
        migrated_specifies = 0

        query = (
            "MATCH (s)-[r:SPECIFIES]->(t) "
            "WHERE r.metadata_json IS NOT NULL AND (r.confidence IS NULL OR r.matching_method IS NULL) "
            "RETURN s.id AS source_id, t.id AS target_id, r.metadata_json AS metadata_json"
        )
        rows_to_update: list[dict[str, Any]] = []
        with self.driver.session() as session:
            try:
                result = session.run(query)
                for record in result:
                    source_id = record["source_id"]
                    target_id = record["target_id"]
                    raw_meta = record["metadata_json"]
                    try:
                        meta = json.loads(raw_meta) if isinstance(raw_meta, str) else (raw_meta or {})
                    except Exception:
                        meta = {}

                    confidence = float(meta.get("confidence", 0.0))
                    matching_method = str(meta.get("matching_method") or "legacy")
                    scores = meta.get("scores")
                    if scores is None:
                        scores = {
                            "lexical": meta.get("lexical_score"),
                            "semantic": meta.get("semantic_score"),
                            "context": meta.get("context_score"),
                            "type": meta.get("type_score"),
                            "final": confidence,
                        }
                    scores_json = json.dumps(scores, sort_keys=True) if isinstance(scores, dict) else str(scores)
                    explanation = str(meta.get("explanation") or "")
                    created_at = str(meta.get("created_at") or "")
                    source = str(meta.get("source") or "legacy_backfill")

                    rows_to_update.append(
                        {
                            "source_id": source_id,
                            "target_id": target_id,
                            "confidence": confidence,
                            "matching_method": matching_method,
                            "scores": scores_json,
                            "explanation": explanation,
                            "created_at": created_at,
                            "source": source,
                            "metadata_json": raw_meta,
                        }
                    )
            except Exception:
                pass

            if rows_to_update:
                update_query = (
                    "UNWIND $rows AS row MATCH (source {id: row.source_id}) "
                    "MATCH (target {id: row.target_id}) "
                    "MATCH (source)-[r:SPECIFIES]->(target) "
                    "SET r.confidence = row.confidence, "
                    "r.matching_method = row.matching_method, "
                    "r.scores = row.scores, "
                    "r.explanation = row.explanation, "
                    "r.created_at = row.created_at, "
                    "r.source = row.source"
                )
                self._run_write(session, update_query, rows_to_update)
                migrated_specifies = len(rows_to_update)

        return {"migrated_specifies": migrated_specifies}

    def close(self) -> None:
        self.driver.close()

    def __enter__(self) -> "Neo4jMemoryStore":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()