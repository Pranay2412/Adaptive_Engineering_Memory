"""Unit and Integration Tests for Incremental Updates Subsystem (Phase 8).

Verifies:
1. File change detection: additions, modifications, deletions, and renames.
2. Surgical AST symbol extraction for Python and Markdown concepts.
3. Checkpoint persistence and idempotency.
4. Semantic index synchronization on incremental changes.
5. Strict equivalence and consistency between incremental update and clean full rebuild.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from backend.hybrid_retrieval import SemanticKnowledgeIndex
from backend.incremental_update import (
    FileChangeRecord,
    IncrementalChangeDetector,
    IncrementalUpdateResult,
    IncrementalUpdateService,
    RepositoryCheckpoint,
    SingleFileExtractor,
)
from backend.models import CodeSymbol, DocumentEntity
from backend.semantic_matcher import MockEmbeddingProvider
from backend.team_memory_store import TeamMemoryDocumentStore


# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """Create a multi-file sample repository on disk."""
    repo = tmp_path / "sample_project"
    repo.mkdir()

    # Core service file
    svc_code = """\"\"\"User service module.\"\"\"
class UserService:
    \"\"\"Manages user lifecycle.\"\"\"
    def __init__(self, db_client):
        self.db = db_client

    def find_user(self, user_id: str):
        \"\"\"Lookup user by ID.\"\"\"
        return self.db.query(user_id)

def create_user_service():
    \"\"\"Factory function.\"\"\"
    return UserService(None)
"""
    (repo / "user_service.py").write_text(svc_code, encoding="utf-8")

    # Utilities file
    utils_code = """\"\"\"Utility functions.\"\"\"
def format_name(first: str, last: str) -> str:
    \"\"\"Return formatted full name.\"\"\"
    return f"{first} {last}".strip()

def calculate_age(birth_year: int) -> int:
    return 2026 - birth_year
"""
    (repo / "utils.py").write_text(utils_code, encoding="utf-8")

    # Documentation file
    doc_text = """# User Architecture
Architecture guide for user lifecycle and permissions.

## Authentication Flow
Explains bearer tokens and authorization guards.

## Database Strategy
Details PostgreSQL connection pooling.
"""
    (repo / "ARCHITECTURE.md").write_text(doc_text, encoding="utf-8")

    return repo


# ---------------------------------------------------------------------------
# 1. Change Detector Tests
# ---------------------------------------------------------------------------

def test_change_detector_add_modify_delete_rename(tmp_path: Path):
    detector = IncrementalChangeDetector()
    repo_dir = tmp_path / "detector_repo"
    repo_dir.mkdir()

    f1 = repo_dir / "app.py"
    f1.write_text("def run(): pass\n", encoding="utf-8")
    f2 = repo_dir / "config.py"
    f2.write_text("DEBUG = True\n", encoding="utf-8")

    # Initial scan: all files are added
    initial_hashes = detector.scan_repository_hashes(repo_dir)
    assert len(initial_hashes) == 2
    initial_changes = detector.detect_changes(initial_hashes, previous_hashes=None)
    assert len(initial_changes) == 2
    assert all(c.change_type == "added" for c in initial_changes)

    # State 2:
    # 1. Modify app.py
    f1.write_text("def run(): print('v2')\n", encoding="utf-8")
    # 2. Add helper.py
    f3 = repo_dir / "helper.py"
    f3.write_text("def help_me(): pass\n", encoding="utf-8")
    # 3. Rename config.py -> settings.py (same content)
    f2.unlink()
    f4 = repo_dir / "settings.py"
    f4.write_text("DEBUG = True\n", encoding="utf-8")

    state2_hashes = detector.scan_repository_hashes(repo_dir)
    changes = detector.detect_changes(state2_hashes, previous_hashes=initial_hashes)

    change_map = {c.path: c for c in changes}
    assert "app.py" in change_map
    assert change_map["app.py"].change_type == "modified"

    assert "helper.py" in change_map
    assert change_map["helper.py"].change_type == "added"

    assert "settings.py" in change_map
    assert change_map["settings.py"].change_type == "renamed"
    assert change_map["settings.py"].old_path == "config.py"


# ---------------------------------------------------------------------------
# 2. Single File Extractor Tests
# ---------------------------------------------------------------------------

def test_single_file_extractor_python(sample_repo: Path):
    extractor = SingleFileExtractor()
    user_file = sample_repo / "user_service.py"

    symbols, entities, edges = extractor.extract_file(
        user_file,
        "user_service.py",
        user_id="u1",
        repository_id="repo1",
    )

    assert len(entities) == 0
    assert len(symbols) >= 3  # UserService, __init__, find_user, create_user_service

    names = {s.name: s for s in symbols}
    assert "UserService" in names
    cls_sym = names["UserService"]
    assert cls_sym.type == "class"
    assert "Manages user lifecycle." in (cls_sym.documentation or "")
    assert cls_sym.line_start == 2

    assert "find_user" in names
    method_sym = names["find_user"]
    assert method_sym.type == "method"
    assert method_sym.parent_symbol == "UserService"
    assert "def find_user" in (method_sym.signature or "")

    assert "create_user_service" in names
    func_sym = names["create_user_service"]
    assert func_sym.type == "function"

    # Verify Class DEFINES method edge
    defines_edges = [e for e in edges if e.relation == "DEFINES"]
    assert len(defines_edges) >= 2


def test_single_file_extractor_markdown(sample_repo: Path):
    extractor = SingleFileExtractor()
    doc_file = sample_repo / "ARCHITECTURE.md"

    symbols, entities, edges = extractor.extract_file(
        doc_file,
        "ARCHITECTURE.md",
        user_id="u1",
        repository_id="repo1",
    )

    assert len(symbols) == 0
    assert len(entities) >= 3

    ent_names = [e.name for e in entities]
    assert "User Architecture" in ent_names
    assert "Authentication Flow" in ent_names
    assert "Database Strategy" in ent_names


# ---------------------------------------------------------------------------
# 3. Checkpoint Persistence & SQLite Store
# ---------------------------------------------------------------------------

def test_checkpoint_persistence_sqlite():
    doc_store = TeamMemoryDocumentStore()
    service = IncrementalUpdateService(doc_store=doc_store)

    cp = RepositoryCheckpoint(
        id="cp_test_001",
        repository_id="repo-checkout",
        user_id="developer_1",
        commit_sha="abcdef123456",
        file_hashes={"main.py": "hash_111", "config.py": "hash_222"},
        symbols_by_file={"main.py": ["sym_1", "sym_2"]},
        entities_by_file={},
        edges_by_file={},
        symbol_count=2,
        entity_count=0,
        edge_count=1,
        metadata={"branch": "main"},
    )

    service.save_checkpoint(cp)

    fetched = service.get_latest_checkpoint("repo-checkout")
    assert fetched is not None
    assert fetched.id == "cp_test_001"
    assert fetched.commit_sha == "abcdef123456"
    assert fetched.file_hashes["main.py"] == "hash_111"
    assert fetched.symbols_by_file["main.py"] == ["sym_1", "sym_2"]
    assert fetched.symbol_count == 2

    # Check store listing
    cp_list = doc_store.list_checkpoints("repo-checkout")
    assert len(cp_list) == 1


# ---------------------------------------------------------------------------
# 4. Incremental Updates & Semantic Synchronization
# ---------------------------------------------------------------------------

def test_incremental_service_initial_and_idempotent(sample_repo: Path):
    doc_store = TeamMemoryDocumentStore()
    service = IncrementalUpdateService(doc_store=doc_store)

    # 1. Initial run -> must be a full rebuild
    res1 = service.run_update(
        sample_repo,
        user_id="alice",
        repository_id="repo-synapse",
    )
    assert res1.is_full_rebuild is True
    assert res1.files_added == 3
    assert res1.symbols_added >= 6
    assert res1.entities_added >= 3

    # Checkpoint should now be stored
    latest_cp = service.get_latest_checkpoint("repo-synapse")
    assert latest_cp is not None
    assert len(latest_cp.file_hashes) == 3

    # 2. Re-run without changes -> must be idempotent no-op
    res2 = service.run_update(
        sample_repo,
        user_id="alice",
        repository_id="repo-synapse",
    )
    assert res2.is_full_rebuild is False
    assert res2.has_changes is False
    assert len(res2.changes_detected) == 0


def test_incremental_service_surgical_modification(sample_repo: Path):
    doc_store = TeamMemoryDocumentStore()
    service = IncrementalUpdateService(doc_store=doc_store)
    provider = MockEmbeddingProvider()
    semantic_index = SemanticKnowledgeIndex(provider=provider)

    # Baseline run
    res_base = service.run_update(
        sample_repo,
        user_id="alice",
        repository_id="repo-synapse",
        semantic_index=semantic_index,
    )
    assert len(semantic_index.symbols) >= 6
    initial_sym_names = {s.name for s in semantic_index.symbols}
    assert "format_name" in initial_sym_names

    # Surgical edit: Add a function to utils.py and modify calculate_age
    utils_path = sample_repo / "utils.py"
    utils_new = """\"\"\"Updated utility functions.\"\"\"
def format_name(first: str, last: str) -> str:
    return f"{first.capitalize()} {last.capitalize()}".strip()

def calculate_age(birth_year: int) -> int:
    return 2026 - birth_year

def generate_uuid() -> str:
    return "0000-1111"
"""
    utils_path.write_text(utils_new, encoding="utf-8")

    # Run incremental update
    res_inc = service.run_update(
        sample_repo,
        user_id="alice",
        repository_id="repo-synapse",
        semantic_index=semantic_index,
    )
    assert res_inc.is_full_rebuild is False
    assert res_inc.files_modified == 1
    assert res_inc.files_added == 0
    assert res_inc.files_deleted == 0
    assert res_inc.changes_detected[0].path == "utils.py"

    # Semantic index must now contain generate_uuid
    updated_sym_names = {s.name for s in semantic_index.symbols}
    assert "generate_uuid" in updated_sym_names
    assert "format_name" in updated_sym_names


def test_incremental_service_deletion_and_rename(sample_repo: Path):
    doc_store = TeamMemoryDocumentStore()
    service = IncrementalUpdateService(doc_store=doc_store)

    # Baseline run
    service.run_update(
        sample_repo,
        user_id="alice",
        repository_id="repo-synapse",
    )

    # 1. Delete utils.py
    (sample_repo / "utils.py").unlink()

    # 2. Rename ARCHITECTURE.md -> DOCS.md (same content)
    arch_content = (sample_repo / "ARCHITECTURE.md").read_text(encoding="utf-8")
    (sample_repo / "ARCHITECTURE.md").unlink()
    (sample_repo / "DOCS.md").write_text(arch_content, encoding="utf-8")

    # Run incremental update
    res_inc = service.run_update(
        sample_repo,
        user_id="alice",
        repository_id="repo-synapse",
    )
    assert res_inc.files_deleted == 1
    assert res_inc.files_renamed == 1

    cp = service.get_latest_checkpoint("repo-synapse")
    assert "utils.py" not in cp.file_hashes
    assert "ARCHITECTURE.md" not in cp.file_hashes
    assert "DOCS.md" in cp.file_hashes


# ---------------------------------------------------------------------------
# 5. Incremental vs Clean Rebuild Strict Equivalence
# ---------------------------------------------------------------------------

def test_incremental_vs_clean_rebuild_strict_consistency(sample_repo: Path):
    doc_store = TeamMemoryDocumentStore()
    service = IncrementalUpdateService(doc_store=doc_store)

    # 1. Baseline Run
    service.run_update(sample_repo, user_id="bob", repository_id="repo-strict")

    # 2. Apply a realistic multi-file changeset:
    # A: Modify user_service.py
    user_file = sample_repo / "user_service.py"
    user_file.write_text(
        user_file.read_text(encoding="utf-8") + "\ndef delete_user(uid: str): pass\n",
        encoding="utf-8",
    )

    # B: Add new service metrics.py
    metrics_file = sample_repo / "metrics.py"
    metrics_file.write_text("class MetricsCollector:\n    def emit(self): pass\n", encoding="utf-8")

    # C: Delete utils.py
    (sample_repo / "utils.py").unlink()

    # D: Rename ARCHITECTURE.md to SPECS.md
    arch_text = (sample_repo / "ARCHITECTURE.md").read_text(encoding="utf-8")
    (sample_repo / "ARCHITECTURE.md").unlink()
    (sample_repo / "SPECS.md").write_text(arch_text, encoding="utf-8")

    # 3. Execute incremental update
    res_inc = service.run_update(sample_repo, user_id="bob", repository_id="repo-strict")
    assert res_inc.is_full_rebuild is False

    current_checkpoint = service.get_latest_checkpoint("repo-strict")
    assert current_checkpoint is not None

    # 4. Verify strict consistency against clean full rebuild
    consistency = service.verify_incremental_consistency(
        sample_repo,
        user_id="bob",
        repository_id="repo-strict",
        current_checkpoint=current_checkpoint,
    )

    assert consistency["agrees"] is True, f"Discrepancies found: {consistency['discrepancies']}"
    assert consistency["discrepancies"] == []
    assert consistency["incremental_symbols"] == consistency["clean_rebuild_symbols"]
    assert consistency["incremental_entities"] == consistency["clean_rebuild_entities"]
    assert consistency["incremental_symbols"] > 0
    assert consistency["incremental_entities"] > 0
