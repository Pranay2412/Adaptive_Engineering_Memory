#!/usr/bin/env python3
"""Database & Knowledge Graph Backup and Restore CLI for Synapse (Phase 17).

Usage:
    python scripts/backup_restore.py create [--note "Pre-release snapshot"]
    python scripts/backup_restore.py list
    python scripts/backup_restore.py verify backups/synapse_backup_20261015_120000.tar.gz
    python scripts/backup_restore.py restore backups/synapse_backup_20261015_120000.tar.gz
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

from backend.backup import BackupRestoreManager


def main() -> int:
    parser = argparse.ArgumentParser(description="Synapse Backup and Restore Utility")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # create
    p_create = subparsers.add_parser("create", help="Create a new verified backup archive")
    p_create.add_argument("--note", default="", help="Optional descriptive note")
    p_create.add_argument("--db", default=None, help="Database file path")
    p_create.add_argument("--graph-dir", default=None, help="Graph artifacts directory")
    p_create.add_argument("--backup-dir", default=None, help="Destination backup directory")

    # list
    p_list = subparsers.add_parser("list", help="List all available backup archives")
    p_list.add_argument("--backup-dir", default=None, help="Backup directory")

    # verify
    p_verify = subparsers.add_parser("verify", help="Verify checksum integrity of a backup archive")
    p_verify.add_argument("archive", help="Path to backup archive (.tar.gz)")

    # restore
    p_restore = subparsers.add_parser("restore", help="Restore database and artifacts from backup")
    p_restore.add_argument("archive", help="Path to backup archive (.tar.gz)")
    p_restore.add_argument("--target-db", default=None, help="Target database path")
    p_restore.add_argument("--target-graph", default=None, help="Target graph artifacts directory")

    args = parser.parse_args()

    default_db = os.environ.get("SYNAPSE_DB_PATH", "data/synapse_team_memory.db")
    db_path = getattr(args, "db", None) or default_db
    graph_dir = getattr(args, "graph_dir", None) or "graphify-out"
    backup_dir = getattr(args, "backup_dir", None) or "backups"

    manager = BackupRestoreManager(db_path=db_path, graph_dir=graph_dir, backup_dir=backup_dir)

    if args.command == "create":
        print(f"Creating backup from db '{db_path}' and graph '{graph_dir}'...")
        archive_path = manager.create_backup(note=args.note)
        print(f"[OK] Backup created successfully: {archive_path}")
        manifest = manager.inspect_backup(archive_path)
        print(f"     Backup ID:      {manifest.backup_id}")
        print(f"     Schema Version: {manifest.schema_version}")
        print(f"     Database Size:  {manifest.db_size_bytes} bytes")
        print(f"     Files Included: {len(manifest.file_checksums)}")
        return 0

    elif args.command == "list":
        backups = manager.list_backups()
        if not backups:
            print("No backups found in directory:", backup_dir)
            return 0
        print(f"Found {len(backups)} backup archive(s) in '{backup_dir}':")
        for b in backups:
            if "error" in b:
                print(f"  - {b['filename']} ({b['size_bytes']} bytes) [ERROR: {b['error']}]")
            else:
                print(
                    f"  - {b['filename']} | ID: {b['backup_id']} | Schema v{b['schema_version']} | Size: {b['size_bytes']} bytes"
                )
        return 0

    elif args.command == "verify":
        print(f"Verifying backup integrity for '{args.archive}'...")
        res = manager.verify_backup(args.archive)
        print(json.dumps(res, indent=2))
        return 0 if res["valid"] else 1

    elif args.command == "restore":
        print(f"Restoring from '{args.archive}'...")
        try:
            manifest = manager.restore_backup(
                archive_path=args.archive,
                target_db_path=args.target_db,
                target_graph_dir=args.target_graph,
            )
            print(f"[OK] Successfully restored backup: {manifest.backup_id}")
            print(f"     Schema Version: {manifest.schema_version}")
            print(f"     Table Counts:   {manifest.table_counts}")
            return 0
        except Exception as e:
            print(f"[ERROR] Restore failed: {e}")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
