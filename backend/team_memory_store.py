"""Relational and document storage layer for the Team Memory subsystem.

Provides structured persistence, versioned revisions, audit trails, and multi-tenant
scoping (personal, team, repository) with SQLite default engine and PostgreSQL compatibility.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from typing import Any

from .models import (
    AISession,
    AISessionRecord,
    EngineeringDecision,
    EngineeringEvent,
    Memory,
    MemoryProvenance,
    Project,
    Repository,
    Team,
    TeamMembership,
    User,
    utc_now_iso,
)

POSTGRES_SCHEMA_DDL = """-- PostgreSQL schema for Team Memory subsystem

CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(255) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    role VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS teams (
    id VARCHAR(255) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS team_memberships (
    user_id VARCHAR(255) REFERENCES users(id) ON DELETE CASCADE,
    team_id VARCHAR(255) REFERENCES teams(id) ON DELETE CASCADE,
    role VARCHAR(100) DEFAULT 'member',
    joined_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, team_id)
);

CREATE TABLE IF NOT EXISTS projects (
    id VARCHAR(255) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    team_id VARCHAR(255) REFERENCES teams(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS repositories (
    id VARCHAR(255) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    user_id VARCHAR(255),
    repository_id VARCHAR(255),
    url TEXT,
    default_branch VARCHAR(100) DEFAULT 'main',
    language VARCHAR(100),
    description TEXT,
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS ai_sessions (
    id VARCHAR(255) PRIMARY KEY,
    title VARCHAR(255),
    user_id VARCHAR(255) REFERENCES users(id) ON DELETE SET NULL,
    repository_id VARCHAR(255),
    team_id VARCHAR(255) REFERENCES teams(id) ON DELETE SET NULL,
    prompt TEXT,
    response_summary TEXT,
    affected_files JSONB DEFAULT '[]'::jsonb,
    affected_symbols JSONB DEFAULT '[]'::jsonb,
    engineering_decisions JSONB DEFAULT '[]'::jsonb,
    visibility VARCHAR(50) DEFAULT 'repository',
    raw_transcript JSONB,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    summary TEXT,
    query_count INT DEFAULT 0,
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS engineering_events (
    id VARCHAR(255) PRIMARY KEY,
    event_type VARCHAR(100) NOT NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    repository_id VARCHAR(255),
    team_id VARCHAR(255),
    user_id VARCHAR(255),
    external_ref VARCHAR(255),
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS engineering_decisions (
    id VARCHAR(255) PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    rationale TEXT NOT NULL,
    status VARCHAR(50) DEFAULT 'accepted',
    category VARCHAR(100) DEFAULT 'architecture',
    scope VARCHAR(50) DEFAULT 'team',
    author_id VARCHAR(255),
    team_id VARCHAR(255),
    repository_id VARCHAR(255),
    project_id VARCHAR(255),
    alternatives_considered JSONB DEFAULT '[]'::jsonb,
    trade_offs JSONB DEFAULT '[]'::jsonb,
    superseded_by VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ,
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS memories (
    id VARCHAR(255) PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    content TEXT NOT NULL,
    category VARCHAR(100) NOT NULL,
    scope VARCHAR(50) NOT NULL,
    author_id VARCHAR(255) NOT NULL,
    team_id VARCHAR(255),
    repository_id VARCHAR(255),
    project_id VARCHAR(255),
    session_id VARCHAR(255),
    event_id VARCHAR(255),
    decision_id VARCHAR(255),
    impact_areas JSONB DEFAULT '[]'::jsonb,
    actionable_takeaways JSONB DEFAULT '[]'::jsonb,
    provenance JSONB NOT NULL,
    confidence REAL DEFAULT 1.0,
    status VARCHAR(50) DEFAULT 'active',
    superseded_by VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS memory_revisions (
    id VARCHAR(255) PRIMARY KEY,
    memory_id VARCHAR(255) REFERENCES memories(id) ON DELETE CASCADE,
    revision_number INT NOT NULL,
    title VARCHAR(255) NOT NULL,
    content TEXT NOT NULL,
    category VARCHAR(100) NOT NULL,
    scope VARCHAR(50) NOT NULL,
    changed_by VARCHAR(255),
    change_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    snapshot JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope);
CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
CREATE INDEX IF NOT EXISTS idx_memories_author ON memories(author_id);
CREATE INDEX IF NOT EXISTS idx_memories_team ON memories(team_id);
CREATE INDEX IF NOT EXISTS idx_memories_repo ON memories(repository_id);
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
"""

SQLITE_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT,
    role TEXT,
    created_at TEXT NOT NULL,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS teams (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS team_memberships (
    user_id TEXT NOT NULL,
    team_id TEXT NOT NULL,
    role TEXT DEFAULT 'member',
    joined_at TEXT NOT NULL,
    PRIMARY KEY (user_id, team_id)
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    team_id TEXT,
    created_at TEXT NOT NULL,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS repositories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    user_id TEXT,
    repository_id TEXT,
    url TEXT,
    default_branch TEXT DEFAULT 'main',
    language TEXT,
    description TEXT,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS ai_sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    user_id TEXT,
    repository_id TEXT,
    team_id TEXT,
    prompt TEXT,
    response_summary TEXT,
    affected_files TEXT,
    affected_symbols TEXT,
    engineering_decisions TEXT,
    visibility TEXT DEFAULT 'repository',
    raw_transcript TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    summary TEXT,
    query_count INTEGER DEFAULT 0,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS engineering_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    repository_id TEXT,
    team_id TEXT,
    user_id TEXT,
    external_ref TEXT,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS engineering_decisions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    rationale TEXT NOT NULL,
    status TEXT DEFAULT 'accepted',
    category TEXT DEFAULT 'architecture',
    scope TEXT DEFAULT 'team',
    author_id TEXT,
    team_id TEXT,
    repository_id TEXT,
    project_id TEXT,
    alternatives_considered TEXT,
    trade_offs TEXT,
    superseded_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL,
    scope TEXT NOT NULL,
    author_id TEXT NOT NULL,
    team_id TEXT,
    repository_id TEXT,
    project_id TEXT,
    session_id TEXT,
    event_id TEXT,
    decision_id TEXT,
    impact_areas TEXT,
    actionable_takeaways TEXT,
    provenance TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    status TEXT DEFAULT 'active',
    superseded_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS memory_revisions (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL,
    scope TEXT NOT NULL,
    changed_by TEXT,
    change_reason TEXT,
    created_at TEXT NOT NULL,
    snapshot TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope);
CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
CREATE INDEX IF NOT EXISTS idx_memories_author ON memories(author_id);
CREATE INDEX IF NOT EXISTS idx_memories_team ON memories(team_id);
CREATE INDEX IF NOT EXISTS idx_memories_repo ON memories(repository_id);
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
"""


class TeamMemoryDocumentStore:
    """Document and relational persistence store for Team Memory."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(SQLITE_SCHEMA_DDL)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> TeamMemoryDocumentStore:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    # --- Users ---

    def save_user(self, user: User) -> User:
        meta_json = json.dumps(user.metadata, sort_keys=True) if user.metadata else "{}"
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO users (id, name, email, role, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    email=excluded.email,
                    role=excluded.role,
                    metadata=excluded.metadata
                """,
                (user.id, user.name, user.email, user.role, user.created_at, meta_json),
            )
        return user

    def get_user(self, user_id: str) -> User | None:
        cursor = self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return User(
            id=row["id"],
            name=row["name"],
            email=row["email"],
            role=row["role"],
            created_at=row["created_at"],
            metadata=json.loads(row["metadata"] or "{}"),
        )

    def list_users(self) -> list[User]:
        cursor = self._conn.execute("SELECT * FROM users ORDER BY created_at ASC")
        return [
            User(
                id=r["id"],
                name=r["name"],
                email=r["email"],
                role=r["role"],
                created_at=r["created_at"],
                metadata=json.loads(r["metadata"] or "{}"),
            )
            for r in cursor.fetchall()
        ]

    # --- Teams & Memberships ---

    def save_team(self, team: Team) -> Team:
        meta_json = json.dumps(team.metadata, sort_keys=True) if team.metadata else "{}"
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO teams (id, name, description, created_at, metadata)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    description=excluded.description,
                    metadata=excluded.metadata
                """,
                (team.id, team.name, team.description, team.created_at, meta_json),
            )
        return team

    def get_team(self, team_id: str) -> Team | None:
        cursor = self._conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return Team(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            created_at=row["created_at"],
            metadata=json.loads(row["metadata"] or "{}"),
        )

    def add_team_membership(self, membership: TeamMembership) -> TeamMembership:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO team_memberships (user_id, team_id, role, joined_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, team_id) DO UPDATE SET
                    role=excluded.role
                """,
                (membership.user_id, membership.team_id, membership.role, membership.joined_at),
            )
        return membership

    def get_user_teams(self, user_id: str) -> list[str]:
        cursor = self._conn.execute("SELECT team_id FROM team_memberships WHERE user_id = ?", (user_id,))
        return [r["team_id"] for r in cursor.fetchall()]

    def get_team_members(self, team_id: str) -> list[TeamMembership]:
        cursor = self._conn.execute("SELECT * FROM team_memberships WHERE team_id = ?", (team_id,))
        return [
            TeamMembership(
                user_id=r["user_id"],
                team_id=r["team_id"],
                role=r["role"],
                joined_at=r["joined_at"],
            )
            for r in cursor.fetchall()
        ]

    # --- Projects ---

    def save_project(self, project: Project) -> Project:
        meta_json = json.dumps(project.metadata, sort_keys=True) if project.metadata else "{}"
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO projects (id, name, description, team_id, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    description=excluded.description,
                    team_id=excluded.team_id,
                    metadata=excluded.metadata
                """,
                (project.id, project.name, project.description, project.team_id, project.created_at, meta_json),
            )
        return project

    def get_project(self, project_id: str) -> Project | None:
        cursor = self._conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return Project(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            team_id=row["team_id"],
            created_at=row["created_at"],
            metadata=json.loads(row["metadata"] or "{}"),
        )

    # --- Repositories ---

    def save_repository(self, repository: Repository) -> Repository:
        meta_json = json.dumps(repository.metadata, sort_keys=True) if repository.metadata else "{}"
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO repositories (id, name, user_id, repository_id, url, default_branch, language, description, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    user_id=excluded.user_id,
                    repository_id=excluded.repository_id,
                    url=excluded.url,
                    default_branch=excluded.default_branch,
                    language=excluded.language,
                    description=excluded.description,
                    metadata=excluded.metadata
                """,
                (
                    repository.id,
                    repository.name,
                    repository.user_id,
                    repository.repository_id or repository.id,
                    repository.url,
                    repository.default_branch,
                    repository.language,
                    repository.description,
                    meta_json,
                ),
            )
        return repository

    def get_repository(self, repository_id: str) -> Repository | None:
        cursor = self._conn.execute("SELECT * FROM repositories WHERE id = ?", (repository_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return Repository(
            id=row["id"],
            name=row["name"],
            user_id=row["user_id"],
            repository_id=row["repository_id"],
            url=row["url"],
            default_branch=row["default_branch"],
            language=row["language"],
            description=row["description"],
            metadata=json.loads(row["metadata"] or "{}"),
        )

    # --- AI Sessions ---

    def save_session(self, session: AISession) -> AISession:
        meta_json = json.dumps(session.metadata, sort_keys=True) if session.metadata else "{}"
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO ai_sessions (id, title, user_id, repository_id, team_id, started_at, ended_at, summary, query_count, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    ended_at=excluded.ended_at,
                    summary=excluded.summary,
                    query_count=excluded.query_count,
                    metadata=excluded.metadata
                """,
                (
                    session.id,
                    session.title,
                    session.user_id,
                    session.repository_id,
                    session.team_id,
                    session.started_at,
                    session.ended_at,
                    session.summary,
                    session.query_count,
                    meta_json,
                ),
            )
        return session

    def get_session(self, session_id: str) -> AISession | None:
        cursor = self._conn.execute("SELECT * FROM ai_sessions WHERE id = ?", (session_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return AISession(
            id=row["id"],
            title=row["title"],
            user_id=row["user_id"],
            repository_id=row["repository_id"],
            team_id=row["team_id"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            summary=row["summary"],
            query_count=row["query_count"] or 0,
            metadata=json.loads(row["metadata"] or "{}"),
        )

    def save_session_record(self, record: AISessionRecord) -> AISessionRecord:
        meta_json = json.dumps(record.metadata, sort_keys=True) if record.metadata else "{}"
        files_json = json.dumps(record.affected_files)
        symbols_json = json.dumps(record.affected_symbols)
        decisions_json = json.dumps(record.engineering_decisions)
        raw_json = json.dumps(record.raw_transcript) if record.raw_transcript else None
        title = record.prompt[:80] + ("..." if len(record.prompt) > 80 else "")

        with self._conn:
            self._conn.execute(
                """
                INSERT INTO ai_sessions (
                    id, title, user_id, repository_id, team_id,
                    prompt, response_summary, affected_files, affected_symbols,
                    engineering_decisions, visibility, raw_transcript,
                    started_at, ended_at, summary, query_count, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    user_id=excluded.user_id,
                    repository_id=excluded.repository_id,
                    team_id=excluded.team_id,
                    prompt=excluded.prompt,
                    response_summary=excluded.response_summary,
                    affected_files=excluded.affected_files,
                    affected_symbols=excluded.affected_symbols,
                    engineering_decisions=excluded.engineering_decisions,
                    visibility=excluded.visibility,
                    raw_transcript=excluded.raw_transcript,
                    summary=excluded.summary,
                    metadata=excluded.metadata
                """,
                (
                    record.session_id,
                    title,
                    record.user_id,
                    record.repository_id,
                    record.team_id,
                    record.prompt,
                    record.response_summary,
                    files_json,
                    symbols_json,
                    decisions_json,
                    record.visibility,
                    raw_json,
                    record.timestamp,
                    record.timestamp,
                    record.response_summary,
                    1,
                    meta_json,
                ),
            )
        return record

    def get_session_record(self, session_id: str) -> AISessionRecord | None:
        cursor = self._conn.execute("SELECT * FROM ai_sessions WHERE id = ?", (session_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return AISessionRecord(
            session_id=row["id"],
            user_id=row["user_id"] or "",
            repository_id=row["repository_id"] or "",
            prompt=row["prompt"] or row["title"] or "",
            response_summary=row["response_summary"] or row["summary"] or "",
            affected_files=json.loads(row["affected_files"] or "[]"),
            affected_symbols=json.loads(row["affected_symbols"] or "[]"),
            engineering_decisions=json.loads(row["engineering_decisions"] or "[]"),
            timestamp=row["started_at"],
            visibility=row["visibility"] or "repository",
            team_id=row["team_id"],
            raw_transcript=json.loads(row["raw_transcript"]) if row["raw_transcript"] else None,
            metadata=json.loads(row["metadata"] or "{}"),
        )

    def list_session_records(
        self,
        *,
        user_id: str | None = None,
        repository_id: str | None = None,
        team_id: str | None = None,
        visibility: str | None = None,
        limit: int = 50,
    ) -> list[AISessionRecord]:
        clauses = []
        params = []
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        if repository_id:
            clauses.append("repository_id = ?")
            params.append(repository_id)
        if team_id:
            clauses.append("team_id = ?")
            params.append(team_id)
        if visibility:
            clauses.append("visibility = ?")
            params.append(visibility)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM ai_sessions {where} ORDER BY started_at DESC LIMIT ?"
        params.append(limit)

        cursor = self._conn.execute(sql, params)
        records = []
        for row in cursor.fetchall():
            records.append(
                AISessionRecord(
                    session_id=row["id"],
                    user_id=row["user_id"] or "",
                    repository_id=row["repository_id"] or "",
                    prompt=row["prompt"] or row["title"] or "",
                    response_summary=row["response_summary"] or row["summary"] or "",
                    affected_files=json.loads(row["affected_files"] or "[]"),
                    affected_symbols=json.loads(row["affected_symbols"] or "[]"),
                    engineering_decisions=json.loads(row["engineering_decisions"] or "[]"),
                    timestamp=row["started_at"],
                    visibility=row["visibility"] or "repository",
                    team_id=row["team_id"],
                    raw_transcript=json.loads(row["raw_transcript"]) if row["raw_transcript"] else None,
                    metadata=json.loads(row["metadata"] or "{}"),
                )
            )
        return records

    # --- Engineering Events ---

    def save_event(self, event: EngineeringEvent) -> EngineeringEvent:
        meta_json = json.dumps(event.metadata, sort_keys=True) if event.metadata else "{}"
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO engineering_events (id, event_type, title, description, timestamp, repository_id, team_id, user_id, external_ref, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    event_type=excluded.event_type,
                    title=excluded.title,
                    description=excluded.description,
                    timestamp=excluded.timestamp,
                    external_ref=excluded.external_ref,
                    metadata=excluded.metadata
                """,
                (
                    event.id,
                    event.event_type,
                    event.title,
                    event.description,
                    event.timestamp,
                    event.repository_id,
                    event.team_id,
                    event.user_id,
                    event.external_ref,
                    meta_json,
                ),
            )
        return event

    def get_event(self, event_id: str) -> EngineeringEvent | None:
        cursor = self._conn.execute("SELECT * FROM engineering_events WHERE id = ?", (event_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return EngineeringEvent(
            id=row["id"],
            event_type=row["event_type"],
            title=row["title"],
            description=row["description"],
            timestamp=row["timestamp"],
            repository_id=row["repository_id"],
            team_id=row["team_id"],
            user_id=row["user_id"],
            external_ref=row["external_ref"],
            metadata=json.loads(row["metadata"] or "{}"),
        )

    # --- Engineering Decisions ---

    def save_decision(self, decision: EngineeringDecision) -> EngineeringDecision:
        meta_json = json.dumps(decision.metadata, sort_keys=True) if decision.metadata else "{}"
        alts_json = json.dumps(decision.alternatives_considered)
        tradeoffs_json = json.dumps(decision.trade_offs)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO engineering_decisions (
                    id, title, rationale, status, category, scope, author_id,
                    team_id, repository_id, project_id, alternatives_considered, trade_offs,
                    superseded_by, created_at, updated_at, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    rationale=excluded.rationale,
                    status=excluded.status,
                    category=excluded.category,
                    scope=excluded.scope,
                    alternatives_considered=excluded.alternatives_considered,
                    trade_offs=excluded.trade_offs,
                    superseded_by=excluded.superseded_by,
                    updated_at=excluded.updated_at,
                    metadata=excluded.metadata
                """,
                (
                    decision.id,
                    decision.title,
                    decision.rationale,
                    decision.status,
                    decision.category,
                    decision.scope,
                    decision.author_id,
                    decision.team_id,
                    decision.repository_id,
                    decision.project_id,
                    alts_json,
                    tradeoffs_json,
                    decision.superseded_by,
                    decision.created_at,
                    decision.updated_at or utc_now_iso(),
                    meta_json,
                ),
            )
        return decision

    def get_decision(self, decision_id: str) -> EngineeringDecision | None:
        cursor = self._conn.execute("SELECT * FROM engineering_decisions WHERE id = ?", (decision_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return EngineeringDecision(
            id=row["id"],
            title=row["title"],
            rationale=row["rationale"],
            status=row["status"],
            category=row["category"],
            scope=row["scope"],
            author_id=row["author_id"],
            team_id=row["team_id"],
            repository_id=row["repository_id"],
            project_id=row["project_id"],
            alternatives_considered=json.loads(row["alternatives_considered"] or "[]"),
            trade_offs=json.loads(row["trade_offs"] or "[]"),
            superseded_by=row["superseded_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=json.loads(row["metadata"] or "{}"),
        )

    # --- Memories & Revisions ---

    def _row_to_memory(self, row: sqlite3.Row) -> Memory:
        prov_data = json.loads(row["provenance"] or "{}")
        return Memory(
            id=row["id"],
            title=row["title"],
            content=row["content"],
            category=row["category"],
            scope=row["scope"],
            author_id=row["author_id"],
            team_id=row["team_id"],
            repository_id=row["repository_id"],
            project_id=row["project_id"],
            session_id=row["session_id"],
            event_id=row["event_id"],
            decision_id=row["decision_id"],
            impact_areas=json.loads(row["impact_areas"] or "[]"),
            actionable_takeaways=json.loads(row["actionable_takeaways"] or "[]"),
            provenance=MemoryProvenance.from_dict(prov_data),
            confidence=float(row["confidence"] or 1.0),
            status=row["status"],
            superseded_by=row["superseded_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=json.loads(row["metadata"] or "{}"),
        )

    def save_memory(
        self,
        memory: Memory,
        *,
        changed_by: str = "",
        change_reason: str = "initial_creation",
    ) -> Memory:
        """Save a Memory and record an immutable revision snapshot."""
        now = utc_now_iso()
        memory.updated_at = now
        impact_json = json.dumps(memory.impact_areas)
        takeaways_json = json.dumps(memory.actionable_takeaways)
        prov_json = json.dumps(memory.provenance.to_dict())
        meta_json = json.dumps(memory.metadata, sort_keys=True) if memory.metadata else "{}"

        # Determine revision number
        cursor = self._conn.execute(
            "SELECT COALESCE(MAX(revision_number), 0) AS rev FROM memory_revisions WHERE memory_id = ?",
            (memory.id,),
        )
        row = cursor.fetchone()
        next_rev = (row["rev"] if row else 0) + 1

        with self._conn:
            self._conn.execute(
                """
                INSERT INTO memories (
                    id, title, content, category, scope, author_id,
                    team_id, repository_id, project_id, session_id, event_id, decision_id,
                    impact_areas, actionable_takeaways, provenance, confidence,
                    status, superseded_by, created_at, updated_at, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    content=excluded.content,
                    category=excluded.category,
                    scope=excluded.scope,
                    team_id=excluded.team_id,
                    repository_id=excluded.repository_id,
                    project_id=excluded.project_id,
                    session_id=excluded.session_id,
                    event_id=excluded.event_id,
                    decision_id=excluded.decision_id,
                    impact_areas=excluded.impact_areas,
                    actionable_takeaways=excluded.actionable_takeaways,
                    provenance=excluded.provenance,
                    confidence=excluded.confidence,
                    status=excluded.status,
                    superseded_by=excluded.superseded_by,
                    updated_at=excluded.updated_at,
                    metadata=excluded.metadata
                """,
                (
                    memory.id,
                    memory.title,
                    memory.content,
                    memory.category,
                    memory.scope,
                    memory.author_id,
                    memory.team_id,
                    memory.repository_id,
                    memory.project_id,
                    memory.session_id,
                    memory.event_id,
                    memory.decision_id,
                    impact_json,
                    takeaways_json,
                    prov_json,
                    memory.confidence,
                    memory.status,
                    memory.superseded_by,
                    memory.created_at,
                    memory.updated_at,
                    meta_json,
                ),
            )

            # Record audit revision snapshot
            revision_id = f"rev_{memory.id}_{next_rev}_{uuid.uuid4().hex[:8]}"
            self._conn.execute(
                """
                INSERT INTO memory_revisions (
                    id, memory_id, revision_number, title, content,
                    category, scope, changed_by, change_reason, created_at, snapshot
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    memory.id,
                    next_rev,
                    memory.title,
                    memory.content,
                    memory.category,
                    memory.scope,
                    changed_by or memory.author_id,
                    change_reason,
                    now,
                    json.dumps(memory.to_dict()),
                ),
            )

        return memory

    def get_memory(self, memory_id: str) -> Memory | None:
        cursor = self._conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_memory(row)

    def get_memory_revisions(self, memory_id: str) -> list[dict[str, Any]]:
        cursor = self._conn.execute(
            "SELECT * FROM memory_revisions WHERE memory_id = ? ORDER BY revision_number ASC",
            (memory_id,),
        )
        revisions = []
        for r in cursor.fetchall():
            revisions.append(
                {
                    "id": r["id"],
                    "memory_id": r["memory_id"],
                    "revision_number": r["revision_number"],
                    "title": r["title"],
                    "content": r["content"],
                    "category": r["category"],
                    "scope": r["scope"],
                    "changed_by": r["changed_by"],
                    "change_reason": r["change_reason"],
                    "created_at": r["created_at"],
                    "snapshot": json.loads(r["snapshot"] or "{}"),
                }
            )
        return revisions

    def supersede_memory(
        self,
        old_memory_id: str,
        new_memory_id: str,
        *,
        changed_by: str = "",
        reason: str = "Superseded by newer decision",
    ) -> bool:
        """Mark old_memory as superseded by new_memory."""
        old = self.get_memory(old_memory_id)
        if not old:
            return False
        old.status = "superseded"
        old.superseded_by = new_memory_id
        self.save_memory(
            old,
            changed_by=changed_by,
            change_reason=f"Superseded by {new_memory_id}: {reason}",
        )
        return True

    def query_memories(
        self,
        *,
        user_id: str | None = None,
        team_id: str | None = None,
        user_team_ids: Iterable[str] | None = None,
        repository_id: str | None = None,
        scopes: list[str] | None = None,
        categories: list[str] | None = None,
        query: str | None = None,
        include_superseded: bool = False,
        limit: int = 50,
    ) -> list[Memory]:
        """Query memories enforcing scoping isolation and filtering.

        Scoping Isolation Rules:
        - 'personal' memories are ONLY visible if author_id == user_id.
        - 'team' memories are visible if team_id matches team_id or is in user_team_ids.
        - 'repository' memories are visible if repository_id matches.
        """
        clauses: list[str] = []
        params: list[Any] = []

        if not include_superseded:
            clauses.append("status != 'superseded'")

        # Scope restrictions
        if scopes:
            placeholders = ",".join("?" for _ in scopes)
            clauses.append(f"scope IN ({placeholders})")
            params.extend(scopes)

        # Multi-tenant scoping logic:
        # A memory is accessible to user_id if:
        # 1. Personal: strictly authored by user_id
        # 2. Team: authored by user_id, in user's team, or general team knowledge (team_id IS NULL)
        # 3. Repository: authored by user_id or attached to the target repository
        scope_conditions: list[str] = []
        if user_id:
            scope_conditions.append("(scope = 'personal' AND author_id = ?)")
            params.append(user_id)

            all_teams = set()
            if team_id:
                all_teams.add(team_id)
            if user_team_ids:
                all_teams.update(user_team_ids)

            if all_teams:
                t_placeholders = ",".join("?" for _ in all_teams)
                scope_conditions.append(
                    f"(scope = 'team' AND (author_id = ? OR team_id IN ({t_placeholders}) OR team_id IS NULL))"
                )
                params.append(user_id)
                params.extend(list(all_teams))
            else:
                scope_conditions.append("(scope = 'team' AND (author_id = ? OR team_id IS NULL))")
                params.append(user_id)

            if repository_id:
                scope_conditions.append("(scope = 'repository' AND (author_id = ? OR repository_id = ?))")
                params.extend([user_id, repository_id])
            else:
                scope_conditions.append("(scope = 'repository' AND author_id = ?)")
                params.append(user_id)
        else:
            if team_id:
                scope_conditions.append("(scope = 'team' AND team_id = ?)")
                params.append(team_id)
            if repository_id:
                scope_conditions.append("(scope = 'repository' AND repository_id = ?)")
                params.append(repository_id)

        if scope_conditions:
            clauses.append(f"({' OR '.join(scope_conditions)})")

        # Category filter
        if categories:
            c_placeholders = ",".join("?" for _ in categories)
            clauses.append(f"category IN ({c_placeholders})")
            params.extend(categories)

        # Text query filter (simple keyword containment across title, content, impact_areas)
        if query and query.strip():
            terms = [t.lower() for t in query.strip().split() if len(t) > 2]
            if terms:
                term_clauses = []
                for term in terms:
                    term_clauses.append(
                        "(LOWER(title) LIKE ? OR LOWER(content) LIKE ? OR LOWER(impact_areas) LIKE ? OR LOWER(actionable_takeaways) LIKE ?)"
                    )
                    pattern = f"%{term}%"
                    params.extend([pattern, pattern, pattern, pattern])
                clauses.append(f"({' OR '.join(term_clauses)})")

        where_stmt = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM memories {where_stmt} ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        cursor = self._conn.execute(sql, params)
        return [self._row_to_memory(r) for r in cursor.fetchall()]

    def count_memories(self) -> int:
        cursor = self._conn.execute("SELECT COUNT(*) AS c FROM memories")
        row = cursor.fetchone()
        return row["c"] if row else 0
