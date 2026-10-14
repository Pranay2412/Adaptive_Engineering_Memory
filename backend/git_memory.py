"""Git & Activity Memory subsystem for Adaptive Engineering Memory (Phase 7).

Provides traceable Git activity ingestion, commit metadata extraction,
selective diff summaries, multi-parent merge handling, rename/delete tracking,
commit-to-code/decision linking, and historical retrieval integration for the ACO.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    CodeSymbol,
    DocumentEntity,
    EngineeringDecision,
    HybridRetrievalResult,
    ResultProvenance,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

# Field and record delimiters for git log parsing
COMMIT_RECORD_DELIM = "\x1e"  # ASCII record separator
COMMIT_FIELD_DELIM = "\x1f"   # ASCII unit separator

MAX_SELECTIVE_DIFF_CHARS = 2048
MAX_FILE_DIFF_CHARS = 512


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass
class GitCommit:
    """Traceable record of a Git commit with parent hierarchy and changed paths."""

    commit_sha: str
    repository_id: str
    short_sha: str
    author_name: str
    author_email: str
    authored_at: str
    committer_name: str
    committer_email: str
    committed_at: str
    message: str
    summary: str
    parent_shas: list[str] = field(default_factory=list)
    is_merge: bool = False
    branch: str | None = None
    files_changed: list[str] = field(default_factory=list)
    change_summary: dict[str, str] = field(default_factory=dict)  # path -> "added" | "modified" | "deleted" | "renamed"
    selective_diff: dict[str, str] = field(default_factory=dict)  # path -> truncated diff
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        """Serialize GitCommit to dictionary."""
        return {
            "commit_sha": self.commit_sha,
            "repository_id": self.repository_id,
            "short_sha": self.short_sha,
            "author_name": self.author_name,
            "author_email": self.author_email,
            "authored_at": self.authored_at,
            "committer_name": self.committer_name,
            "committer_email": self.committer_email,
            "committed_at": self.committed_at,
            "message": self.message,
            "summary": self.summary,
            "parent_shas": list(self.parent_shas),
            "is_merge": self.is_merge,
            "branch": self.branch,
            "files_changed": list(self.files_changed),
            "change_summary": dict(self.change_summary),
            "selective_diff": dict(self.selective_diff),
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GitCommit:
        """Construct GitCommit from dictionary."""
        return cls(
            commit_sha=str(data["commit_sha"]),
            repository_id=str(data["repository_id"]),
            short_sha=str(data.get("short_sha") or data["commit_sha"][:8]),
            author_name=str(data.get("author_name", "")),
            author_email=str(data.get("author_email", "")),
            authored_at=str(data.get("authored_at", "")),
            committer_name=str(data.get("committer_name", "")),
            committer_email=str(data.get("committer_email", "")),
            committed_at=str(data.get("committed_at", "")),
            message=str(data.get("message", "")),
            summary=str(data.get("summary") or data.get("message", "").split("\n")[0]),
            parent_shas=list(data.get("parent_shas", [])),
            is_merge=bool(data.get("is_merge", len(data.get("parent_shas", [])) >= 2)),
            branch=data.get("branch"),
            files_changed=list(data.get("files_changed", [])),
            change_summary=dict(data.get("change_summary", {})),
            selective_diff=dict(data.get("selective_diff", {})),
            metadata=dict(data.get("metadata", {})),
            created_at=str(data.get("created_at") or utc_now_iso()),
        )

    def to_hybrid_result(self, score: float = 0.85, matched_terms: list[str] | None = None) -> HybridRetrievalResult:
        """Convert GitCommit into a HybridRetrievalResult for the ACO context packer."""
        files_preview = ", ".join(self.files_changed[:4])
        if len(self.files_changed) > 4:
            files_preview += f" (+{len(self.files_changed) - 4} more)"

        snippet_lines = [
            f"Commit: {self.short_sha} | Author: {self.author_name} <{self.author_email}> | Date: {self.committed_at[:10]}",
            f"Message: {self.summary}",
        ]
        if self.is_merge:
            snippet_lines.append(f"Merge: {' '.join(s[:7] for s in self.parent_shas)}")
        if self.files_changed:
            snippet_lines.append(f"Files Modified: {files_preview}")
        if self.selective_diff:
            diff_samples = []
            for path, diff in list(self.selective_diff.items())[:2]:
                clean_diff = "\n".join(diff.splitlines()[:5])
                diff_samples.append(f"--- {path} ---\n{clean_diff}")
            snippet_lines.append("Changes:\n" + "\n".join(diff_samples))

        content_snippet = "\n".join(snippet_lines)

        prov = ResultProvenance(
            source_type="git_commit",
            repository_id=self.repository_id,
            created_at=self.committed_at,
            channels=["git_history", "lexical"],
            matched_terms=matched_terms or [],
        )

        return HybridRetrievalResult(
            entity=f"Git Commit {self.short_sha}: {self.summary[:60]}",
            type="GitCommit",
            source="git_history",
            repository=self.repository_id,
            file_or_document=self.files_changed[0] if self.files_changed else self.repository_id,
            content_snippet=content_snippet,
            node_id=f"git_{self.repository_id}_{self.commit_sha}",
            confidence=0.95,
            score=round(score, 4),
            scores_breakdown={"git_affinity": score, "recency": 0.90},
            provenance=prov,
            metadata={
                "commit_sha": self.commit_sha,
                "short_sha": self.short_sha,
                "author": self.author_name,
                "committed_at": self.committed_at,
                "is_merge": self.is_merge,
                "parents": self.parent_shas,
                "files_changed": self.files_changed,
            },
        )


@dataclass
class GitCommitLink:
    """Traceable link connecting a commit to a code symbol, document, or engineering decision."""

    commit_sha: str
    repository_id: str
    target_type: str  # "code_symbol" | "document_entity" | "decision" | "file"
    target_id: str
    confidence: float = 1.0
    reason: str = ""
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        """Serialize GitCommitLink to dictionary."""
        return {
            "commit_sha": self.commit_sha,
            "repository_id": self.repository_id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "confidence": self.confidence,
            "reason": self.reason,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GitCommitLink:
        """Construct GitCommitLink from dictionary."""
        return cls(
            commit_sha=str(data["commit_sha"]),
            repository_id=str(data["repository_id"]),
            target_type=str(data["target_type"]),
            target_id=str(data["target_id"]),
            confidence=float(data.get("confidence", 1.0)),
            reason=str(data.get("reason", "")),
            created_at=str(data.get("created_at") or utc_now_iso()),
        )


# ---------------------------------------------------------------------------
# Extractor Engine
# ---------------------------------------------------------------------------

class GitMemoryExtractor:
    """Safely extracts commit history, parents, file status changes, and selective diffs."""

    def __init__(self, git_timeout_seconds: float = 15.0) -> None:
        self.timeout = git_timeout_seconds

    def is_git_repository(self, repo_path: Path | str) -> bool:
        """Check whether directory is a valid Git repository worktree."""
        path = Path(repo_path).resolve()
        if not path.exists():
            return False
        git_dir = path / ".git"
        if git_dir.exists():
            return True
        # Check via git command
        try:
            res = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(path),
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            return res.returncode == 0 and res.stdout.strip() == "true"
        except Exception:
            return False

    def extract_commits(
        self,
        repo_path: Path | str,
        repository_id: str,
        max_commits: int = 200,
        branch: str | None = None,
        include_diffs: bool = True,
    ) -> list[GitCommit]:
        """Extract commits from repository with parents, status, and selective diffs."""
        path = Path(repo_path).resolve()
        if not self.is_git_repository(path):
            logger.info("Directory %s is not a git repository. Skipping git extraction.", path)
            return []

        # Format: commit_sha, short_sha, author_name, author_email, authored_at,
        # Format spec:
        # Prepend record delimiter %x1e before each commit.
        # Delimit fields with %x1f.
        # The trailing %x1f ensures that git's --name-status output is separated into field index 10.
        format_spec = "%x1e%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%cn%x1f%ce%x1f%cI%x1f%P%x1f%B%x1f"

        cmd = [
            "git",
            "log",
            f"--max-count={max_commits}",
            f"--format={format_spec}",
            "--name-status",
            "-M",  # Detect renames
        ]
        if branch:
            cmd.append(branch)

        try:
            res = subprocess.run(
                cmd,
                cwd=str(path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
        except Exception as e:
            logger.warning("Git log command failed for %s: %s", path, e)
            return []

        if res.returncode != 0:
            logger.warning("Git log returned non-zero code %s: %s", res.returncode, res.stderr)
            return []

        raw_output = res.stdout
        if not raw_output.strip():
            return []

        commits: list[GitCommit] = []
        raw_records = raw_output.split(COMMIT_RECORD_DELIM)

        for rec in raw_records:
            rec = rec.strip()
            if not rec:
                continue

            parts = rec.split(COMMIT_FIELD_DELIM)
            if len(parts) < 10:
                continue

            c_sha = parts[0].strip()
            s_sha = parts[1].strip()
            a_name = parts[2].strip()
            a_email = parts[3].strip()
            a_date = parts[4].strip()
            cn_name = parts[5].strip()
            cn_email = parts[6].strip()
            cn_date = parts[7].strip()
            parents_raw = parts[8].strip()
            message = parts[9].strip()
            files_status_part = parts[10].strip() if len(parts) > 10 else ""

            parent_shas = [p.strip() for p in parents_raw.split() if p.strip()]
            is_merge = len(parent_shas) >= 2

            # Parse file status lines from files_status_part
            files_changed: list[str] = []
            change_summary: dict[str, str] = {}

            status_prefix_pattern = re.compile(r"^([ACDMRTUXB])\d*\s+(.+)$")

            for line in files_status_part.splitlines():
                line = line.strip()
                if not line:
                    continue
                m = status_prefix_pattern.match(line)
                if m:
                    status_code = m.group(1)
                    raw_paths = m.group(2).split("\t")
                    # Handle rename R100 old_path new_path
                    if status_code == "R" and len(raw_paths) >= 2:
                        old_p, new_p = raw_paths[0].strip(), raw_paths[1].strip()
                        files_changed.append(new_p)
                        change_summary[new_p] = f"renamed_from:{old_p}"
                    else:
                        file_p = raw_paths[-1].strip()
                        files_changed.append(file_p)
                        status_map = {
                            "A": "added",
                            "M": "modified",
                            "D": "deleted",
                            "C": "copied",
                        }
                        change_summary[file_p] = status_map.get(status_code, "modified")

            summary = message.splitlines()[0].strip() if message else ""

            # Extract selective diff if requested and files were changed
            selective_diff: dict[str, str] = {}
            if include_diffs and files_changed and not is_merge:
                selective_diff = self._extract_selective_diff(path, c_sha, files_changed[:6])

            commit_obj = GitCommit(
                commit_sha=c_sha,
                repository_id=repository_id,
                short_sha=s_sha,
                author_name=a_name,
                author_email=a_email,
                authored_at=a_date,
                committer_name=cn_name,
                committer_email=cn_email,
                committed_at=cn_date,
                message=message,
                summary=summary,
                parent_shas=parent_shas,
                is_merge=is_merge,
                branch=branch,
                files_changed=files_changed,
                change_summary=change_summary,
                selective_diff=selective_diff,
            )
            commits.append(commit_obj)

        return commits

    def _extract_selective_diff(self, repo_path: Path, commit_sha: str, files: list[str]) -> dict[str, str]:
        """Extract small, bounded diff snippets for key files in a commit."""
        diffs: dict[str, str] = {}
        total_chars = 0

        for f_path in files:
            if total_chars >= MAX_SELECTIVE_DIFF_CHARS:
                break
            try:
                cmd = ["git", "show", "--format=", "--unified=1", commit_sha, "--", f_path]
                res = subprocess.run(
                    cmd,
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5.0,
                )
                if res.returncode == 0 and res.stdout.strip():
                    snippet = res.stdout.strip()
                    # Bound to per-file max
                    if len(snippet) > MAX_FILE_DIFF_CHARS:
                        snippet = snippet[:MAX_FILE_DIFF_CHARS] + "\n... [diff truncated]"
                    diffs[f_path] = snippet
                    total_chars += len(snippet)
            except Exception:
                continue

        return diffs


# ---------------------------------------------------------------------------
# Linker Engine
# ---------------------------------------------------------------------------

class GitCommitLinker:
    """Links commits to code symbols, document entities, and engineering decisions."""

    DECISION_REF_PATTERN = re.compile(r"\b(ADR[-_]?\d+|dec[-_][a-zA-Z0-9]+)\b", re.IGNORECASE)

    def link_commits(
        self,
        commits: list[GitCommit],
        symbols: list[CodeSymbol] | None = None,
        entities: list[DocumentEntity] | None = None,
        decisions: list[EngineeringDecision] | None = None,
    ) -> list[GitCommitLink]:
        """Derive high-confidence traceable links between commits and knowledge entities."""
        links: list[GitCommitLink] = []

        # Index symbols by file path
        symbols_by_file: dict[str, list[CodeSymbol]] = {}
        if symbols:
            for s in symbols:
                f = (s.source_file or s.file or "").replace("\\", "/")
                if f:
                    symbols_by_file.setdefault(f, []).append(s)

        # Index decisions by ID and title terms
        decisions_by_id: dict[str, EngineeringDecision] = {}
        if decisions:
            for d in decisions:
                decisions_by_id[d.id] = d

        for commit in commits:
            msg_lower = commit.message.lower()

            # 1. Link to files and code symbols
            for f_path in commit.files_changed:
                norm_f = f_path.replace("\\", "/")

                # Direct file link
                links.append(
                    GitCommitLink(
                        commit_sha=commit.commit_sha,
                        repository_id=commit.repository_id,
                        target_type="file",
                        target_id=norm_f,
                        confidence=0.90,
                        reason=f"File {commit.change_summary.get(f_path, 'modified')} in commit",
                    )
                )

                # Match symbols in modified files
                matching_syms = symbols_by_file.get(norm_f, [])
                for sym in matching_syms:
                    # If symbol name explicitly mentioned in commit message -> high confidence
                    if sym.name.lower() in msg_lower:
                        links.append(
                            GitCommitLink(
                                commit_sha=commit.commit_sha,
                                repository_id=commit.repository_id,
                                target_type="code_symbol",
                                target_id=sym.id,
                                confidence=0.95,
                                reason=f"Symbol '{sym.name}' modified and mentioned in commit message",
                            )
                        )
                    else:
                        links.append(
                            GitCommitLink(
                                commit_sha=commit.commit_sha,
                                repository_id=commit.repository_id,
                                target_type="code_symbol",
                                target_id=sym.id,
                                confidence=0.75,
                                reason=f"Symbol '{sym.name}' resides in modified file '{norm_f}'",
                            )
                        )

            # 2. Link to Engineering Decisions (ADRs)
            # A: Regex match on ADR ID
            for match in self.DECISION_REF_PATTERN.findall(commit.message):
                clean_ref = match.upper().replace("_", "-")
                for d_id, dec in decisions_by_id.items():
                    if clean_ref in dec.id.upper() or clean_ref in dec.title.upper():
                        links.append(
                            GitCommitLink(
                                commit_sha=commit.commit_sha,
                                repository_id=commit.repository_id,
                                target_type="decision",
                                target_id=d_id,
                                confidence=0.95,
                                reason=f"Commit message explicitly references decision '{clean_ref}'",
                            )
                        )

            # B: Semantic/Keyword match on decision title
            for d_id, dec in decisions_by_id.items():
                title_words = [w.lower() for w in dec.title.split() if len(w) > 4]
                if title_words and sum(1 for w in title_words if w in msg_lower) >= 2:
                    links.append(
                        GitCommitLink(
                            commit_sha=commit.commit_sha,
                            repository_id=commit.repository_id,
                            target_type="decision",
                            target_id=d_id,
                            confidence=0.85,
                            reason=f"Commit message matches key concepts of decision '{dec.title[:40]}'",
                        )
                    )

        return links


# ---------------------------------------------------------------------------
# Storage & Retrieval Service
# ---------------------------------------------------------------------------

class GitMemoryService:
    """Unified service for ingesting, persisting, linking, and retrieving Git history."""

    def __init__(self, doc_store: Any | None = None) -> None:
        self.doc_store = doc_store
        self.extractor = GitMemoryExtractor()
        self.linker = GitCommitLinker()

    def ingest_repository_git(
        self,
        repo_path: Path | str,
        repository_id: str,
        *,
        symbols: list[CodeSymbol] | None = None,
        entities: list[DocumentEntity] | None = None,
        decisions: list[EngineeringDecision] | None = None,
        max_commits: int = 200,
        branch: str | None = None,
    ) -> dict[str, int]:
        """Extract commits from Git repository, derive entity links, and persist idempotently."""
        commits = self.extractor.extract_commits(
            repo_path=repo_path,
            repository_id=repository_id,
            max_commits=max_commits,
            branch=branch,
        )
        if not commits:
            return {"commits_ingested": 0, "links_derived": 0}

        links = self.linker.link_commits(
            commits=commits,
            symbols=symbols,
            entities=entities,
            decisions=decisions,
        )

        persisted_commits = 0
        persisted_links = 0

        # Save to doc_store SQLite tables if available
        if self.doc_store and hasattr(self.doc_store, "_conn"):
            with self.doc_store._conn:
                # 1. Insert/Replace commits
                for c in commits:
                    self.doc_store._conn.execute(
                        """
                        INSERT INTO git_commits (
                            commit_sha, repository_id, short_sha, author_name, author_email,
                            authored_at, committer_name, committer_email, committed_at,
                            message, summary, parent_shas, is_merge, branch, files_changed,
                            change_summary, selective_diff, metadata, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(commit_sha, repository_id) DO UPDATE SET
                            summary=excluded.summary,
                            message=excluded.message,
                            files_changed=excluded.files_changed,
                            change_summary=excluded.change_summary,
                            selective_diff=excluded.selective_diff
                        """,
                        (
                            c.commit_sha,
                            c.repository_id,
                            c.short_sha,
                            c.author_name,
                            c.author_email,
                            c.authored_at,
                            c.committer_name,
                            c.committer_email,
                            c.committed_at,
                            c.message,
                            c.summary,
                            json.dumps(c.parent_shas),
                            1 if c.is_merge else 0,
                            c.branch,
                            json.dumps(c.files_changed),
                            json.dumps(c.change_summary),
                            json.dumps(c.selective_diff),
                            json.dumps(c.metadata),
                            c.created_at,
                        ),
                    )
                    persisted_commits += 1

                # 2. Insert/Replace links
                for l in links:
                    self.doc_store._conn.execute(
                        """
                        INSERT INTO git_commit_links (
                            commit_sha, repository_id, target_type, target_id,
                            confidence, reason, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(commit_sha, repository_id, target_type, target_id) DO UPDATE SET
                            confidence=excluded.confidence,
                            reason=excluded.reason
                        """,
                        (
                            l.commit_sha,
                            l.repository_id,
                            l.target_type,
                            l.target_id,
                            l.confidence,
                            l.reason,
                            l.created_at,
                        ),
                    )
                    persisted_links += 1

        return {
            "commits_ingested": persisted_commits or len(commits),
            "links_derived": persisted_links or len(links),
        }

    def retrieve_for_aco(
        self,
        query: str,
        repository_id: str,
        limit: int = 5,
    ) -> list[HybridRetrievalResult]:
        """Retrieve historical commits relevant to an engineering query for ACO context packing."""
        if not self.doc_store or not hasattr(self.doc_store, "_conn"):
            return []

        query_terms = [t.lower() for t in query.split() if len(t) > 2]
        if not query_terms:
            return []

        cursor = self.doc_store._conn.execute(
            """
            SELECT * FROM git_commits
            WHERE repository_id = ?
            ORDER BY committed_at DESC
            LIMIT 100
            """,
            (repository_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            return []

        results: list[HybridRetrievalResult] = []

        for row in rows:
            c = self._row_to_commit(row)
            text_corpus = f"{c.summary} {c.message} {c.author_name} {' '.join(c.files_changed)}".lower()

            matches = [t for t in query_terms if t in text_corpus]
            if not matches:
                continue

            # Term overlap score
            term_score = len(matches) / len(query_terms)
            final_score = round(0.50 + 0.50 * term_score, 4)

            results.append(c.to_hybrid_result(score=final_score, matched_terms=matches))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:limit]

    def _row_to_commit(self, row: Any) -> GitCommit:
        """Convert SQLite row into GitCommit instance."""
        return GitCommit(
            commit_sha=row["commit_sha"],
            repository_id=row["repository_id"],
            short_sha=row["short_sha"],
            author_name=row["author_name"] or "",
            author_email=row["author_email"] or "",
            authored_at=row["authored_at"],
            committer_name=row["committer_name"] or "",
            committer_email=row["committer_email"] or "",
            committed_at=row["committed_at"],
            message=row["message"] or "",
            summary=row["summary"] or "",
            parent_shas=json.loads(row["parent_shas"]) if row["parent_shas"] else [],
            is_merge=bool(row["is_merge"]),
            branch=row["branch"],
            files_changed=json.loads(row["files_changed"]) if row["files_changed"] else [],
            change_summary=json.loads(row["change_summary"]) if row["change_summary"] else {},
            selective_diff=json.loads(row["selective_diff"]) if row["selective_diff"] else {},
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
            created_at=row["created_at"],
        )
