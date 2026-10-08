"""Idempotent Neo4j persistence for the unified memory graph."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import Any

from dotenv import load_dotenv

from .models import (
    AISession,
    AISessionRecord,
    CodeSymbol,
    Document,
    DocumentEntity,
    EngineeringDecision,
    EngineeringEvent,
    GraphEdge,
    Memory,
    MemoryProvenance,
    Module,
    Project,
    Repository,
    Team,
    TeamMembership,
    User,
    scoped_id,
)

RELATION_TYPES = {
    # Core code & doc relationships
    "CALLS",
    "IMPORTS",
    "INHERITS",
    "DEFINES",
    "MOTIVATES",
    "DEPENDS_ON",
    "SPECIFIES",
    # Team memory relationships
    "MEMBER_OF",
    "OWNS_PROJECT",
    "CONTAINS_REPO",
    "INITIATED_SESSION",
    "IN_REPOSITORY",
    "AUTHORED",
    "SHARED_WITH_TEAM",
    "SCOPED_TO_REPO",
    "PART_OF_PROJECT",
    "EXTRACTED_FROM_SESSION",
    "ORIGINATED_FROM_EVENT",
    "JUSTIFIED_BY",
    "REFERENCES_SYMBOL",
    "REFERENCES_DOC",
    "SUPERSEDES",
    "AUTHORED_BY",
    "AFFECTS_REPO",
    "APPLIES_TO_TEAM",
    "RELATES_TO_EVENT",
    "TOUCHES_SYMBOL",
    "TOUCHES_FILE",
}


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
            # User indexes
            "CREATE INDEX user_id IF NOT EXISTS FOR (u:User) ON (u.id)",
            "CREATE INDEX user_email IF NOT EXISTS FOR (u:User) ON (u.email)",
            # Team indexes
            "CREATE INDEX team_id IF NOT EXISTS FOR (t:Team) ON (t.id)",
            "CREATE INDEX team_name IF NOT EXISTS FOR (t:Team) ON (t.name)",
            # Project indexes
            "CREATE INDEX project_id IF NOT EXISTS FOR (p:Project) ON (p.id)",
            "CREATE INDEX project_team IF NOT EXISTS FOR (p:Project) ON (p.team_id)",
            # AISession indexes
            "CREATE INDEX ai_session_id IF NOT EXISTS FOR (s:AISession) ON (s.id)",
            "CREATE INDEX ai_session_user IF NOT EXISTS FOR (s:AISession) ON (s.user_id)",
            "CREATE INDEX ai_session_repo IF NOT EXISTS FOR (s:AISession) ON (s.repository_id)",
            # EngineeringEvent indexes
            "CREATE INDEX eng_event_id IF NOT EXISTS FOR (e:EngineeringEvent) ON (e.id)",
            "CREATE INDEX eng_event_repo IF NOT EXISTS FOR (e:EngineeringEvent) ON (e.repository_id)",
            "CREATE INDEX eng_event_type IF NOT EXISTS FOR (e:EngineeringEvent) ON (e.event_type)",
            # EngineeringDecision indexes
            "CREATE INDEX eng_decision_id IF NOT EXISTS FOR (d:EngineeringDecision) ON (d.id)",
            "CREATE INDEX eng_decision_repo IF NOT EXISTS FOR (d:EngineeringDecision) ON (d.repository_id)",
            "CREATE INDEX eng_decision_team IF NOT EXISTS FOR (d:EngineeringDecision) ON (d.team_id)",
            "CREATE INDEX eng_decision_category IF NOT EXISTS FOR (d:EngineeringDecision) ON (d.category)",
            # Memory indexes
            "CREATE INDEX memory_id IF NOT EXISTS FOR (m:Memory) ON (m.id)",
            "CREATE INDEX memory_scope IF NOT EXISTS FOR (m:Memory) ON (m.scope)",
            "CREATE INDEX memory_category IF NOT EXISTS FOR (m:Memory) ON (m.category)",
            "CREATE INDEX memory_repo IF NOT EXISTS FOR (m:Memory) ON (m.repository_id)",
            "CREATE INDEX memory_team IF NOT EXISTS FOR (m:Memory) ON (m.team_id)",
            "CREATE INDEX memory_author IF NOT EXISTS FOR (m:Memory) ON (m.author_id)",
            "CREATE INDEX memory_status IF NOT EXISTS FOR (m:Memory) ON (m.status)",
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

    def upsert_users(self, users: Iterable[User]) -> int:
        rows = [
            {
                "id": u.id,
                "name": u.name,
                "email": u.email,
                "role": u.role,
                "created_at": u.created_at,
                "metadata_json": json.dumps(u.metadata, sort_keys=True) if u.metadata else None,
            }
            for u in users
        ]
        self._write_rows(
            "UNWIND $rows AS row MERGE (u:User {id: row.id}) "
            "SET u.name = row.name, u.email = row.email, u.role = row.role, "
            "u.created_at = row.created_at, u.metadata_json = row.metadata_json",
            rows,
        )
        return len(rows)

    def upsert_teams(self, teams: Iterable[Team]) -> int:
        rows = [
            {
                "id": t.id,
                "name": t.name,
                "description": t.description,
                "created_at": t.created_at,
                "metadata_json": json.dumps(t.metadata, sort_keys=True) if t.metadata else None,
            }
            for t in teams
        ]
        self._write_rows(
            "UNWIND $rows AS row MERGE (t:Team {id: row.id}) "
            "SET t.name = row.name, t.description = row.description, "
            "t.created_at = row.created_at, t.metadata_json = row.metadata_json",
            rows,
        )
        return len(rows)

    def upsert_team_memberships(self, memberships: Iterable[TeamMembership]) -> int:
        rows = [
            {
                "user_id": m.user_id,
                "team_id": m.team_id,
                "role": m.role,
                "joined_at": m.joined_at,
            }
            for m in memberships
        ]
        self._write_rows(
            "UNWIND $rows AS row MATCH (u:User {id: row.user_id}) "
            "MATCH (t:Team {id: row.team_id}) "
            "MERGE (u)-[r:MEMBER_OF]->(t) "
            "SET r.role = row.role, r.joined_at = row.joined_at",
            rows,
        )
        return len(rows)

    def upsert_projects(self, projects: Iterable[Project]) -> int:
        rows = [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "team_id": p.team_id,
                "created_at": p.created_at,
                "metadata_json": json.dumps(p.metadata, sort_keys=True) if p.metadata else None,
            }
            for p in projects
        ]
        self._write_rows(
            "UNWIND $rows AS row MERGE (p:Project {id: row.id}) "
            "SET p.name = row.name, p.description = row.description, "
            "p.team_id = row.team_id, p.created_at = row.created_at, "
            "p.metadata_json = row.metadata_json",
            rows,
        )
        team_rows = [r for r in rows if r.get("team_id")]
        if team_rows:
            self._write_rows(
                "UNWIND $rows AS row MATCH (t:Team {id: row.team_id}) "
                "MATCH (p:Project {id: row.id}) "
                "MERGE (t)-[:OWNS_PROJECT]->(p)",
                team_rows,
            )
        return len(rows)

    def upsert_ai_sessions(self, sessions: Iterable[AISession | AISessionRecord]) -> int:
        rows: list[dict[str, Any]] = []
        user_links: list[dict[str, Any]] = []
        repo_links: list[dict[str, Any]] = []
        team_links: list[dict[str, Any]] = []
        symbol_links: list[dict[str, Any]] = []
        file_links: list[dict[str, Any]] = []

        for s in sessions:
            sess_id = getattr(s, "session_id", getattr(s, "id", ""))
            user_id = getattr(s, "user_id", "")
            repo_id = getattr(s, "repository_id", None)
            team_id = getattr(s, "team_id", None)
            prompt = getattr(s, "prompt", "")
            resp_summary = getattr(s, "response_summary", getattr(s, "summary", ""))
            title = getattr(s, "title", prompt[:80] if prompt else sess_id)
            started_at = getattr(s, "timestamp", getattr(s, "started_at", ""))
            visibility = getattr(s, "visibility", "repository")
            meta = getattr(s, "metadata", {}) or {}

            rows.append(
                {
                    "id": sess_id,
                    "title": title,
                    "user_id": user_id,
                    "repository_id": repo_id,
                    "team_id": team_id,
                    "prompt": prompt,
                    "response_summary": resp_summary,
                    "visibility": visibility,
                    "started_at": started_at,
                    "summary": resp_summary,
                    "query_count": getattr(s, "query_count", 1),
                    "metadata_json": json.dumps(meta, sort_keys=True) if meta else None,
                }
            )
            if user_id:
                user_links.append({"user_id": user_id, "session_id": sess_id})
            if repo_id:
                repo_links.append({"repository_id": repo_id, "session_id": sess_id})
            if team_id:
                team_links.append({"team_id": team_id, "session_id": sess_id})

            affected_symbols = getattr(s, "affected_symbols", [])
            for sym in (affected_symbols or []):
                symbol_links.append({"session_id": sess_id, "symbol": sym})

            affected_files = getattr(s, "affected_files", [])
            for f in (affected_files or []):
                file_links.append({"session_id": sess_id, "file": f})

        self._write_rows(
            "UNWIND $rows AS row MERGE (s:AISession {id: row.id}) "
            "SET s.title = row.title, s.user_id = row.user_id, "
            "s.repository_id = row.repository_id, s.team_id = row.team_id, "
            "s.prompt = row.prompt, s.response_summary = row.response_summary, "
            "s.visibility = row.visibility, "
            "s.started_at = row.started_at, s.summary = row.summary, "
            "s.query_count = row.query_count, "
            "s.metadata_json = row.metadata_json",
            rows,
        )

        if user_links:
            self._write_rows(
                "UNWIND $rows AS row MATCH (u:User {id: row.user_id}) "
                "MATCH (s:AISession {id: row.session_id}) "
                "MERGE (u)-[:INITIATED_SESSION]->(s)",
                user_links,
            )
        if repo_links:
            self._write_rows(
                "UNWIND $rows AS row MATCH (s:AISession {id: row.session_id}) "
                "MATCH (r:Repository {id: row.repository_id}) "
                "MERGE (s)-[:IN_REPOSITORY]->(r)",
                repo_links,
            )
        if team_links:
            self._write_rows(
                "UNWIND $rows AS row MATCH (s:AISession {id: row.session_id}) "
                "MATCH (t:Team {id: row.team_id}) "
                "MERGE (s)-[:SHARED_WITH_TEAM]->(t)",
                team_links,
            )
        if symbol_links:
            self._write_rows(
                "UNWIND $rows AS row MATCH (s:AISession {id: row.session_id}) "
                "MATCH (c:CodeSymbol) "
                "WHERE c.id = row.symbol OR c.name = row.symbol OR c.qualified_name = row.symbol "
                "MERGE (s)-[:TOUCHES_SYMBOL]->(c)",
                symbol_links,
            )
        if file_links:
            self._write_rows(
                "UNWIND $rows AS row MATCH (s:AISession {id: row.session_id}) "
                "MATCH (doc:Document) "
                "WHERE doc.id = row.file OR doc.path = row.file OR doc.name = row.file "
                "MERGE (s)-[:TOUCHES_FILE]->(doc)",
                file_links,
            )

        return len(rows)

    def upsert_engineering_events(self, events: Iterable[EngineeringEvent]) -> int:
        rows = [
            {
                "id": e.id,
                "event_type": e.event_type,
                "title": e.title,
                "description": e.description,
                "timestamp": e.timestamp,
                "repository_id": e.repository_id,
                "team_id": e.team_id,
                "user_id": e.user_id,
                "external_ref": e.external_ref,
                "metadata_json": json.dumps(e.metadata, sort_keys=True) if e.metadata else None,
            }
            for e in events
        ]
        self._write_rows(
            "UNWIND $rows AS row MERGE (e:EngineeringEvent {id: row.id}) "
            "SET e.event_type = row.event_type, e.title = row.title, "
            "e.description = row.description, e.timestamp = row.timestamp, "
            "e.repository_id = row.repository_id, e.team_id = row.team_id, "
            "e.user_id = row.user_id, e.external_ref = row.external_ref, "
            "e.metadata_json = row.metadata_json",
            rows,
        )
        return len(rows)

    def upsert_engineering_decisions(self, decisions: Iterable[EngineeringDecision]) -> int:
        rows = [
            {
                "id": d.id,
                "title": d.title,
                "rationale": d.rationale,
                "status": d.status,
                "category": d.category,
                "scope": d.scope,
                "author_id": d.author_id,
                "team_id": d.team_id,
                "repository_id": d.repository_id,
                "project_id": d.project_id,
                "alternatives_json": json.dumps(d.alternatives_considered),
                "trade_offs_json": json.dumps(d.trade_offs),
                "superseded_by": d.superseded_by,
                "created_at": d.created_at,
                "updated_at": d.updated_at,
                "metadata_json": json.dumps(d.metadata, sort_keys=True) if d.metadata else None,
            }
            for d in decisions
        ]
        self._write_rows(
            "UNWIND $rows AS row MERGE (d:EngineeringDecision {id: row.id}) "
            "SET d.title = row.title, d.rationale = row.rationale, "
            "d.status = row.status, d.category = row.category, d.scope = row.scope, "
            "d.author_id = row.author_id, d.team_id = row.team_id, "
            "d.repository_id = row.repository_id, d.project_id = row.project_id, "
            "d.alternatives_json = row.alternatives_json, d.trade_offs_json = row.trade_offs_json, "
            "d.superseded_by = row.superseded_by, d.created_at = row.created_at, "
            "d.updated_at = row.updated_at, d.metadata_json = row.metadata_json",
            rows,
        )
        author_rows = [r for r in rows if r.get("author_id")]
        if author_rows:
            self._write_rows(
                "UNWIND $rows AS row MATCH (d:EngineeringDecision {id: row.id}) "
                "MATCH (u:User {id: row.author_id}) "
                "MERGE (d)-[:AUTHORED_BY]->(u)",
                author_rows,
            )
        repo_rows = [r for r in rows if r.get("repository_id")]
        if repo_rows:
            self._write_rows(
                "UNWIND $rows AS row MATCH (d:EngineeringDecision {id: row.id}) "
                "MATCH (r:Repository {id: row.repository_id}) "
                "MERGE (d)-[:AFFECTS_REPO]->(r)",
                repo_rows,
            )
        team_rows = [r for r in rows if r.get("team_id")]
        if team_rows:
            self._write_rows(
                "UNWIND $rows AS row MATCH (d:EngineeringDecision {id: row.id}) "
                "MATCH (t:Team {id: row.team_id}) "
                "MERGE (d)-[:APPLIES_TO_TEAM]->(t)",
                team_rows,
            )
        return len(rows)

    def upsert_memories(self, memories: Iterable[Memory]) -> int:
        rows: list[dict[str, Any]] = []
        author_links: list[dict[str, Any]] = []
        team_links: list[dict[str, Any]] = []
        repo_links: list[dict[str, Any]] = []
        project_links: list[dict[str, Any]] = []
        session_links: list[dict[str, Any]] = []
        event_links: list[dict[str, Any]] = []
        decision_links: list[dict[str, Any]] = []
        supersede_links: list[dict[str, Any]] = []
        symbol_links: list[dict[str, Any]] = []
        doc_links: list[dict[str, Any]] = []

        for m in memories:
            rows.append(
                {
                    "id": m.id,
                    "title": m.title,
                    "content": m.content,
                    "category": m.category,
                    "scope": m.scope,
                    "author_id": m.author_id,
                    "team_id": m.team_id,
                    "repository_id": m.repository_id,
                    "project_id": m.project_id,
                    "session_id": m.session_id,
                    "event_id": m.event_id,
                    "decision_id": m.decision_id,
                    "impact_areas_json": json.dumps(m.impact_areas),
                    "actionable_takeaways_json": json.dumps(m.actionable_takeaways),
                    "confidence": m.confidence,
                    "status": m.status,
                    "superseded_by": m.superseded_by,
                    "created_at": m.created_at,
                    "updated_at": m.updated_at,
                    "metadata_json": json.dumps(m.metadata, sort_keys=True) if m.metadata else None,
                    "provenance_json": json.dumps(m.provenance.to_dict(), sort_keys=True) if m.provenance else None,
                }
            )
            if m.author_id:
                author_links.append({"author_id": m.author_id, "memory_id": m.id})
            if m.team_id:
                team_links.append({"team_id": m.team_id, "memory_id": m.id})
            if m.repository_id:
                repo_links.append({"repository_id": m.repository_id, "memory_id": m.id})
            if m.project_id:
                project_links.append({"project_id": m.project_id, "memory_id": m.id})
            if m.session_id:
                session_links.append({"session_id": m.session_id, "memory_id": m.id})
            if m.event_id:
                event_links.append({"event_id": m.event_id, "memory_id": m.id})
            if m.decision_id:
                decision_links.append({"decision_id": m.decision_id, "memory_id": m.id})
            if m.superseded_by:
                supersede_links.append({"old_id": m.id, "new_id": m.superseded_by})
            for sym in (m.provenance.symbol_names if m.provenance else []):
                symbol_links.append({"memory_id": m.id, "symbol_ref": sym})
            for doc in (m.provenance.document_paths if m.provenance else []):
                doc_links.append({"memory_id": m.id, "doc_ref": doc})

        self._write_rows(
            "UNWIND $rows AS row MERGE (m:Memory {id: row.id}) "
            "SET m.title = row.title, m.content = row.content, "
            "m.category = row.category, m.scope = row.scope, "
            "m.author_id = row.author_id, m.team_id = row.team_id, "
            "m.repository_id = row.repository_id, m.project_id = row.project_id, "
            "m.session_id = row.session_id, m.event_id = row.event_id, "
            "m.decision_id = row.decision_id, "
            "m.impact_areas_json = row.impact_areas_json, "
            "m.actionable_takeaways_json = row.actionable_takeaways_json, "
            "m.confidence = row.confidence, m.status = row.status, "
            "m.superseded_by = row.superseded_by, m.created_at = row.created_at, "
            "m.updated_at = row.updated_at, m.metadata_json = row.metadata_json, "
            "m.provenance_json = row.provenance_json",
            rows,
        )

        if author_links:
            self._write_rows(
                "UNWIND $rows AS row MATCH (u:User {id: row.author_id}) "
                "MATCH (m:Memory {id: row.memory_id}) "
                "MERGE (u)-[:AUTHORED]->(m)",
                author_links,
            )
        if team_links:
            self._write_rows(
                "UNWIND $rows AS row MATCH (m:Memory {id: row.memory_id}) "
                "MATCH (t:Team {id: row.team_id}) "
                "MERGE (m)-[:SHARED_WITH_TEAM]->(t)",
                team_links,
            )
        if repo_links:
            self._write_rows(
                "UNWIND $rows AS row MATCH (m:Memory {id: row.memory_id}) "
                "MATCH (r:Repository {id: row.repository_id}) "
                "MERGE (m)-[:SCOPED_TO_REPO]->(r)",
                repo_links,
            )
        if project_links:
            self._write_rows(
                "UNWIND $rows AS row MATCH (m:Memory {id: row.memory_id}) "
                "MATCH (p:Project {id: row.project_id}) "
                "MERGE (m)-[:PART_OF_PROJECT]->(p)",
                project_links,
            )
        if session_links:
            self._write_rows(
                "UNWIND $rows AS row MATCH (m:Memory {id: row.memory_id}) "
                "MATCH (s:AISession {id: row.session_id}) "
                "MERGE (m)-[:EXTRACTED_FROM_SESSION]->(s)",
                session_links,
            )
        if event_links:
            self._write_rows(
                "UNWIND $rows AS row MATCH (m:Memory {id: row.memory_id}) "
                "MATCH (e:EngineeringEvent {id: row.event_id}) "
                "MERGE (m)-[:ORIGINATED_FROM_EVENT]->(e)",
                event_links,
            )
        if decision_links:
            self._write_rows(
                "UNWIND $rows AS row MATCH (m:Memory {id: row.memory_id}) "
                "MATCH (d:EngineeringDecision {id: row.decision_id}) "
                "MERGE (m)-[:JUSTIFIED_BY]->(d)",
                decision_links,
            )
        if supersede_links:
            self._write_rows(
                "UNWIND $rows AS row MATCH (m_old:Memory {id: row.old_id}) "
                "MATCH (m_new:Memory {id: row.new_id}) "
                "MERGE (m_old)-[:SUPERSEDES]->(m_new)",
                supersede_links,
            )
        if symbol_links:
            self._write_rows(
                "UNWIND $rows AS row MATCH (m:Memory {id: row.memory_id}) "
                "MATCH (c:CodeSymbol) "
                "WHERE c.id = row.symbol_ref OR c.name = row.symbol_ref OR c.qualified_name = row.symbol_ref "
                "MERGE (m)-[:REFERENCES_SYMBOL]->(c)",
                symbol_links,
            )
        if doc_links:
            self._write_rows(
                "UNWIND $rows AS row MATCH (m:Memory {id: row.memory_id}) "
                "MATCH (doc:Document) "
                "WHERE doc.id = row.doc_ref OR doc.path = row.doc_ref OR doc.name = row.doc_ref "
                "MERGE (m)-[:REFERENCES_DOC]->(doc)",
                doc_links,
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