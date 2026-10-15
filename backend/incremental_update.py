"""Incremental Updates Subsystem for Adaptive Engineering Memory (Phase 8).

Provides:
1. Detection of file additions, modifications, deletions, and renames.
2. Checkpoint tracking and atomic checkpoint persistence in SQLite / Postgres.
3. Surgical extraction and graph/semantic record updates for affected files.
4. Retention of full rebuild capabilities.
5. Consistency verification comparing incremental state with a clean full rebuild.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .models import (
    CodeSymbol,
    DocumentEntity,
    GraphEdge,
    scoped_id,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

FileChangeType = Literal["added", "modified", "deleted", "renamed", "unchanged"]

IGNORED_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".graphify",
    "graphify-out",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
}

SUPPORTED_CODE_EXTENSIONS = {".py"}
SUPPORTED_DOC_EXTENSIONS = {".md"}
TRACKED_EXTENSIONS = SUPPORTED_CODE_EXTENSIONS | SUPPORTED_DOC_EXTENSIONS


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass
class FileChangeRecord:
    """Record of a single file status change detected between checkpoints."""

    path: str
    change_type: FileChangeType
    old_path: str | None = None
    sha256: str | None = None
    old_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "change_type": self.change_type,
            "old_path": self.old_path,
            "sha256": self.sha256,
            "old_sha256": self.old_sha256,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileChangeRecord:
        return cls(
            path=str(data["path"]),
            change_type=data["change_type"],
            old_path=data.get("old_path"),
            sha256=data.get("sha256"),
            old_sha256=data.get("old_sha256"),
        )


@dataclass
class RepositoryCheckpoint:
    """Snapshot checkpoint of a repository's file hashes and mapped entity IDs."""

    id: str
    repository_id: str
    user_id: str
    commit_sha: str | None = None
    timestamp: str = field(default_factory=utc_now_iso)
    file_hashes: dict[str, str] = field(default_factory=dict)             # rel_path -> sha256
    symbols_by_file: dict[str, list[str]] = field(default_factory=dict)   # rel_path -> list of symbol_ids
    entities_by_file: dict[str, list[str]] = field(default_factory=dict)  # rel_path -> list of entity_ids
    edges_by_file: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    symbol_count: int = 0
    entity_count: int = 0
    edge_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repository_id": self.repository_id,
            "user_id": self.user_id,
            "commit_sha": self.commit_sha,
            "timestamp": self.timestamp,
            "file_hashes": dict(self.file_hashes),
            "symbols_by_file": {k: list(v) for k, v in self.symbols_by_file.items()},
            "entities_by_file": {k: list(v) for k, v in self.entities_by_file.items()},
            "edges_by_file": {k: list(v) for k, v in self.edges_by_file.items()},
            "symbol_count": self.symbol_count,
            "entity_count": self.entity_count,
            "edge_count": self.edge_count,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RepositoryCheckpoint:
        return cls(
            id=str(data["id"]),
            repository_id=str(data["repository_id"]),
            user_id=str(data["user_id"]),
            commit_sha=data.get("commit_sha"),
            timestamp=str(data.get("timestamp") or utc_now_iso()),
            file_hashes=dict(data.get("file_hashes", {})),
            symbols_by_file=dict(data.get("symbols_by_file", {})),
            entities_by_file=dict(data.get("entities_by_file", {})),
            edges_by_file=dict(data.get("edges_by_file", {})),
            symbol_count=int(data.get("symbol_count", 0)),
            entity_count=int(data.get("entity_count", 0)),
            edge_count=int(data.get("edge_count", 0)),
            metadata=dict(data.get("metadata", {})),
            created_at=str(data.get("created_at") or utc_now_iso()),
        )


@dataclass
class IncrementalUpdateResult:
    """Execution summary of an incremental or full rebuild job."""

    repository_id: str
    checkpoint_id: str
    previous_checkpoint_id: str | None
    changes_detected: list[FileChangeRecord] = field(default_factory=list)
    files_added: int = 0
    files_modified: int = 0
    files_deleted: int = 0
    files_renamed: int = 0
    symbols_added: int = 0
    symbols_updated: int = 0
    symbols_removed: int = 0
    entities_added: int = 0
    entities_updated: int = 0
    entities_removed: int = 0
    edges_added: int = 0
    edges_removed: int = 0
    duration_ms: float = 0.0
    is_full_rebuild: bool = False
    is_clean_agreement: bool | None = None

    @property
    def has_changes(self) -> bool:
        return bool(self.changes_detected) or self.is_full_rebuild

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "checkpoint_id": self.checkpoint_id,
            "previous_checkpoint_id": self.previous_checkpoint_id,
            "changes_detected": [c.to_dict() for c in self.changes_detected],
            "files_added": self.files_added,
            "files_modified": self.files_modified,
            "files_deleted": self.files_deleted,
            "files_renamed": self.files_renamed,
            "symbols_added": self.symbols_added,
            "symbols_updated": self.symbols_updated,
            "symbols_removed": self.symbols_removed,
            "entities_added": self.entities_added,
            "entities_updated": self.entities_updated,
            "entities_removed": self.entities_removed,
            "edges_added": self.edges_added,
            "edges_removed": self.edges_removed,
            "duration_ms": self.duration_ms,
            "is_full_rebuild": self.is_full_rebuild,
            "is_clean_agreement": self.is_clean_agreement,
        }


# ---------------------------------------------------------------------------
# Checkpoint & Change Detector
# ---------------------------------------------------------------------------

class IncrementalChangeDetector:
    """Detects file modifications, additions, deletions, and renames across repository runs."""

    @staticmethod
    def compute_file_sha256(path: Path) -> str:
        """Compute SHA-256 hash of file contents."""
        hasher = hashlib.sha256()
        with path.open("rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()

    def scan_repository_hashes(self, repo_path: Path | str) -> dict[str, str]:
        """Scan repository worktree and compute SHA-256 for all supported files."""
        root = Path(repo_path).resolve()
        hashes: dict[str, str] = {}

        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if any(part in IGNORED_DIRECTORIES for part in p.parts):
                continue
            if p.suffix.lower() not in TRACKED_EXTENSIONS:
                continue

            rel_path = str(p.relative_to(root)).replace("\\", "/")
            try:
                hashes[rel_path] = self.compute_file_sha256(p)
            except (OSError, PermissionError) as e:
                logger.warning("Failed to read %s for hashing: %s", rel_path, e)

        return hashes

    def detect_changes(
        self,
        current_hashes: dict[str, str],
        previous_hashes: dict[str, str] | None,
    ) -> list[FileChangeRecord]:
        """Compare current file hashes against previous checkpoint to classify changes."""
        if previous_hashes is None:
            # Baseline / initial: all current files are added
            return [
                FileChangeRecord(path=path, change_type="added", sha256=sha)
                for path, sha in sorted(current_hashes.items())
            ]

        changes: list[FileChangeRecord] = []
        old_paths_by_sha = {sha: path for path, sha in previous_hashes.items()}
        current_paths_by_sha = {sha: path for path, sha in current_hashes.items()}

        unmatched_added_paths: dict[str, str] = {}  # path -> sha
        unmatched_deleted_paths: dict[str, str] = {}  # path -> sha

        # 1. Identify additions and modifications
        for path, cur_sha in current_hashes.items():
            if path in previous_hashes:
                old_sha = previous_hashes[path]
                if cur_sha != old_sha:
                    changes.append(
                        FileChangeRecord(
                            path=path,
                            change_type="modified",
                            sha256=cur_sha,
                            old_sha256=old_sha,
                        )
                    )
            else:
                unmatched_added_paths[path] = cur_sha

        # 2. Identify deletions
        for path, old_sha in previous_hashes.items():
            if path not in current_hashes:
                unmatched_deleted_paths[path] = old_sha

        # 3. Detect renames (matching SHA between deleted path and added path)
        renamed_old: set[str] = set()
        renamed_new: set[str] = set()

        for new_p, new_sha in unmatched_added_paths.items():
            if new_sha in unmatched_deleted_paths.values():
                # Find matching old path with same hash
                for old_p, old_sha in unmatched_deleted_paths.items():
                    if old_sha == new_sha and old_p not in renamed_old:
                        changes.append(
                            FileChangeRecord(
                                path=new_p,
                                change_type="renamed",
                                old_path=old_p,
                                sha256=new_sha,
                                old_sha256=old_sha,
                            )
                        )
                        renamed_old.add(old_p)
                        renamed_new.add(new_p)
                        break

        # Remaining additions
        for new_p, new_sha in unmatched_added_paths.items():
            if new_p not in renamed_new:
                changes.append(
                    FileChangeRecord(path=new_p, change_type="added", sha256=new_sha)
                )

        # Remaining deletions
        for old_p, old_sha in unmatched_deleted_paths.items():
            if old_p not in renamed_old:
                changes.append(
                    FileChangeRecord(path=old_p, change_type="deleted", old_sha256=old_sha)
                )

        changes.sort(key=lambda c: (c.change_type, c.path))
        return changes


# ---------------------------------------------------------------------------
# Surgical Single File Extractor
# ---------------------------------------------------------------------------

class SingleFileExtractor:
    """Extracts symbols, entities, and edges from individual code and doc files."""

    def extract_file(
        self,
        full_path: Path,
        rel_path: str,
        *,
        user_id: str,
        repository_id: str,
    ) -> tuple[list[CodeSymbol], list[DocumentEntity], list[GraphEdge]]:
        """Extract symbols/entities and local edges for a single file."""
        if not full_path.is_file():
            return [], [], []

        suffix = full_path.suffix.lower()
        if suffix == ".py":
            return self._extract_python(full_path, rel_path, user_id, repository_id)
        elif suffix == ".md":
            return self._extract_markdown(full_path, rel_path, user_id, repository_id)
        return [], [], []

    def _extract_python(
        self,
        full_path: Path,
        rel_path: str,
        user_id: str,
        repository_id: str,
    ) -> tuple[list[CodeSymbol], list[DocumentEntity], list[GraphEdge]]:
        """Parse Python AST to extract classes, functions, methods, and relationships."""
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(content, filename=str(full_path))
        except Exception as e:
            logger.warning("Could not parse Python AST for %s: %s", rel_path, e)
            return [], [], []

        symbols: list[CodeSymbol] = []
        edges: list[GraphEdge] = []
        norm_path = rel_path.replace("\\", "/")
        module_name = norm_path.replace("/", ".").rstrip(".py")

        # Track top-level definitions and classes
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                cls_id = scoped_id(user_id, repository_id, f"{norm_path}::{node.name}")
                doc = ast.get_docstring(node)
                cls_sym = CodeSymbol(
                    id=cls_id,
                    name=node.name,
                    file=norm_path,
                    type="class",
                    user_id=user_id,
                    repository_id=repository_id,
                    qualified_name=f"{module_name}.{node.name}",
                    module=module_name,
                    source_file=norm_path,
                    line_start=node.lineno,
                    line_end=getattr(node, "end_lineno", node.lineno),
                    documentation=doc,
                    language="python",
                )
                symbols.append(cls_sym)

                # Inheritance edges
                for base in node.bases:
                    base_name = getattr(base, "id", None) or getattr(base, "attr", None)
                    if base_name:
                        edges.append(
                            GraphEdge(
                                source_id=cls_id,
                                target_id=scoped_id(user_id, repository_id, f"{norm_path}::{base_name}"),
                                relation="INHERITS",
                                metadata={"base": base_name},
                            )
                        )

                # Methods inside class
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method_id = scoped_id(user_id, repository_id, f"{norm_path}::{node.name}.{item.name}")
                        m_doc = ast.get_docstring(item)
                        args = [a.arg for a in item.args.args]
                        sig = f"def {item.name}({', '.join(args)})"

                        method_sym = CodeSymbol(
                            id=method_id,
                            name=item.name,
                            file=norm_path,
                            type="method",
                            user_id=user_id,
                            repository_id=repository_id,
                            qualified_name=f"{module_name}.{node.name}.{item.name}",
                            module=module_name,
                            source_file=norm_path,
                            line_start=item.lineno,
                            line_end=getattr(item, "end_lineno", item.lineno),
                            parent_symbol=node.name,
                            signature=sig,
                            documentation=m_doc,
                            language="python",
                        )
                        symbols.append(method_sym)

                        # Class DEFINES method
                        edges.append(
                            GraphEdge(
                                source_id=cls_id,
                                target_id=method_id,
                                relation="DEFINES",
                            )
                        )

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_id = scoped_id(user_id, repository_id, f"{norm_path}::{node.name}")
                f_doc = ast.get_docstring(node)
                args = [a.arg for a in node.args.args]
                sig = f"def {node.name}({', '.join(args)})"

                func_sym = CodeSymbol(
                    id=func_id,
                    name=node.name,
                    file=norm_path,
                    type="function",
                    user_id=user_id,
                    repository_id=repository_id,
                    qualified_name=f"{module_name}.{node.name}",
                    module=module_name,
                    source_file=norm_path,
                    line_start=node.lineno,
                    line_end=getattr(node, "end_lineno", node.lineno),
                    signature=sig,
                    documentation=f_doc,
                    language="python",
                )
                symbols.append(func_sym)

            elif isinstance(node, ast.Import):
                for alias in node.names:
                    edges.append(
                        GraphEdge(
                            source_id=scoped_id(user_id, repository_id, norm_path),
                            target_id=alias.name,
                            relation="IMPORTS",
                        )
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    edges.append(
                        GraphEdge(
                            source_id=scoped_id(user_id, repository_id, norm_path),
                            target_id=node.module,
                            relation="IMPORTS",
                        )
                    )

        return symbols, [], edges

    def _extract_markdown(
        self,
        full_path: Path,
        rel_path: str,
        user_id: str,
        repository_id: str,
    ) -> tuple[list[CodeSymbol], list[DocumentEntity], list[GraphEdge]]:
        """Extract sections, headings, and documented concepts from a Markdown file."""
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning("Could not read Markdown for %s: %s", rel_path, e)
            return [], [], []

        entities: list[DocumentEntity] = []
        edges: list[GraphEdge] = []
        norm_path = rel_path.replace("\\", "/")
        doc_name = full_path.name

        current_heading = doc_name
        heading_lines: list[str] = []

        for line in content.splitlines():
            line_s = line.strip()
            if line_s.startswith("#"):
                # New heading
                parts = line_s.lstrip("#").strip().split(":", 1)
                h_name = parts[0].strip()
                if h_name:
                    ent_id = scoped_id(user_id, repository_id, f"{norm_path}#{h_name}")
                    desc = parts[1].strip() if len(parts) > 1 else f"Documentation section '{h_name}'"
                    entities.append(
                        DocumentEntity(
                            id=ent_id,
                            name=h_name,
                            type="Concept",
                            user_id=user_id,
                            repository_id=repository_id,
                            description=desc,
                            source_document=doc_name,
                            document_path=norm_path,
                            section=h_name,
                            source_file=norm_path,
                        )
                    )
                    current_heading = h_name

        # If no headings found, create a single DocumentEntity representing the document
        if not entities:
            ent_id = scoped_id(user_id, repository_id, norm_path)
            entities.append(
                DocumentEntity(
                    id=ent_id,
                    name=doc_name,
                    type="Document",
                    user_id=user_id,
                    repository_id=repository_id,
                    description=f"Documentation document at {norm_path}",
                    source_document=doc_name,
                    document_path=norm_path,
                    source_file=norm_path,
                )
            )

        return [], entities, edges


# ---------------------------------------------------------------------------
# Incremental Update Service
# ---------------------------------------------------------------------------

class IncrementalUpdateService:
    """Coordinates change detection, surgical updates, checkpoints, and consistency checks."""

    def __init__(
        self,
        doc_store: Any | None = None,
        extractor: SingleFileExtractor | None = None,
    ) -> None:
        self.doc_store = doc_store
        self.detector = IncrementalChangeDetector()
        self.extractor = extractor or SingleFileExtractor()
        # In-memory checkpoint fallback when doc_store is not provided
        self._checkpoints: dict[str, list[RepositoryCheckpoint]] = {}

    def get_latest_checkpoint(self, repository_id: str) -> RepositoryCheckpoint | None:
        """Fetch the most recent checkpoint for repository."""
        if self.doc_store and hasattr(self.doc_store, "get_latest_checkpoint"):
            return self.doc_store.get_latest_checkpoint(repository_id)
        cps = self._checkpoints.get(repository_id, [])
        return cps[-1] if cps else None

    def save_checkpoint(self, checkpoint: RepositoryCheckpoint) -> RepositoryCheckpoint:
        """Persist checkpoint to store or in-memory tracking."""
        if self.doc_store and hasattr(self.doc_store, "save_checkpoint"):
            self.doc_store.save_checkpoint(checkpoint)
        self._checkpoints.setdefault(checkpoint.repository_id, []).append(checkpoint)
        return checkpoint

    def run_update(
        self,
        repository_path: Path | str,
        *,
        user_id: str,
        repository_id: str,
        commit_sha: str | None = None,
        force_full_rebuild: bool = False,
        semantic_index: Any | None = None,
        graph_store: Any | None = None,
    ) -> IncrementalUpdateResult:
        """Execute incremental update or full rebuild based on checkpoint diff."""
        start_time = time.perf_counter()
        root = Path(repository_path).resolve()
        if not root.is_dir():
            raise NotADirectoryError(f"Target directory does not exist: {root}")

        current_hashes = self.detector.scan_repository_hashes(root)
        prev_cp = None if force_full_rebuild else self.get_latest_checkpoint(repository_id)

        # 1. Determine if this is full rebuild or incremental
        is_full_rebuild = force_full_rebuild or (prev_cp is None)
        changes = self.detector.detect_changes(
            current_hashes=current_hashes,
            previous_hashes=prev_cp.file_hashes if prev_cp else None,
        )

        # Idempotency check: if no changes detected on incremental run, return no-op result
        if not is_full_rebuild and not changes:
            return IncrementalUpdateResult(
                repository_id=repository_id,
                checkpoint_id=prev_cp.id if prev_cp else "cp_initial",
                previous_checkpoint_id=prev_cp.id if prev_cp else None,
                changes_detected=[],
                duration_ms=round((time.perf_counter() - start_time) * 1000, 2),
                is_full_rebuild=False,
            )

        # State tracking for new checkpoint
        symbols_by_file: dict[str, list[str]] = dict(prev_cp.symbols_by_file) if prev_cp and not is_full_rebuild else {}
        entities_by_file: dict[str, list[str]] = dict(prev_cp.entities_by_file) if prev_cp and not is_full_rebuild else {}
        edges_by_file: dict[str, list[dict[str, Any]]] = dict(prev_cp.edges_by_file) if prev_cp and not is_full_rebuild else {}

        symbols_added = 0
        symbols_updated = 0
        symbols_removed = 0
        entities_added = 0
        entities_updated = 0
        entities_removed = 0
        edges_added = 0
        edges_removed = 0

        files_added = 0
        files_modified = 0
        files_deleted = 0
        files_renamed = 0

        # All newly extracted symbols and entities to pass to semantic index
        newly_extracted_symbols: list[CodeSymbol] = []
        newly_extracted_entities: list[DocumentEntity] = []

        # 2. Process changes
        for change in changes:
            c_path = change.path
            c_type = change.change_type

            if c_type == "added":
                files_added += 1
                syms, ents, eds = self.extractor.extract_file(
                    root / c_path, c_path, user_id=user_id, repository_id=repository_id
                )
                symbols_by_file[c_path] = [s.id for s in syms]
                entities_by_file[c_path] = [e.id for e in ents]
                edges_by_file[c_path] = [e.to_dict() for e in eds]

                symbols_added += len(syms)
                entities_added += len(ents)
                edges_added += len(eds)

                newly_extracted_symbols.extend(syms)
                newly_extracted_entities.extend(ents)

            elif c_type == "modified":
                files_modified += 1
                # Invalidate stale records for this file
                old_sym_ids = symbols_by_file.pop(c_path, [])
                old_ent_ids = entities_by_file.pop(c_path, [])
                old_edges = edges_by_file.pop(c_path, [])

                symbols_removed += len(old_sym_ids)
                entities_removed += len(old_ent_ids)
                edges_removed += len(old_edges)

                if semantic_index:
                    if hasattr(semantic_index, "remove_symbols_by_file"):
                        semantic_index.remove_symbols_by_file(c_path)
                    if hasattr(semantic_index, "remove_entities_by_path"):
                        semantic_index.remove_entities_by_path(c_path)

                if graph_store:
                    if hasattr(graph_store, "delete_code_symbols_by_file"):
                        graph_store.delete_code_symbols_by_file(repository_id, c_path)
                    if hasattr(graph_store, "delete_document_entities_by_path"):
                        graph_store.delete_document_entities_by_path(repository_id, c_path)

                # Re-extract
                syms, ents, eds = self.extractor.extract_file(
                    root / c_path, c_path, user_id=user_id, repository_id=repository_id
                )
                symbols_by_file[c_path] = [s.id for s in syms]
                entities_by_file[c_path] = [e.id for e in ents]
                edges_by_file[c_path] = [e.to_dict() for e in eds]

                symbols_updated += len(syms)
                entities_updated += len(ents)
                edges_added += len(eds)

                newly_extracted_symbols.extend(syms)
                newly_extracted_entities.extend(ents)

            elif c_type == "deleted":
                files_deleted += 1
                old_sym_ids = symbols_by_file.pop(c_path, [])
                old_ent_ids = entities_by_file.pop(c_path, [])
                old_edges = edges_by_file.pop(c_path, [])

                symbols_removed += len(old_sym_ids)
                entities_removed += len(old_ent_ids)
                edges_removed += len(old_edges)

                if semantic_index:
                    if hasattr(semantic_index, "remove_symbols_by_file"):
                        semantic_index.remove_symbols_by_file(c_path)
                    if hasattr(semantic_index, "remove_entities_by_path"):
                        semantic_index.remove_entities_by_path(c_path)

                if graph_store:
                    if hasattr(graph_store, "delete_code_symbols_by_file"):
                        graph_store.delete_code_symbols_by_file(repository_id, c_path)
                    if hasattr(graph_store, "delete_document_entities_by_path"):
                        graph_store.delete_document_entities_by_path(repository_id, c_path)

            elif c_type == "renamed":
                files_renamed += 1
                # Purge under old path
                old_p = change.old_path or ""
                old_sym_ids = symbols_by_file.pop(old_p, [])
                old_ent_ids = entities_by_file.pop(old_p, [])
                old_edges = edges_by_file.pop(old_p, [])

                symbols_removed += len(old_sym_ids)
                entities_removed += len(old_ent_ids)
                edges_removed += len(old_edges)

                if semantic_index:
                    if hasattr(semantic_index, "remove_symbols_by_file") and old_p:
                        semantic_index.remove_symbols_by_file(old_p)
                    if hasattr(semantic_index, "remove_entities_by_path") and old_p:
                        semantic_index.remove_entities_by_path(old_p)

                # Re-extract under new path
                syms, ents, eds = self.extractor.extract_file(
                    root / c_path, c_path, user_id=user_id, repository_id=repository_id
                )
                symbols_by_file[c_path] = [s.id for s in syms]
                entities_by_file[c_path] = [e.id for e in ents]
                edges_by_file[c_path] = [e.to_dict() for e in eds]

                symbols_added += len(syms)
                entities_added += len(ents)
                edges_added += len(eds)

                newly_extracted_symbols.extend(syms)
                newly_extracted_entities.extend(ents)

        # 3. Synchronize semantic index with newly extracted items
        if semantic_index:
            if newly_extracted_symbols and hasattr(semantic_index, "upsert_symbols"):
                semantic_index.upsert_symbols(newly_extracted_symbols)
            if newly_extracted_entities and hasattr(semantic_index, "upsert_entities"):
                semantic_index.upsert_entities(newly_extracted_entities)

        # 4. Synchronize graph store with newly extracted items
        if graph_store:
            if newly_extracted_symbols and hasattr(graph_store, "upsert_code_symbols"):
                graph_store.upsert_code_symbols(newly_extracted_symbols)
            if newly_extracted_entities and hasattr(graph_store, "upsert_document_entities"):
                graph_store.upsert_document_entities(newly_extracted_entities)

        # 5. Build and save new checkpoint
        total_syms = sum(len(v) for v in symbols_by_file.values())
        total_ents = sum(len(v) for v in entities_by_file.values())
        total_eds = sum(len(v) for v in edges_by_file.values())

        new_cp_id = f"cp_{repository_id}_{int(time.time() * 1000)}"
        new_cp = RepositoryCheckpoint(
            id=new_cp_id,
            repository_id=repository_id,
            user_id=user_id,
            commit_sha=commit_sha,
            file_hashes=current_hashes,
            symbols_by_file=symbols_by_file,
            entities_by_file=entities_by_file,
            edges_by_file=edges_by_file,
            symbol_count=total_syms,
            entity_count=total_ents,
            edge_count=total_eds,
            metadata={"is_full_rebuild": is_full_rebuild},
        )
        self.save_checkpoint(new_cp)

        # Invalidate cached retrieval results for this repository upon update
        try:
            from .cache import default_cache_manager
            default_cache_manager.invalidate_repository(repository_id)
        except Exception:
            pass

        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

        return IncrementalUpdateResult(
            repository_id=repository_id,
            checkpoint_id=new_cp_id,
            previous_checkpoint_id=prev_cp.id if prev_cp else None,
            changes_detected=changes,
            files_added=files_added,
            files_modified=files_modified,
            files_deleted=files_deleted,
            files_renamed=files_renamed,
            symbols_added=symbols_added,
            symbols_updated=symbols_updated,
            symbols_removed=symbols_removed,
            entities_added=entities_added,
            entities_updated=entities_updated,
            entities_removed=entities_removed,
            edges_added=edges_added,
            edges_removed=edges_removed,
            duration_ms=elapsed_ms,
            is_full_rebuild=is_full_rebuild,
        )

    def verify_incremental_consistency(
        self,
        repository_path: Path | str,
        *,
        user_id: str,
        repository_id: str,
        current_checkpoint: RepositoryCheckpoint,
    ) -> dict[str, Any]:
        """Verify that incremental state strictly agrees with a fresh full rebuild on same files."""
        root = Path(repository_path).resolve()
        fresh_hashes = self.detector.scan_repository_hashes(root)

        # Full extraction from disk
        fresh_symbols_by_file: dict[str, list[str]] = {}
        fresh_entities_by_file: dict[str, list[str]] = {}

        for rel_path in sorted(fresh_hashes.keys()):
            syms, ents, _ = self.extractor.extract_file(
                root / rel_path, rel_path, user_id=user_id, repository_id=repository_id
            )
            if syms:
                fresh_symbols_by_file[rel_path] = [s.id for s in syms]
            if ents:
                fresh_entities_by_file[rel_path] = [e.id for e in ents]

        discrepancies: list[str] = []

        # 1. Compare file hashes
        if fresh_hashes != current_checkpoint.file_hashes:
            discrepancies.append(
                f"File hashes mismatch: {len(fresh_hashes)} current vs {len(current_checkpoint.file_hashes)} in checkpoint"
            )

        # 2. Compare symbols by file
        current_sym_ids = set()
        for s_list in current_checkpoint.symbols_by_file.values():
            current_sym_ids.update(s_list)

        fresh_sym_ids = set()
        for s_list in fresh_symbols_by_file.values():
            fresh_sym_ids.update(s_list)

        if current_sym_ids != fresh_sym_ids:
            missing_in_current = fresh_sym_ids - current_sym_ids
            excess_in_current = current_sym_ids - fresh_sym_ids
            discrepancies.append(
                f"Symbol ID mismatch: missing={len(missing_in_current)}, excess={len(excess_in_current)}"
            )

        # 3. Compare entities by file
        current_ent_ids = set()
        for e_list in current_checkpoint.entities_by_file.values():
            current_ent_ids.update(e_list)

        fresh_ent_ids = set()
        for e_list in fresh_entities_by_file.values():
            fresh_ent_ids.update(e_list)

        if current_ent_ids != fresh_ent_ids:
            missing_ents = fresh_ent_ids - current_ent_ids
            excess_ents = current_ent_ids - fresh_ent_ids
            discrepancies.append(
                f"Entity ID mismatch: missing={len(missing_ents)}, excess={len(excess_ents)}"
            )

        agrees = len(discrepancies) == 0
        return {
            "agrees": agrees,
            "repository_id": repository_id,
            "discrepancies": discrepancies,
            "incremental_symbols": len(current_sym_ids),
            "clean_rebuild_symbols": len(fresh_sym_ids),
            "incremental_entities": len(current_ent_ids),
            "clean_rebuild_entities": len(fresh_ent_ids),
        }
