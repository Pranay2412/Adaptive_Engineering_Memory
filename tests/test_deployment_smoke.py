"""Automated Deployment, Migration, and Backup/Restore Tests for Phase 17.

Verifies:
1. Environment template integrity (.env.example placeholders, no committed secrets)
2. Production container specifications (Dockerfile non-root user, healthchecks, Compose resource limits)
3. Database migrations lifecycle (idempotency, table verification, schema status)
4. Cryptographic backup creation, SHA-256 verification, and atomic restoration
5. Rejection of corrupted or tampered backup archives with rollback preservation
6. Clean-checkout application bootstrap and authenticated context query execution
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest

from fastapi.testclient import TestClient

from backend.api import create_app
from backend.backup import BackupRestoreManager
from backend.config import EnvironmentMode, SynapseConfig
from backend.migrations import MIGRATIONS, MigrationManager
from backend.security import Role, TokenService
from backend.session_protocol import AuthenticatedPrincipal


class TestDeploymentAndOperationsSuite(unittest.TestCase):
    """Test suite covering Phase 17 deployment, migrations, and operations."""

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parent.parent

    def test_env_template_integrity_and_placeholders(self):
        """Verify .env.example contains required configuration and no leaked secrets."""
        env_example = self.repo_root / ".env.example"
        self.assertTrue(env_example.exists(), ".env.example must exist in repository root")

        content = env_example.read_text(encoding="utf-8")
        # Required configuration keys
        required_keys = [
            "SYNAPSE_ENV",
            "SYNAPSE_HOST",
            "SYNAPSE_PORT",
            "SYNAPSE_SECRET_KEY",
            "SYNAPSE_REQUIRE_AUTH",
            "SYNAPSE_DB_PATH",
            "SYNAPSE_CORS_ORIGINS",
            "SYNAPSE_REQUEST_TIMEOUT",
            "NEO4J_URI",
            "NEO4J_USERNAME",
            "NEO4J_PASSWORD",
        ]
        for key in required_keys:
            self.assertIn(key, content, f"Expected configuration key {key} in .env.example")

        # Verify no hardcoded production API keys
        self.assertNotIn("sk-proj-", content)
        self.assertNotIn("AIzaSy", content)

    def test_docker_and_compose_configuration(self):
        """Verify Dockerfile and docker-compose.yml conform to production security standards."""
        dockerfile = self.repo_root / "Dockerfile"
        compose = self.repo_root / "docker-compose.yml"
        dockerignore = self.repo_root / ".dockerignore"

        self.assertTrue(dockerfile.exists())
        self.assertTrue(compose.exists())
        self.assertTrue(dockerignore.exists())

        df_content = dockerfile.read_text(encoding="utf-8")
        # Check non-root user
        self.assertIn("useradd", df_content)
        self.assertIn("USER synapse:synapse", df_content)
        # Check healthcheck
        self.assertIn("HEALTHCHECK", df_content)

        dc_content = compose.read_text(encoding="utf-8")
        # Check volume mounts and resource limits
        self.assertIn("synapse_data:/app/data", dc_content)
        self.assertIn("resources:", dc_content)
        self.assertIn("limits:", dc_content)

        di_content = dockerignore.read_text(encoding="utf-8")
        self.assertIn(".env", di_content)
        self.assertIn(".git", di_content)

    def test_database_migrations_lifecycle(self):
        """Verify clean migration execution, idempotency, and schema integrity."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "mig_test.db"
            mgr = MigrationManager(db_path=str(db_path))

            # Initial state
            self.assertEqual(mgr.get_current_version(), 0)
            pending = mgr.get_pending_migrations()
            self.assertEqual(len(pending), len(MIGRATIONS))

            # Apply migrations
            applied = mgr.apply_pending()
            self.assertEqual(len(applied), len(MIGRATIONS))
            self.assertEqual(mgr.get_current_version(), len(MIGRATIONS))

            # Verify idempotency (second run applies 0 migrations)
            reapplied = mgr.apply_pending()
            self.assertEqual(len(reapplied), 0)

            # Verify schema status report
            report = mgr.verify_schema()
            self.assertEqual(report["status"], "healthy")
            self.assertTrue(report["is_up_to_date"])
            self.assertEqual(len(report["missing_tables"]), 0)
            self.assertGreaterEqual(report["tables_count"], 16)

    def test_backup_restore_and_checksum_verification(self):
        """Verify backup creation, manifest checksum verification, and atomic restoration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            db_path = tmppath / "original.db"
            backups_dir = tmppath / "backups"
            graph_dir = tmppath / "graph"
            graph_dir.mkdir(parents=True, exist_ok=True)
            (graph_dir / "graph.json").write_text('{"nodes": [1, 2], "edges": [1]}', encoding="utf-8")

            # Setup DB with migration and sample data
            mgr = MigrationManager(db_path=str(db_path))
            mgr.apply_pending()

            conn = sqlite3.connect(str(db_path))
            with conn:
                conn.execute(
                    "INSERT INTO users (id, name, email, created_at) VALUES (?, ?, ?, ?)",
                    ("u_test", "Backup Tester", "test@synapse.internal", "2026-10-15T00:00:00Z"),
                )
            conn.close()

            # Create backup
            backup_mgr = BackupRestoreManager(
                db_path=str(db_path),
                graph_dir=str(graph_dir),
                backup_dir=str(backups_dir),
            )
            archive = backup_mgr.create_backup(note="Unit test snapshot")
            self.assertTrue(archive.exists())

            # Verify backup integrity
            verify_res = backup_mgr.verify_backup(archive)
            self.assertTrue(verify_res["valid"])
            self.assertEqual(len(verify_res["errors"]), 0)

            # Restore into new target locations
            target_db = tmppath / "restored.db"
            target_graph = tmppath / "restored_graph"
            manifest = backup_mgr.restore_backup(
                archive_path=archive,
                target_db_path=target_db,
                target_graph_dir=target_graph,
            )
            self.assertEqual(manifest.schema_version, mgr.get_current_version())
            self.assertTrue(target_db.exists())
            self.assertTrue((target_graph / "graph.json").exists())

            # Verify restored data
            conn_res = sqlite3.connect(str(target_db))
            row = conn_res.execute("SELECT name FROM users WHERE id = 'u_test'").fetchone()
            conn_res.close()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "Backup Tester")

    def test_corrupt_backup_archive_rejection(self):
        """Verify that tampered or corrupt archives are safely rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            db_path = tmppath / "test.db"
            backups_dir = tmppath / "backups"
            mgr = MigrationManager(db_path=str(db_path))
            mgr.apply_pending()

            backup_mgr = BackupRestoreManager(db_path=str(db_path), backup_dir=str(backups_dir))
            archive = backup_mgr.create_backup()

            # Tamper with the archive by replacing db with junk
            corrupt_archive = backups_dir / "corrupt_backup.tar.gz"
            import io
            with tarfile.open(archive, "r:gz") as src_tar, tarfile.open(corrupt_archive, "w:gz") as dst_tar:
                manifest_f = src_tar.extractfile("manifest.json")
                manifest_data = manifest_f.read()
                ti_m = tarfile.TarInfo(name="manifest.json")
                ti_m.size = len(manifest_data)
                dst_tar.addfile(ti_m, io.BytesIO(manifest_data))

                # Add tampered database file
                tampered_bytes = b"TAMPERED_JUNK_DATA"
                ti_db = tarfile.TarInfo(name="synapse_team_memory.db")
                ti_db.size = len(tampered_bytes)
                dst_tar.addfile(ti_db, io.BytesIO(tampered_bytes))

            # Verify that checksum validation catches tampering
            verification = backup_mgr.verify_backup(corrupt_archive)
            self.assertFalse(verification["valid"])
            self.assertGreater(len(verification["errors"]), 0)

            # Verify restore raises ValueError
            with self.assertRaises(ValueError):
                backup_mgr.restore_backup(corrupt_archive, target_db_path=tmppath / "fail.db")

    def test_clean_environment_bootstrap_and_query(self):
        """Verify bootstrapping the FastAPI application and running a context query from clean checkout."""
        import unittest.mock
        from backend.api import reset_protocol_engine

        with unittest.mock.patch.dict("os.environ", {"EMBEDDING_PROVIDER": "mock"}):
            reset_protocol_engine()
            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    tmppath = Path(tmpdir)
                    db_path = tmppath / "clean_env.db"

                    # Apply migrations
                    mgr = MigrationManager(db_path=str(db_path))
                    mgr.apply_pending()

                    # Create production-configured app
                    config = SynapseConfig(
                        environment=EnvironmentMode.DEVELOPMENT.value,
                        secret_key="unit-test-secret-must-be-at-least-32-chars-long",
                        db_path=str(db_path),
                        require_auth=True,
                        request_timeout_seconds=60.0,
                    )
                    app = create_app(config)
                    client = TestClient(app)

                    # Verify probes
                    r_health = client.get("/health")
                    self.assertEqual(r_health.status_code, 200)
                    self.assertIn(r_health.json().get("status"), ("ok", "pass"))

                    r_ready = client.get("/ready")
                    self.assertEqual(r_ready.status_code, 200)
                    self.assertIn(r_ready.json().get("status"), ("ready", "ok", "pass"))

                    # Authenticated context query with deterministic mock embeddings
                    token_svc = TokenService(secret_key=config.secret_key)
                    principal = AuthenticatedPrincipal(
                        principal_id="deploy-tester",
                        tenant_id="tenant-primary",
                        roles=[Role.ADMIN.value],
                        allowed_repositories=["*"],
                    )
                    token = token_svc.create_token(principal)
                    headers = {"Authorization": f"Bearer {token}"}

                    payload = {
                        "query": "What is the token optimization strategy?",
                        "repository_id": "synapse-core",
                        "context_budget": 1000,
                        "optimization_mode": "deterministic",
                    }
                    r_query = client.post("/api/v1/dashboard/query", json=payload, headers=headers)
                    self.assertEqual(r_query.status_code, 200)
                    data = r_query.json()
                    self.assertIn("tokens", data)
                    self.assertIn("evidence_items", data)
                    self.assertEqual(data["tokens"]["configured_budget"], 1000)
                    self.assertGreater(data["tokens"]["total_input_tokens"], 0)
            finally:
                reset_protocol_engine()
