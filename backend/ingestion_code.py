"""Run Graphify and normalize its AST graph into code symbols and edges."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .models import CodeSymbol, GraphEdge, scoped_id

CODE_RELATIONS = {"CALLS": "CALLS", "IMPORTS": "IMPORTS", "INHERITS": "INHERITS", "EXTENDS": "INHERITS"}


def _graph_path(repository_path: Path) -> Path:
    candidates = (
        repository_path / ".graphify" / "graph.json",
        repository_path / "graphify-out" / "graph.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    expected = ", ".join(str(path) for path in candidates)
    raise FileNotFoundError(f"Graphify did not produce graph.json; checked: {expected}")


def _edge_endpoint(edge: dict[str, Any], primary: str, alternate: str) -> str | None:
    value = edge.get(primary, edge.get(alternate))
    if isinstance(value, dict):
        value = value.get("id")
    return str(value) if value is not None else None


def parse_graphify_graph(
    graph_path: str | Path,
    *,
    user_id: str | None = None,
    repository_id: str | None = None,
) -> tuple[list[CodeSymbol], list[GraphEdge]]:
    """Parse Graphify node-link JSON, accepting either ``edges`` or ``links``."""
    with Path(graph_path).open("r", encoding="utf-8") as graph_file:
        graph = json.load(graph_file)
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
        raise ValueError("Graphify JSON must contain a 'nodes' list")

    symbols: list[CodeSymbol] = []
    source_to_scoped: dict[str, str] = {}
    for node in graph["nodes"]:
        if not isinstance(node, dict) or node.get("id") is None:
            continue
        if str(node.get("file_type", "code")).lower() != "code":
            continue
        source_id = str(node["id"])
        name = node.get("name") or node.get("label") or node.get("title")
        if not name:
            continue
        symbol_id = (
            scoped_id(user_id, repository_id or "default", source_id)
            if user_id is not None
            else source_id
        )
        source_to_scoped[source_id] = symbol_id
        symbol_type = (
            node.get("symbol_type")
            or node.get("kind")
            or node.get("type")
            or node.get("file_type")
            or "symbol"
        )
        symbols.append(
            CodeSymbol(
                id=symbol_id,
                name=str(name),
                file=str(node.get("source_file") or node.get("file") or ""),
                type=str(symbol_type),
                user_id=user_id,
                repository_id=repository_id,
            )
        )

    raw_edges = graph.get("edges", graph.get("links", []))
    if not isinstance(raw_edges, list):
        raise ValueError("Graphify JSON 'edges'/'links' must be a list")
    edges: list[GraphEdge] = []
    for edge in raw_edges:
        if not isinstance(edge, dict):
            continue
        source = _edge_endpoint(edge, "source", "from")
        target = _edge_endpoint(edge, "target", "to")
        relation = str(edge.get("relation") or edge.get("type") or "").upper().replace(" ", "_")
        normalized_relation = CODE_RELATIONS.get(relation)
        if source not in source_to_scoped or target not in source_to_scoped or not normalized_relation:
            continue
        edges.append(
            GraphEdge(
                source_id=source_to_scoped[source],
                target_id=source_to_scoped[target],
                relation=normalized_relation,
                metadata={key: value for key, value in edge.items() if key not in {"source", "target", "from", "to", "relation", "type"}},
            )
        )
    return symbols, edges


def ingest_code(
    repository_path: str | Path,
    *,
    user_id: str | None = None,
    repository_id: str | None = None,
    graphify_executable: str | None = None,
) -> tuple[list[CodeSymbol], list[GraphEdge]]:
    """Run ``graphify update`` for a repository and return its AST graph."""
    root = Path(repository_path).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Repository directory does not exist: {root}")
    executable = graphify_executable or shutil.which("graphify")
    command = [executable, "update", str(root), "--no-cluster"] if executable else [
        sys.executable,
        "-m",
        "graphify",
        "update",
        str(root),
        "--no-cluster",
    ]
    environment = os.environ.copy()
    subprocess.run(command, cwd=root, env=environment, check=True, capture_output=True, text=True)
    return parse_graphify_graph(
        _graph_path(root), user_id=user_id, repository_id=repository_id
    )