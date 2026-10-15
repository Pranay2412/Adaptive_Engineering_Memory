#!/usr/bin/env python3
"""Database Migration CLI for Synapse (Phase 17).

Usage:
    python scripts/migrate.py status [--db PATH]
    python scripts/migrate.py up [--db PATH]
    python scripts/migrate.py verify [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

# Ensure repository root is on sys.path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from backend.migrations import MIGRATIONS, MigrationManager


def main() -> int:
    parser = argparse.ArgumentParser(description="Synapse Schema Migration CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    p_status = subparsers.add_parser("status", help="Show migration status")
    p_status.add_argument("--db", default=None, help="Database file path")

    # up
    p_up = subparsers.add_parser("up", help="Apply pending migrations")
    p_up.add_argument("--db", default=None, help="Database file path")

    # verify
    p_verify = subparsers.add_parser("verify", help="Verify schema integrity")
    p_verify.add_argument("--db", default=None, help="Database file path")

    args = parser.parse_args()

    default_db = os.environ.get("SYNAPSE_DB_PATH", "data/synapse_team_memory.db")
    db_path = args.db or default_db

    manager = MigrationManager(db_path=db_path)

    if args.command == "status":
        curr = manager.get_current_version()
        pending = manager.get_pending_migrations()
        print(f"Database: {db_path}")
        print(f"Current Schema Version: {curr}")
        print(f"Pending Migrations:     {len(pending)}")
        for p in pending:
            print(f"  - Version {p.version}: {p.name} (checksum: {p.checksum})")
        return 0

    elif args.command == "up":
        pending = manager.get_pending_migrations()
        if not pending:
            print(f"Database {db_path} is already up to date at version {manager.get_current_version()}.")
            return 0
        print(f"Applying {len(pending)} pending migrations to {db_path}...")
        applied = manager.apply_pending()
        for v in applied:
            print(f"  [OK] Applied version {v}")
        print(f"Migration complete. Schema version is now {manager.get_current_version()}.")
        return 0

    elif args.command == "verify":
        report = manager.verify_schema()
        print(json.dumps(report, indent=2))
        return 0 if report["status"] == "healthy" else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
