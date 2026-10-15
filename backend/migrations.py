"""Database Schema Migrations and Version Management for Synapse (Phase 17).

Provides reproducible, verifiable schema migrations for the SQLite / PostgreSQL
engineering memory document store.
Tracks applied migration history in a dedicated `schema_migrations` table,
supports atomic transactions per migration step, and includes schema integrity
verification.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import logging
from pathlib import Path
import sqlite3
from typing import Any, Callable

logger = logging.getLogger(__name__)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class MigrationStep:
    version: int
    name: str
    up_sql: str
    down_sql: str = ""

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.up_sql.strip().encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Migration Registry
# ---------------------------------------------------------------------------

MIGRATIONS: list[MigrationStep] = [
    MigrationStep(
        version=1,
        name="initial_core_team_memory_schema",
        up_sql="""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            role TEXT DEFAULT 'developer',
            created_at TEXT NOT NULL,
            metadata TEXT DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS teams (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            metadata TEXT DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS team_memberships (
            user_id TEXT NOT NULL,
            team_id TEXT NOT NULL,
            role TEXT DEFAULT 'member',
            joined_at TEXT NOT NULL,
            PRIMARY KEY (user_id, team_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            team_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            metadata TEXT DEFAULT '{}',
            FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS repositories (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            project_id TEXT NOT NULL,
            remote_url TEXT,
            default_branch TEXT DEFAULT 'main',
            created_at TEXT NOT NULL,
            metadata TEXT DEFAULT '{}',
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS ai_sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            user_id TEXT,
            team_id TEXT,
            repository_id TEXT,
            branch TEXT,
            task_description TEXT,
            summary TEXT,
            prompt TEXT,
            response_summary TEXT,
            affected_files TEXT DEFAULT '[]',
            affected_symbols TEXT DEFAULT '[]',
            engineering_decisions TEXT DEFAULT '[]',
            visibility TEXT DEFAULT 'repository',
            raw_transcript TEXT,
            metadata TEXT DEFAULT '{}',
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY (repository_id) REFERENCES repositories(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS engineering_events (
            id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            user_id TEXT,
            team_id TEXT,
            repository_id TEXT,
            session_id TEXT,
            payload TEXT DEFAULT '{}',
            metadata TEXT DEFAULT '{}',
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY (repository_id) REFERENCES repositories(id) ON DELETE CASCADE,
            FOREIGN KEY (session_id) REFERENCES ai_sessions(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS engineering_decisions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            rationale TEXT NOT NULL,
            status TEXT DEFAULT 'accepted',
            timestamp TEXT NOT NULL,
            author_id TEXT,
            team_id TEXT,
            repository_id TEXT,
            superseded_by TEXT,
            impacted_files TEXT DEFAULT '[]',
            impacted_symbols TEXT DEFAULT '[]',
            metadata TEXT DEFAULT '{}',
            FOREIGN KEY (author_id) REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY (repository_id) REFERENCES repositories(id) ON DELETE CASCADE,
            FOREIGN KEY (superseded_by) REFERENCES engineering_decisions(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            category TEXT NOT NULL,
            scope TEXT DEFAULT 'repository',
            author_id TEXT,
            team_id TEXT,
            repository_id TEXT,
            confidence REAL DEFAULT 1.0,
            access_count INTEGER DEFAULT 0,
            last_accessed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            superseded_by TEXT,
            impact_areas TEXT DEFAULT '[]',
            actionable_takeaways TEXT DEFAULT '[]',
            metadata TEXT DEFAULT '{}',
            FOREIGN KEY (author_id) REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY (repository_id) REFERENCES repositories(id) ON DELETE CASCADE,
            FOREIGN KEY (superseded_by) REFERENCES memories(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS memory_revisions (
            id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL,
            revision_number INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            category TEXT NOT NULL,
            scope TEXT NOT NULL,
            author_id TEXT,
            team_id TEXT,
            repository_id TEXT,
            confidence REAL NOT NULL,
            created_at TEXT NOT NULL,
            change_summary TEXT,
            impact_areas TEXT DEFAULT '[]',
            actionable_takeaways TEXT DEFAULT '[]',
            metadata TEXT DEFAULT '{}',
            FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
            FOREIGN KEY (author_id) REFERENCES users(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS git_commits (
            commit_sha TEXT NOT NULL,
            repository_id TEXT NOT NULL,
            author_name TEXT,
            author_email TEXT,
            committed_at TEXT NOT NULL,
            message TEXT NOT NULL,
            files_changed TEXT DEFAULT '[]',
            stats TEXT DEFAULT '{}',
            metadata TEXT DEFAULT '{}',
            PRIMARY KEY (commit_sha, repository_id),
            FOREIGN KEY (repository_id) REFERENCES repositories(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS git_commit_links (
            commit_sha TEXT NOT NULL,
            repository_id TEXT NOT NULL,
            linked_entity_type TEXT NOT NULL,
            linked_entity_id TEXT NOT NULL,
            link_type TEXT NOT NULL,
            confidence REAL DEFAULT 1.0,
            created_at TEXT NOT NULL,
            metadata TEXT DEFAULT '{}',
            PRIMARY KEY (commit_sha, repository_id, linked_entity_type, linked_entity_id, link_type),
            FOREIGN KEY (commit_sha, repository_id) REFERENCES git_commits(commit_sha, repository_id) ON DELETE CASCADE
        );
        """,
        down_sql="DROP TABLE IF EXISTS git_commit_links; DROP TABLE IF EXISTS git_commits; DROP TABLE IF EXISTS memory_revisions; DROP TABLE IF EXISTS memories; DROP TABLE IF EXISTS engineering_decisions; DROP TABLE IF EXISTS engineering_events; DROP TABLE IF EXISTS ai_sessions; DROP TABLE IF EXISTS repositories; DROP TABLE IF EXISTS projects; DROP TABLE IF EXISTS team_memberships; DROP TABLE IF EXISTS teams; DROP TABLE IF EXISTS users;",
    ),
    MigrationStep(
        version=2,
        name="incremental_checkpoints_and_indexing",
        up_sql="""
        CREATE TABLE IF NOT EXISTS incremental_checkpoints (
            id TEXT PRIMARY KEY,
            repository_id TEXT NOT NULL,
            commit_sha TEXT NOT NULL,
            parent_sha TEXT,
            created_at TEXT NOT NULL,
            symbol_count INTEGER NOT NULL,
            file_count INTEGER NOT NULL,
            symbols_by_file TEXT NOT NULL,
            graph_hash TEXT NOT NULL,
            metadata TEXT DEFAULT '{}',
            FOREIGN KEY (repository_id) REFERENCES repositories(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_checkpoints_repo ON incremental_checkpoints(repository_id, created_at DESC);
        """,
        down_sql="DROP TABLE IF EXISTS incremental_checkpoints;",
    ),
    MigrationStep(
        version=3,
        name="security_audit_and_api_keys",
        up_sql="""
        CREATE TABLE IF NOT EXISTS security_audit_events (
            id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            repository_id TEXT,
            team_id TEXT,
            action TEXT NOT NULL,
            resource TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT,
            ip_address TEXT,
            timestamp TEXT NOT NULL,
            metadata TEXT DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS api_keys (
            key_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            key_hash TEXT UNIQUE NOT NULL,
            prefix TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            roles TEXT NOT NULL,
            allowed_repositories TEXT NOT NULL,
            allowed_teams TEXT NOT NULL,
            expires_at TEXT,
            revoked INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
        CREATE INDEX IF NOT EXISTS idx_api_keys_tenant ON api_keys(tenant_id);
        CREATE INDEX IF NOT EXISTS idx_audit_tenant_time ON security_audit_events(tenant_id, timestamp DESC);
        """,
        down_sql="DROP TABLE IF EXISTS api_keys; DROP TABLE IF EXISTS security_audit_events;",
    ),
    MigrationStep(
        version=4,
        name="performance_composite_indexes",
        up_sql="""
        CREATE INDEX IF NOT EXISTS idx_ai_sessions_repo ON ai_sessions(repository_id, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memories_cat_scope ON memories(category, scope);
        CREATE INDEX IF NOT EXISTS idx_memories_repo ON memories(repository_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_decisions_repo ON engineering_decisions(repository_id, timestamp DESC);
        """,
        down_sql="DROP INDEX IF EXISTS idx_ai_sessions_repo; DROP INDEX IF EXISTS idx_memories_cat_scope; DROP INDEX IF EXISTS idx_memories_repo; DROP INDEX IF EXISTS idx_decisions_repo;",
    ),
]


class MigrationManager:
    """Manages database migration execution, version tracking, and schema validation."""

    def __init__(self, db_path: str = "data/synapse_team_memory.db") -> None:
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_migration_table(self, conn: sqlite3.Connection) -> None:
        """Create schema_migrations table if not present."""
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )

    def get_applied_versions(self, conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
        """Return dict of version -> record for all applied migrations."""
        self.ensure_migration_table(conn)
        cursor = conn.execute("SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version ASC")
        return {
            row["version"]: {
                "name": row["name"],
                "checksum": row["checksum"],
                "applied_at": row["applied_at"],
            }
            for row in cursor.fetchall()
        }

    def get_current_version(self) -> int:
        """Return the highest applied migration version, or 0 if none."""
        conn = self._get_connection()
        try:
            self.ensure_migration_table(conn)
            cursor = conn.execute("SELECT MAX(version) as max_v FROM schema_migrations")
            row = cursor.fetchone()
            return row["max_v"] or 0
        finally:
            conn.close()

    def get_pending_migrations(self) -> list[MigrationStep]:
        """Return list of migrations not yet applied."""
        conn = self._get_connection()
        try:
            applied = self.get_applied_versions(conn)
            return [m for m in MIGRATIONS if m.version not in applied]
        finally:
            conn.close()

    def apply_pending(self) -> list[int]:
        """Apply all pending migrations in atomic transactions."""
        conn = self._get_connection()
        applied_versions: list[int] = []
        try:
            self.ensure_migration_table(conn)
            applied = self.get_applied_versions(conn)

            for step in MIGRATIONS:
                if step.version in applied:
                    # Verify checksum integrity
                    existing_checksum = applied[step.version]["checksum"]
                    if existing_checksum != step.checksum:
                        logger.warning(
                            "Checksum mismatch for migration %d (%s): recorded %s, current %s",
                            step.version,
                            step.name,
                            existing_checksum,
                            step.checksum,
                        )
                    continue

                logger.info("Applying migration %d: %s", step.version, step.name)
                with conn:
                    conn.executescript(step.up_sql)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                        (step.version, step.name, step.checksum, utc_now_iso()),
                    )
                applied_versions.append(step.version)
        finally:
            conn.close()

        return applied_versions

    def verify_schema(self) -> dict[str, Any]:
        """Verify schema integrity and return status report."""
        conn = self._get_connection()
        try:
            self.ensure_migration_table(conn)
            applied = self.get_applied_versions(conn)
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            tables = [row["name"] for row in cursor.fetchall()]

            expected_tables = [
                "ai_sessions",
                "api_keys",
                "engineering_decisions",
                "engineering_events",
                "git_commit_links",
                "git_commits",
                "incremental_checkpoints",
                "memories",
                "memory_revisions",
                "projects",
                "repositories",
                "schema_migrations",
                "security_audit_events",
                "team_memberships",
                "teams",
                "users",
            ]
            missing_tables = [t for t in expected_tables if t not in tables]

            return {
                "current_version": max(applied.keys()) if applied else 0,
                "latest_available_version": MIGRATIONS[-1].version if MIGRATIONS else 0,
                "applied_count": len(applied),
                "is_up_to_date": len(applied) == len(MIGRATIONS),
                "tables_count": len(tables),
                "missing_tables": missing_tables,
                "status": "healthy" if not missing_tables else "degraded",
            }
        finally:
            conn.close()
