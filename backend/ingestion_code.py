"""Run Graphify and normalize its AST graph into code symbols and edges."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .models import CodeSymbol, GraphEdge, scoped_id

CODE_RELATIONS = {
    "CALLS": "CALLS",
    "IMPORTS": "IMPORTS",
    "INHERITS": "INHERITS",
    "EXTENDS": "INHERITS",
    "DEFINES": "DEFINES",
    "CONTAINS": "DEFINES",
    "DECLARES": "DEFINES",
}

EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".sh": "shell",
    ".bash": "shell",
    ".sql": "sql",
    ".lua": "lua",
    ".dart": "dart",
    ".zig": "zig",
}


def _parse_line_range(node: dict[str, Any]) -> tuple[int | None, int | None]:
    """Extract starting and ending line numbers from various Graphify node representations."""
    for start_key in ("line_start", "lineno", "line", "start_line"):
        val = node.get(start_key)
        if val is not None:
            try:
                start_int = int(val)
                end_val = node.get("line_end") or node.get("end_line")
                end_int = int(end_val) if end_val is not None else start_int
                return start_int, end_int
            except (ValueError, TypeError):
                pass

    for loc_key in ("source_location", "location", "range"):
        loc = node.get(loc_key)
        if loc is not None:
            nums = re.findall(r"\d+", str(loc))
            if len(nums) >= 2:
                try:
                    return int(nums[0]), int(nums[1])
                except (ValueError, TypeError):
                    pass
            elif len(nums) == 1:
                try:
                    return int(nums[0]), int(nums[0])
                except (ValueError, TypeError):
                    pass
    return None, None


def _infer_language(node: dict[str, Any], file_path: str) -> str | None:
    """Detect programming language from node attributes or source file extension."""
    lang = node.get("language") or node.get("lang")
    if lang:
        return str(lang).strip().lower()
    if file_path:
        ext = Path(file_path).suffix.lower()
        return EXTENSION_TO_LANGUAGE.get(ext)
    return None


def _infer_module_and_package(
    file_path: str,
    explicit_module: str | None = None,
    explicit_package: str | None = None,
) -> tuple[str | None, str | None]:
    """Infer hierarchical module and package names from file path or explicit fields."""
    module = str(explicit_module).strip() if explicit_module else None
    package = str(explicit_package).strip() if explicit_package else None

    if file_path and file_path != ".":
        cleaned = file_path.replace("\\", "/").strip().lstrip("./")
        parts = [p for p in cleaned.split("/") if p and p != "."]
        if parts:
            stem = Path(parts[-1]).stem
            if stem and stem != ".":
                inferred_parts = parts[:-1] + [stem]
                if not module:
                    module = ".".join(inferred_parts)
                if not package and len(parts) > 1:
                    package = ".".join(parts[:-1])

    return module, package


def _parse_signature(node: dict[str, Any]) -> str | None:
    """Extract signature string from signature, sig, or params fields."""
    sig = node.get("signature") or node.get("sig")
    if sig:
        return str(sig).strip()
    params = node.get("params") or node.get("parameters")
    if params is not None:
        if isinstance(params, list):
            formatted_params = []
            for p in params:
                if isinstance(p, dict):
                    p_name = p.get("name", "")
                    p_type = p.get("type") or p.get("annotation")
                    if p_name and p_type:
                        formatted_params.append(f"{p_name}: {p_type}")
                    elif p_name:
                        formatted_params.append(str(p_name))
                else:
                    formatted_params.append(str(p))
            ret_type = node.get("return_type") or node.get("returns")
            sig_str = f"({', '.join(formatted_params)})"
            if ret_type:
                sig_str += f" -> {ret_type}"
            return sig_str
        elif isinstance(params, str):
            return params.strip()
    return None


def _parse_documentation(node: dict[str, Any]) -> str | None:
    """Extract docstrings, comments, or documentation notes."""
    for key in ("documentation", "docstring", "doc", "comments", "comment", "description"):
        val = node.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


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
    """Parse Graphify node-link JSON, normalizing AST metadata into rich CodeSymbols."""
    with Path(graph_path).open("r", encoding="utf-8") as graph_file:
        graph = json.load(graph_file)
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
        raise ValueError("Graphify JSON must contain a 'nodes' list")

    raw_nodes: dict[str, dict[str, Any]] = {}
    source_to_scoped: dict[str, str] = {}
    rationale_texts: dict[str, str] = {}

    for node in graph["nodes"]:
        if not isinstance(node, dict) or node.get("id") is None:
            continue
        file_type = str(node.get("file_type", "code")).lower()
        node_id = str(node["id"])

        if file_type == "rationale":
            r_text = node.get("label") or node.get("name") or node.get("title") or node.get("text")
            if r_text:
                rationale_texts[node_id] = str(r_text).strip()
            continue

        if file_type != "code":
            continue

        name = node.get("name") or node.get("label") or node.get("title")
        if not name:
            continue

        symbol_id = (
            scoped_id(user_id, repository_id or "default", node_id)
            if user_id is not None
            else node_id
        )
        source_to_scoped[node_id] = symbol_id

        symbol_type = (
            node.get("symbol_type")
            or node.get("kind")
            or node.get("type")
            or node.get("file_type")
            or "symbol"
        )
        file_path = str(node.get("source_file") or node.get("file") or "")
        line_start, line_end = _parse_line_range(node)
        language = _infer_language(node, file_path)
        module, package = _infer_module_and_package(
            file_path,
            explicit_module=node.get("module"),
            explicit_package=node.get("package"),
        )
        parent_symbol = (
            node.get("parent_symbol")
            or node.get("parent")
            or node.get("class_name")
            or node.get("scope")
            or node.get("container")
        )
        signature = _parse_signature(node)
        documentation = _parse_documentation(node)
        explicit_qname = node.get("qualified_name") or node.get("qname")

        raw_nodes[node_id] = {
            "id": symbol_id,
            "name": str(name),
            "file": file_path,
            "type": str(symbol_type),
            "user_id": user_id,
            "repository_id": repository_id,
            "explicit_qname": str(explicit_qname) if explicit_qname else None,
            "module": module,
            "package": package,
            "source_file": file_path,
            "line_start": line_start,
            "line_end": line_end,
            "parent_symbol": str(parent_symbol) if parent_symbol else None,
            "signature": signature,
            "documentation": documentation,
            "language": language,
        }

    raw_edges = graph.get("edges", graph.get("links", []))
    if not isinstance(raw_edges, list):
        raise ValueError("Graphify JSON 'edges'/'links' must be a list")

    edges: list[GraphEdge] = []
    for edge in raw_edges:
        if not isinstance(edge, dict):
            continue
        source = _edge_endpoint(edge, "source", "from")
        target = _edge_endpoint(edge, "target", "to")
        raw_relation = str(edge.get("relation") or edge.get("type") or "").upper().replace(" ", "_").replace("-", "_")

        # Contextually enrich parent_symbol and rationale documentation
        if source and target:
            if raw_relation in ("DEFINES", "CONTAINS", "DECLARES"):
                if target in raw_nodes and source in raw_nodes:
                    if not raw_nodes[target]["parent_symbol"]:
                        raw_nodes[target]["parent_symbol"] = raw_nodes[source]["name"]
            elif raw_relation in ("MEMBER_OF", "BELONGS_TO"):
                if source in raw_nodes and target in raw_nodes:
                    if not raw_nodes[source]["parent_symbol"]:
                        raw_nodes[source]["parent_symbol"] = raw_nodes[target]["name"]
            elif raw_relation in ("EXPLAINS", "RATIONALE", "DOCUMENTS"):
                if source in rationale_texts and target in raw_nodes:
                    if not raw_nodes[target]["documentation"]:
                        raw_nodes[target]["documentation"] = rationale_texts[source]
                elif target in rationale_texts and source in raw_nodes:
                    if not raw_nodes[source]["documentation"]:
                        raw_nodes[source]["documentation"] = rationale_texts[target]

        normalized_relation = CODE_RELATIONS.get(raw_relation)
        if not source or not target:
            continue
        if source not in source_to_scoped or target not in source_to_scoped or not normalized_relation:
            continue

        edges.append(
            GraphEdge(
                source_id=source_to_scoped[source],
                target_id=source_to_scoped[target],
                relation=normalized_relation,
                metadata={
                    key: value
                    for key, value in edge.items()
                    if key not in {"source", "target", "from", "to", "relation", "type"}
                },
            )
        )

    # Build final CodeSymbol objects with resolved qualified_names
    symbols: list[CodeSymbol] = []
    for node_info in raw_nodes.values():
        qname = node_info["explicit_qname"]
        if not qname:
            n_name = node_info["name"]
            n_module = node_info["module"]
            n_parent = node_info["parent_symbol"]
            if n_module and n_parent:
                qname = f"{n_module}.{n_parent}.{n_name}"
            elif n_parent:
                qname = f"{n_parent}.{n_name}"
            elif n_module:
                qname = f"{n_module}.{n_name}"
            else:
                qname = n_name

        symbols.append(
            CodeSymbol(
                id=node_info["id"],
                name=node_info["name"],
                file=node_info["file"],
                type=node_info["type"],
                user_id=node_info["user_id"],
                repository_id=node_info["repository_id"],
                qualified_name=qname,
                module=node_info["module"],
                package=node_info["package"],
                source_file=node_info["source_file"],
                line_start=node_info["line_start"],
                line_end=node_info["line_end"],
                parent_symbol=node_info["parent_symbol"],
                signature=node_info["signature"],
                documentation=node_info["documentation"],
                language=node_info["language"],
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