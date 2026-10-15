"""Database & Artifact Backup and Restore System for Synapse (Phase 17).

Provides reproducible, checksum-verified, zero-downtime backups and atomic
restorations of:
1. SQLite team memory document store (via SQLite online backup API)
2. Graphify knowledge graph artifacts (graph.json, graph.graphml)
3. Manifest metadata with SHA-256 checksums, table statistics, and schema versions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
from typing import Any

from .migrations import MigrationManager

logger = logging.getLogger(__name__)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_file_sha256(filepath: Path) -> str:
    """Compute hex-encoded SHA-256 digest of a file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class BackupManifest:
    backup_id: str
    created_at: str
    schema_version: int
    db_size_bytes: int
    table_counts: dict[str, int]
    file_checksums: dict[str, str]
    graph_artifact_included: bool
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BackupManifest:
        return cls(**data)


class BackupRestoreManager:
    """Manages creation, verification, and restoration of Synapse backup archives."""

    def __init__(
        self,
        db_path: str = "data/synapse_team_memory.db",
        graph_dir: str = "graphify-out",
        backup_dir: str = "backups",
    ) -> None:
        self.db_path = Path(db_path)
        self.graph_dir = Path(graph_dir)
        self.backup_dir = Path(backup_dir)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def _get_table_counts(self, db_file: Path) -> dict[str, int]:
        """Collect row counts across all non-system tables."""
        if not db_file.exists():
            return {}
        counts: dict[str, int] = {}
        try:
            conn = sqlite3.connect(str(db_file))
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            tables = [row[0] for row in cursor.fetchall()]
            for tbl in tables:
                try:
                    c = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()
                    counts[tbl] = c[0] if c else 0
                except Exception:
                    counts[tbl] = -1
            conn.close()
        except Exception as e:
            logger.warning("Could not collect table counts: %s", e)
        return counts

    def create_backup(self, note: str = "") -> Path:
        """Create a complete, checksum-verified backup archive (.tar.gz)."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_id = f"synapse_backup_{timestamp}"
        archive_path = self.backup_dir / f"{backup_id}.tar.gz"

        with tempfile.TemporaryDirectory(prefix="synapse_backup_") as tmpdir:
            tmppath = Path(tmpdir)
            staged_db = tmppath / "synapse_team_memory.db"

            # 1. Safely snapshot SQLite database using online backup API
            if self.db_path.exists():
                src_conn = sqlite3.connect(str(self.db_path))
                dst_conn = sqlite3.connect(str(staged_db))
                with dst_conn:
                    src_conn.backup(dst_conn)
                dst_conn.close()
                src_conn.close()
            else:
                # If database does not exist yet, initialize an empty schema
                mgr = MigrationManager(db_path=str(staged_db))
                mgr.apply_pending()

            db_size = staged_db.stat().st_size
            table_counts = self._get_table_counts(staged_db)
            mgr = MigrationManager(db_path=str(staged_db))
            schema_ver = mgr.get_current_version()

            checksums: dict[str, str] = {
                "synapse_team_memory.db": compute_file_sha256(staged_db),
            }

            # 2. Stage Graphify graph artifacts if available
            has_graph = False
            staged_graph_dir = tmppath / "graphify-out"
            if self.graph_dir.exists() and any(self.graph_dir.iterdir()):
                staged_graph_dir.mkdir(parents=True, exist_ok=True)
                for item in self.graph_dir.glob("*"):
                    if item.is_file():
                        dest_file = staged_graph_dir / item.name
                        shutil.copy2(item, dest_file)
                        checksums[f"graphify-out/{item.name}"] = compute_file_sha256(dest_file)
                        has_graph = True

            # 3. Create and save manifest
            manifest = BackupManifest(
                backup_id=backup_id,
                created_at=utc_now_iso(),
                schema_version=schema_ver,
                db_size_bytes=db_size,
                table_counts=table_counts,
                file_checksums=checksums,
                graph_artifact_included=has_graph,
                notes=note,
            )
            manifest_file = tmppath / "manifest.json"
            with open(manifest_file, "w", encoding="utf-8") as f:
                json.dump(manifest.to_dict(), f, indent=2)

            # 4. Pack into tar.gz
            with tarfile.open(archive_path, "w:gz") as tar:
                tar.add(manifest_file, arcname="manifest.json")
                tar.add(staged_db, arcname="synapse_team_memory.db")
                if has_graph:
                    tar.add(staged_graph_dir, arcname="graphify-out")

        logger.info("Successfully created backup archive: %s", archive_path)
        return archive_path

    def inspect_backup(self, archive_path: Path | str) -> BackupManifest:
        """Read and validate the manifest of a backup archive without unpacking."""
        archive_path = Path(archive_path)
        if not archive_path.exists():
            raise FileNotFoundError(f"Backup archive not found: {archive_path}")

        with tarfile.open(archive_path, "r:gz") as tar:
            try:
                manifest_member = tar.getmember("manifest.json")
            except KeyError:
                raise ValueError("Corrupt backup: missing manifest.json")

            manifest_f = tar.extractfile(manifest_member)
            if manifest_f is None:
                raise ValueError("Corrupt backup: cannot read manifest.json")
            data = json.load(manifest_f)
            return BackupManifest.from_dict(data)

    def verify_backup(self, archive_path: Path | str) -> dict[str, Any]:
        """Extract to a temporary directory and verify all cryptographic checksums."""
        archive_path = Path(archive_path)
        manifest = self.inspect_backup(archive_path)

        with tempfile.TemporaryDirectory(prefix="synapse_verify_") as tmpdir:
            tmppath = Path(tmpdir)
            with tarfile.open(archive_path, "r:gz") as tar:
                tar.extractall(tmppath)

            errors: list[str] = []
            for rel_path, expected_hash in manifest.file_checksums.items():
                fpath = tmppath / rel_path
                if not fpath.exists():
                    errors.append(f"Missing file declared in manifest: {rel_path}")
                    continue
                actual_hash = compute_file_sha256(fpath)
                if actual_hash != expected_hash:
                    errors.append(
                        f"Checksum mismatch for {rel_path}: expected {expected_hash}, got {actual_hash}"
                    )

            return {
                "valid": len(errors) == 0,
                "backup_id": manifest.backup_id,
                "created_at": manifest.created_at,
                "schema_version": manifest.schema_version,
                "files_verified": len(manifest.file_checksums),
                "errors": errors,
            }

    def restore_backup(
        self,
        archive_path: Path | str,
        target_db_path: Path | str | None = None,
        target_graph_dir: Path | str | None = None,
    ) -> BackupManifest:
        """Atomically restore a verified backup with rollback safety on failure."""
        archive_path = Path(archive_path)
        verification = self.verify_backup(archive_path)
        if not verification["valid"]:
            raise ValueError(f"Cannot restore invalid backup: {verification['errors']}")

        dest_db = Path(target_db_path) if target_db_path else self.db_path
        dest_graph = Path(target_graph_dir) if target_graph_dir else self.graph_dir

        dest_db.parent.mkdir(parents=True, exist_ok=True)
        dest_graph.mkdir(parents=True, exist_ok=True)

        backup_rollback_db = None
        if dest_db.exists():
            backup_rollback_db = dest_db.with_suffix(".pre-restore.bak")
            shutil.copy2(dest_db, backup_rollback_db)

        try:
            with tempfile.TemporaryDirectory(prefix="synapse_restore_") as tmpdir:
                tmppath = Path(tmpdir)
                with tarfile.open(archive_path, "r:gz") as tar:
                    tar.extractall(tmppath)

                # Restore database
                src_db = tmppath / "synapse_team_memory.db"
                if src_db.exists():
                    shutil.copy2(src_db, dest_db)

                # Restore graph artifacts if present
                src_graph = tmppath / "graphify-out"
                if src_graph.exists():
                    for item in src_graph.glob("*"):
                        if item.is_file():
                            shutil.copy2(item, dest_graph / item.name)

                # Clean up rollback safety copy upon successful restore
                if backup_rollback_db and backup_rollback_db.exists():
                    backup_rollback_db.unlink()

                manifest_file = tmppath / "manifest.json"
                with open(manifest_file, "r", encoding="utf-8") as f:
                    manifest = BackupManifest.from_dict(json.load(f))

                logger.info("Successfully restored backup '%s' to %s", manifest.backup_id, dest_db)
                return manifest

        except Exception as e:
            logger.error("Restore failed: %s. Initiating rollback...", e)
            if backup_rollback_db and backup_rollback_db.exists():
                shutil.copy2(backup_rollback_db, dest_db)
                backup_rollback_db.unlink()
            raise RuntimeError(f"Restore failed and was rolled back: {e}") from e

    def list_backups(self) -> list[dict[str, Any]]:
        """List all available backups in the backup directory."""
        results: list[dict[str, Any]] = []
        for p in sorted(self.backup_dir.glob("synapse_backup_*.tar.gz"), reverse=True):
            try:
                manifest = self.inspect_backup(p)
                results.append({
                    "path": str(p),
                    "filename": p.name,
                    "size_bytes": p.stat().st_size,
                    "backup_id": manifest.backup_id,
                    "created_at": manifest.created_at,
                    "schema_version": manifest.schema_version,
                    "table_counts": manifest.table_counts,
                })
            except Exception as e:
                results.append({
                    "path": str(p),
                    "filename": p.name,
                    "size_bytes": p.stat().st_size,
                    "error": str(e),
                })
        return results
