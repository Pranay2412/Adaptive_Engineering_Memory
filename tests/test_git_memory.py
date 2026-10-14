"""Unit and Integration Tests for Git & Activity Memory (Phase 7).

Verifies commit extraction, parent/merge detection, rename tracking,
selective diff bounding, symbol/decision linking, idempotent persistence,
and historical ACO retrieval.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from backend.git_memory import (
    MAX_FILE_DIFF_CHARS,
    MAX_SELECTIVE_DIFF_CHARS,
    GitCommit,
    GitCommitLink,
    GitCommitLinker,
    GitMemoryExtractor,
    GitMemoryService,
)
from backend.models import CodeSymbol, DocumentEntity, EngineeringDecision
from backend.team_memory_store import TeamMemoryDocumentStore


# ---------------------------------------------------------------------------
# Test Helpers
# ---------------------------------------------------------------------------

def run_git(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Execute git command with isolated test user identity."""
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "Synapse Engineer"
    env["GIT_AUTHOR_EMAIL"] = "engineer@synapse.ai"
    env["GIT_COMMITTER_NAME"] = "Synapse Committer"
    env["GIT_COMMITTER_EMAIL"] = "committer@synapse.ai"
    return subprocess.run(
        ["git"] + cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """Create a temporary initialized Git repository on 'main' branch."""
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir()
    run_git(["init"], repo_dir)
    run_git(["checkout", "-B", "main"], repo_dir)
    return repo_dir


# ---------------------------------------------------------------------------
# 1. Model Serialization & Deserialization
# ---------------------------------------------------------------------------

def test_git_commit_and_link_serialization():
    commit = GitCommit(
        commit_sha="abcd1234abcd1234abcd1234abcd1234abcd1234",
        repository_id="repo-synapse",
        short_sha="abcd1234",
        author_name="Alice Developer",
        author_email="alice@synapse.ai",
        authored_at="2026-10-14T08:00:00Z",
        committer_name="Alice Developer",
        committer_email="alice@synapse.ai",
        committed_at="2026-10-14T08:00:00Z",
        message="feat(auth): add OAuth2 token verification\n\nImplements JWT validation.",
        summary="feat(auth): add OAuth2 token verification",
        parent_shas=["0000000000000000000000000000000000000000"],
        is_merge=False,
        branch="main",
        files_changed=["auth/jwt.py", "auth/verifier.py"],
        change_summary={"auth/jwt.py": "added", "auth/verifier.py": "modified"},
        selective_diff={"auth/jwt.py": "+ def verify_token(): ..."},
        metadata={"build": "passed"},
    )

    data = commit.to_dict()
    assert data["commit_sha"] == "abcd1234abcd1234abcd1234abcd1234abcd1234"
    assert data["files_changed"] == ["auth/jwt.py", "auth/verifier.py"]

    restored = GitCommit.from_dict(data)
    assert restored.commit_sha == commit.commit_sha
    assert restored.summary == commit.summary
    assert restored.change_summary == commit.change_summary
    assert restored.selective_diff == commit.selective_diff

    # Test Hybrid Result conversion
    hybrid_res = commit.to_hybrid_result(score=0.92, matched_terms=["oauth2", "jwt"])
    assert hybrid_res.provenance.source_type == "git_commit"
    assert hybrid_res.score == 0.92
    assert hybrid_res.metadata["author"] == "Alice Developer"
    assert "oauth2" in hybrid_res.provenance.matched_terms

    link = GitCommitLink(
        commit_sha="abcd1234",
        repository_id="repo-synapse",
        target_type="symbol",
        target_id="sym_jwt_verify",
        confidence=0.95,
        reason="Modified function verify_token",
    )
    link_data = link.to_dict()
    restored_link = GitCommitLink.from_dict(link_data)
    assert restored_link.target_id == "sym_jwt_verify"
    assert restored_link.confidence == 0.95


# ---------------------------------------------------------------------------
# 2. Extractor Edge Cases: Non-Git & Empty Repositories
# ---------------------------------------------------------------------------

def test_non_git_directory_fallback(tmp_path: Path):
    plain_dir = tmp_path / "not_a_git_repo"
    plain_dir.mkdir()

    extractor = GitMemoryExtractor()
    assert extractor.is_git_repository(plain_dir) is False

    commits = extractor.extract_commits(plain_dir, "repo-fake")
    assert commits == []


def test_empty_git_repository(temp_git_repo: Path):
    extractor = GitMemoryExtractor()
    assert extractor.is_git_repository(temp_git_repo) is True

    commits = extractor.extract_commits(temp_git_repo, "repo-empty")
    assert commits == []


# ---------------------------------------------------------------------------
# 3. Commit Extraction, Changes, and Renames
# ---------------------------------------------------------------------------

def test_commit_extraction_add_modify_delete(temp_git_repo: Path):
    # 1. Initial commit (add files)
    f1 = temp_git_repo / "service.py"
    f1.write_text("def run():\n    return 'initial'\n", encoding="utf-8")
    run_git(["add", "service.py"], temp_git_repo)
    run_git(["commit", "-m", "Initial commit: add service.py"], temp_git_repo)

    # 2. Second commit (modify service.py, add utils.py)
    f1.write_text("def run():\n    return 'updated'\n", encoding="utf-8")
    f2 = temp_git_repo / "utils.py"
    f2.write_text("def helper():\n    return 42\n", encoding="utf-8")
    run_git(["add", "service.py", "utils.py"], temp_git_repo)
    run_git(["commit", "-m", "feat: update service and add helper"], temp_git_repo)

    extractor = GitMemoryExtractor()
    commits = extractor.extract_commits(temp_git_repo, "repo-test")

    assert len(commits) == 2
    head_commit = commits[0]
    assert head_commit.summary == "feat: update service and add helper"
    assert "service.py" in head_commit.files_changed
    assert "utils.py" in head_commit.files_changed
    assert head_commit.change_summary.get("service.py") == "modified"
    assert head_commit.change_summary.get("utils.py") == "added"
    assert head_commit.is_merge is False
    assert len(head_commit.parent_shas) == 1


def test_file_rename_detection(temp_git_repo: Path):
    f_orig = temp_git_repo / "legacy_handler.py"
    f_orig.write_text("# Old Handler\nclass Handler:\n    pass\n", encoding="utf-8")
    run_git(["add", "legacy_handler.py"], temp_git_repo)
    run_git(["commit", "-m", "Add legacy handler"], temp_git_repo)

    # Rename file using git mv
    run_git(["mv", "legacy_handler.py", "modern_handler.py"], temp_git_repo)
    run_git(["commit", "-m", "refactor: rename legacy_handler to modern_handler"], temp_git_repo)

    extractor = GitMemoryExtractor()
    commits = extractor.extract_commits(temp_git_repo, "repo-test")

    assert len(commits) == 2
    rename_commit = commits[0]
    assert "modern_handler.py" in rename_commit.files_changed
    assert rename_commit.change_summary.get("modern_handler.py") == "renamed_from:legacy_handler.py"


# ---------------------------------------------------------------------------
# 4. Multi-Parent Merge Commit Handling
# ---------------------------------------------------------------------------

def test_multi_parent_merge_commit(temp_git_repo: Path):
    # Base commit on main
    base_file = temp_git_repo / "README.md"
    base_file.write_text("# Project\n", encoding="utf-8")
    run_git(["add", "README.md"], temp_git_repo)
    run_git(["commit", "-m", "Initial commit"], temp_git_repo)

    # Branch 'feature-cache'
    run_git(["checkout", "-b", "feature-cache"], temp_git_repo)
    cache_file = temp_git_repo / "cache.py"
    cache_file.write_text("class Cache:\n    pass\n", encoding="utf-8")
    run_git(["add", "cache.py"], temp_git_repo)
    run_git(["commit", "-m", "feat: implement cache"], temp_git_repo)

    # Switch back to main and commit another file
    run_git(["checkout", "main"], temp_git_repo)
    main_file = temp_git_repo / "main.py"
    main_file.write_text("import os\n", encoding="utf-8")
    run_git(["add", "main.py"], temp_git_repo)
    run_git(["commit", "-m", "chore: setup main entrypoint"], temp_git_repo)

    # Merge feature-cache into main with --no-ff
    run_git(["merge", "feature-cache", "--no-ff", "-m", "Merge branch feature-cache into main"], temp_git_repo)

    extractor = GitMemoryExtractor()
    commits = extractor.extract_commits(temp_git_repo, "repo-test")

    merge_commit = commits[0]
    assert merge_commit.is_merge is True
    assert len(merge_commit.parent_shas) == 2
    assert "Merge branch feature-cache" in merge_commit.summary


# ---------------------------------------------------------------------------
# 5. Selective Diff Bounding
# ---------------------------------------------------------------------------

def test_selective_diff_truncation(temp_git_repo: Path):
    # Commit a large file that exceeds MAX_FILE_DIFF_CHARS
    large_file = temp_git_repo / "large_data.py"
    large_content = "\n".join(f"ROW_{i} = 'some payload line {i}'" for i in range(100))
    large_file.write_text(large_content, encoding="utf-8")
    run_git(["add", "large_data.py"], temp_git_repo)
    run_git(["commit", "-m", "Add large dataset"], temp_git_repo)

    extractor = GitMemoryExtractor()
    commits = extractor.extract_commits(temp_git_repo, "repo-test", include_diffs=True)

    assert len(commits) == 1
    diff = commits[0].selective_diff.get("large_data.py", "")
    assert diff != ""
    assert "... [diff truncated]" in diff
    assert len(diff) <= MAX_FILE_DIFF_CHARS + 50


# ---------------------------------------------------------------------------
# 6. Entity & Decision Linker
# ---------------------------------------------------------------------------

def test_git_commit_linker_symbols_and_decisions():
    linker = GitCommitLinker()

    commit = GitCommit(
        commit_sha="c111",
        repository_id="repo-synapse",
        short_sha="c111",
        author_name="Dev",
        author_email="dev@synapse.ai",
        authored_at="2026-10-14T10:00:00Z",
        committer_name="Dev",
        committer_email="dev@synapse.ai",
        committed_at="2026-10-14T10:00:00Z",
        message="feat(db): implement PostgresConnectionPool based on ADR-042\n\nResolves issue with connection leak.",
        summary="feat(db): implement PostgresConnectionPool based on ADR-042",
        files_changed=["backend/db/pool.py"],
        change_summary={"backend/db/pool.py": "added"},
        selective_diff={"backend/db/pool.py": "+ class PostgresConnectionPool:\n+     pass"},
    )

    symbols = [
        CodeSymbol(
            id="sym_pool",
            name="PostgresConnectionPool",
            file="backend/db/pool.py",
            type="class",
            line_start=1,
            line_end=10,
        )
    ]
    decisions = [
        EngineeringDecision(
            id="ADR-042",
            title="Migrate to Postgres Connection Pooling",
            rationale="Eliminate connection leaks and handle higher concurrency.",
        )
    ]

    links = linker.link_commits([commit], symbols=symbols, decisions=decisions)

    assert len(links) >= 2
    # Verify symbol link
    sym_link = next((l for l in links if l.target_type == "code_symbol"), None)
    assert sym_link is not None
    assert sym_link.target_id == "sym_pool"
    assert sym_link.confidence >= 0.90

    # Verify decision link
    dec_link = next((l for l in links if l.target_type == "decision"), None)
    assert dec_link is not None
    assert dec_link.target_id == "ADR-042"
    assert dec_link.confidence >= 0.95


# ---------------------------------------------------------------------------
# 7. GitMemoryService & TeamMemoryDocumentStore Persistence & Idempotency
# ---------------------------------------------------------------------------

def test_git_memory_service_ingestion_and_idempotency(temp_git_repo: Path):
    # Setup 2 commits in repo
    f = temp_git_repo / "auth.py"
    f.write_text("class Authenticator:\n    pass\n", encoding="utf-8")
    run_git(["add", "auth.py"], temp_git_repo)
    run_git(["commit", "-m", "feat: create Authenticator class (ADR-010)"], temp_git_repo)

    doc_store = TeamMemoryDocumentStore()
    service = GitMemoryService(doc_store=doc_store)

    decisions = [
        EngineeringDecision(
            id="ADR-010",
            title="Authentication Standard",
            rationale="Adopt bearer token auth.",
        )
    ]

    # Ingest repository
    stats_first = service.ingest_repository_git(
        repo_path=temp_git_repo,
        repository_id="repo-security",
        decisions=decisions,
    )
    assert stats_first["commits_ingested"] == 1
    assert stats_first["links_derived"] >= 1

    # Check store methods
    commits = doc_store.list_git_commits("repo-security")
    assert len(commits) == 1
    c = commits[0]
    assert c.repository_id == "repo-security"
    assert "Authenticator" in c.summary

    links = doc_store.get_git_commit_links(c.commit_sha, "repo-security")
    assert len(links) >= 1
    assert links[0].target_id == "ADR-010"

    # Idempotency check: run ingestion again
    stats_second = service.ingest_repository_git(
        repo_path=temp_git_repo,
        repository_id="repo-security",
        decisions=decisions,
    )
    assert stats_second["commits_ingested"] == 1

    # Confirm count hasn't doubled
    commits_after = doc_store.list_git_commits("repo-security")
    assert len(commits_after) == 1


# ---------------------------------------------------------------------------
# 8. ACO Historical Retrieval
# ---------------------------------------------------------------------------

def test_aco_historical_retrieval(temp_git_repo: Path):
    f1 = temp_git_repo / "cache.py"
    f1.write_text("def get_cache(): pass\n", encoding="utf-8")
    run_git(["add", "cache.py"], temp_git_repo)
    run_git(["commit", "-m", "perf(cache): introduce Redis tiered caching layer"], temp_git_repo)

    f2 = temp_git_repo / "db.py"
    f2.write_text("def get_db(): pass\n", encoding="utf-8")
    run_git(["add", "db.py"], temp_git_repo)
    run_git(["commit", "-m", "fix(db): handle transaction rollback on timeout"], temp_git_repo)

    doc_store = TeamMemoryDocumentStore()
    service = GitMemoryService(doc_store=doc_store)

    service.ingest_repository_git(
        repo_path=temp_git_repo,
        repository_id="repo-backend",
    )

    # Query 1: Redis caching
    results_redis = service.retrieve_for_aco(
        query="Why did we introduce Redis tiered caching?",
        repository_id="repo-backend",
        limit=5,
    )
    assert len(results_redis) >= 1
    top_hit = results_redis[0]
    assert top_hit.provenance.source_type == "git_commit"
    assert "Redis" in (top_hit.content_snippet + top_hit.entity)
    assert top_hit.score > 0.5

    # Query 2: Transaction rollback
    results_db = service.retrieve_for_aco(
        query="How are database transaction timeouts handled?",
        repository_id="repo-backend",
        limit=5,
    )
    assert len(results_db) >= 1
    assert "rollback" in (results_db[0].content_snippet + results_db[0].entity)

    # Query 3: Non-matching repository isolation
    results_other = service.retrieve_for_aco(
        query="Redis tiered caching",
        repository_id="repo-other-unrelated",
        limit=5,
    )
    assert len(results_other) == 0
