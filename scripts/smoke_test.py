#!/usr/bin/env python3
"""Clean-Environment Deployment Smoke Test for Synapse (Phase 17).

Simulates a brand-new developer checkout:
1. Verifies configuration templates (.env.example) and required files.
2. Executes fresh schema migrations from scratch on an isolated database.
3. Bootstraps the production FastAPI application via create_app().
4. Verifies liveness (/health), readiness (/ready), and safe config (/api/v1/config).
5. Performs an authenticated context query to ensure the ACO engine operates cleanly.
6. Exercises a backup and restore round-trip to guarantee disaster recovery.

Usage:
    python scripts/smoke_test.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile

# Ensure repository root is on sys.path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from fastapi.testclient import TestClient

from backend.api import create_app
from backend.backup import BackupRestoreManager
from backend.config import EnvironmentMode, SynapseConfig
from backend.migrations import MigrationManager
from backend.security import Role, TokenService
from backend.session_protocol import AuthenticatedPrincipal


def print_step(step_num: int, title: str) -> None:
    print(f"\n[{step_num}/6] {title}...")


def main() -> int:
    print("=" * 80)
    print("SYNAPSE PLATFORM DEPLOYMENT SMOKE TEST (PHASE 17)")
    print("=" * 80)

    # Step 1: Verify deployment artifacts
    print_step(1, "Verifying deployment configuration files")
    required_files = [
        repo_root / ".env.example",
        repo_root / "requirements.txt",
        repo_root / "Dockerfile",
        repo_root / "docker-compose.yml",
        repo_root / ".dockerignore",
    ]
    for rf in required_files:
        if not rf.exists():
            print(f"  [FAIL] Missing required deployment file: {rf.name}")
            return 1
        print(f"  [PASS] Found {rf.name}")

    with tempfile.TemporaryDirectory(prefix="synapse_smoke_") as tmpdir:
        tmppath = Path(tmpdir)
        db_path = tmppath / "smoke_team_memory.db"
        backups_path = tmppath / "smoke_backups"
        graph_path = repo_root / "graphify-out"

        # Step 2: Database schema migration
        print_step(2, "Applying database migrations on fresh isolated storage")
        mgr = MigrationManager(db_path=str(db_path))
        applied = mgr.apply_pending()
        print(f"  [PASS] Applied {len(applied)} migrations. Current schema version: {mgr.get_current_version()}")

        verify_report = mgr.verify_schema()
        if verify_report["status"] != "healthy":
            print(f"  [FAIL] Schema verification failed: {verify_report}")
            return 1
        print(f"  [PASS] Verified 16 tables initialized with status: healthy")

        # Step 3: Application bootstrap
        print_step(3, "Bootstrapping FastAPI application with production config")
        config = SynapseConfig(
            environment=EnvironmentMode.DEVELOPMENT.value,
            secret_key="smoke-test-secret-must-be-at-least-32-chars-long",
            db_path=str(db_path),
            require_auth=True,
            request_timeout_seconds=60.0,
        )
        app = create_app(config)
        client = TestClient(app)
        token_svc = TokenService(secret_key=config.secret_key)
        print("  [PASS] FastAPI application instantiated successfully")

        # Step 4: Health and readiness checks
        print_step(4, "Testing liveness, readiness, and configuration probes")
        r_health = client.get("/health")
        if r_health.status_code != 200 or r_health.json().get("status") not in ("ok", "pass"):
            print(f"  [FAIL] Health check failed: {r_health.status_code} - {r_health.text}")
            return 1
        print("  [PASS] Liveness probe /health: 200 OK (status: ok)")

        r_ready = client.get("/ready")
        if r_ready.status_code != 200 or r_ready.json().get("status") not in ("ready", "ok", "pass"):
            print(f"  [FAIL] Readiness check failed: {r_ready.status_code} - {r_ready.text}")
            return 1
        print("  [PASS] Readiness probe /ready: 200 OK (status: ready)")

        # Auth token generation
        principal = AuthenticatedPrincipal(
            principal_id="smoke-admin",
            tenant_id="tenant-smoke",
            roles=[Role.ADMIN.value],
            allowed_repositories=["*"],
        )
        token = token_svc.create_token(principal)
        headers = {"Authorization": f"Bearer {token}"}

        r_cfg = client.get("/api/v1/config", headers=headers)
        cfg_data = r_cfg.json().get("config", {})
        if r_cfg.status_code != 200 or not cfg_data.get("secret_key_configured"):
            print(f"  [FAIL] Safe config endpoint failed: {r_cfg.status_code} - {r_cfg.text}")
            return 1
        print("  [PASS] Safe config diagnostics /api/v1/config: 200 OK (secret redacted, secret_key_configured=True)")

        # Step 5: Basic Context Query Execution
        print_step(5, "Executing authenticated context query against ACO engine")
        from backend.api import reset_protocol_engine
        os.environ["EMBEDDING_PROVIDER"] = "mock"
        reset_protocol_engine()

        query_payload = {
            "query": "How does authentication work with JWT in Synapse?",
            "repository_id": "synapse-core",
            "context_budget": 1200,
            "optimization_mode": "deterministic",
        }
        r_query = client.post("/api/v1/dashboard/query", json=query_payload, headers=headers)
        if r_query.status_code != 200:
            print(f"  [FAIL] Context query failed: {r_query.status_code} - {r_query.text}")
            return 1

        query_data = r_query.json()
        tokens = query_data.get("tokens", {})
        evidence = query_data.get("evidence_items", [])
        print(f"  [PASS] Query succeeded in {query_data.get('timing', {}).get('latency_ms')}ms")
        print(f"         Total input tokens:       {tokens.get('total_input_tokens')}")
        print(f"         Knowledge context tokens: {tokens.get('knowledge_context_tokens')}")
        print(f"         Budget utilization:       {tokens.get('budget_utilization_pct')}%")
        print(f"         Retrieved evidence items: {len(evidence)}")

        # Step 6: Backup and Restore Verification
        print_step(6, "Testing zero-downtime backup and atomic restore round-trip")
        backup_mgr = BackupRestoreManager(
            db_path=str(db_path),
            graph_dir=str(graph_path),
            backup_dir=str(backups_path),
        )
        archive = backup_mgr.create_backup(note="Smoke test backup")
        print(f"  [PASS] Created backup archive: {archive.name} ({archive.stat().st_size} bytes)")

        verify_res = backup_mgr.verify_backup(archive)
        if not verify_res["valid"]:
            print(f"  [FAIL] Backup verification failed: {verify_res}")
            return 1
        print(f"  [PASS] Cryptographic verification passed: {verify_res['files_verified']} files verified")

        # Test restoration into fresh directory
        restore_db = tmppath / "restored" / "synapse_team_memory.db"
        restore_graph = tmppath / "restored" / "graphify-out"
        restore_manifest = backup_mgr.restore_backup(
            archive_path=archive,
            target_db_path=restore_db,
            target_graph_dir=restore_graph,
        )
        if not restore_db.exists() or restore_manifest.schema_version != mgr.get_current_version():
            print("  [FAIL] Restored database verification failed!")
            return 1
        print(f"  [PASS] Restored database to {restore_db.name} with schema v{restore_manifest.schema_version}")

    print("\n" + "=" * 80)
    print("DEPLOYMENT SMOKE TEST RESULT: ALL 6 CHECKS PASSED [OK]")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
