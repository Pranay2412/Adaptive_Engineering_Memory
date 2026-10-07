"""Idempotent Neo4j persistence for the unified memory graph."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import Any

from dotenv import load_dotenv

from .models import CodeSymbol, DocumentEntity, GraphEdge

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
            "CREATE INDEX code_symbol_name IF NOT EXISTS FOR (c:CodeSymbol) ON (c.name)",
            "CREATE INDEX code_symbol_id IF NOT EXISTS FOR (c:CodeSymbol) ON (c.id)",
            "CREATE INDEX document_entity_id IF NOT EXISTS FOR (d:DocumentEntity) ON (d.id)",
        )
        with self.driver.session() as session:
            for statement in statements:
                session.run(statement).consume()

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
            }
            for symbol in symbols
        ]
        self._write_rows(
            "UNWIND $rows AS row MERGE (c:CodeSymbol {id: row.id}) "
            "SET c.name = row.name, c.file = row.file, c.type = row.type, "
            "c.user_id = row.user_id, c.repository_id = row.repository_id",
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
            }
            for entity in entities
        ]
        self._write_rows(
            "UNWIND $rows AS row MERGE (d:DocumentEntity {id: row.id}) "
            "SET d.name = row.name, d.type = row.type, "
            "d.user_id = row.user_id, d.repository_id = row.repository_id",
            rows,
        )
        return len(rows)

    def upsert_relationships(self, edges: Iterable[GraphEdge]) -> int:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for edge in edges:
            relation = edge.relation.upper()
            if relation not in RELATION_TYPES:
                raise ValueError(f"Unsupported relationship type: {edge.relation}")
            grouped.setdefault(relation, []).append(
                {
                    "source_id": edge.source_id,
                    "target_id": edge.target_id,
                    "metadata_json": json.dumps(edge.metadata, sort_keys=True),
                }
            )
        with self.driver.session() as session:
            for relation, rows in grouped.items():
                query = (
                    "UNWIND $rows AS row MATCH (source {id: row.source_id}) "
                    "MATCH (target {id: row.target_id}) "
                    f"MERGE (source)-[r:{relation}]->(target) "
                    "SET r.metadata_json = row.metadata_json"
                )
                self._run_write(session, query, rows)
        return sum(len(rows) for rows in grouped.values())

    def close(self) -> None:
        self.driver.close()

    def __enter__(self) -> "Neo4jMemoryStore":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()