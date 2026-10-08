"""Ingest repository Markdown with Cognee using Google Gemini and normalize its graph output."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from backend.models import DocumentEntity, GraphEdge, scoped_id

DOCUMENT_RELATIONS = {"DEFINES": "DEFINES", "MOTIVATES": "MOTIVATES", "DEPENDS_ON": "DEPENDS_ON"}
DOC_NODE_TYPES = {"document", "textdocument", "codefiledocument"}
CHUNK_NODE_TYPES = {"chunk", "documentchunk", "textsummary", "datapoint"}
NON_ENTITY_TYPES = DOC_NODE_TYPES | CHUNK_NODE_TYPES | {"data"}

COGNEE_INTERNAL_KEYS = {
    "source_dataset_ids",
    "dataset_ids",
    "dataset_id",
    "source_dataset_id",
    "embedding",
    "embeddings",
    "vector",
    "vector_id",
    "_id",
    "_type",
    "_key",
    "__class__",
    "__module__",
    "cognee_id",
    "node_id",
    "chunk_id",
    "doc_id",
    "name",
    "title",
    "label",
    "type",
    "entity_type",
    "description",
    "entity_description",
    "summary",
    "source_document",
    "document",
    "document_name",
    "doc_name",
    "document_path",
    "file_path",
    "path",
    "source_file",
    "file",
    "section",
    "heading",
    "header",
    "chapter",
    "source_chunk",
    "chunk",
    "chunk_text",
    "text_chunk",
    "snippet",
}


def _extract_dataset_id(properties: dict[str, Any]) -> str | None:
    for key in ("dataset_id", "source_dataset_id"):
        val = properties.get(key)
        if val is not None:
            return str(val)
    for key in ("source_dataset_ids", "dataset_ids"):
        val = properties.get(key)
        if isinstance(val, (list, tuple, set)) and val:
            return str(next(iter(val)))
        elif val is not None:
            return str(val)
    return None


def _clean_metadata(properties: dict[str, Any]) -> dict[str, Any]:
    cleaned = {}
    for key, value in properties.items():
        if key.lower() not in COGNEE_INTERNAL_KEYS and not key.startswith("_"):
            if isinstance(value, (str, int, float, bool, list, dict)):
                cleaned[key] = value
    return cleaned


def _enrich_entity_from_chunk(
    entity_info: dict[str, Any],
    chunk_info: dict[str, Any],
    doc_nodes: dict[str, dict[str, str | None]],
) -> None:
    if not entity_info["source_chunk"] and chunk_info.get("text"):
        entity_info["source_chunk"] = chunk_info["text"]
    if not entity_info["section"] and chunk_info.get("section"):
        entity_info["section"] = chunk_info["section"]

    c_path = chunk_info.get("path")
    c_name = chunk_info.get("doc_name")
    doc_id = chunk_info.get("doc_id")
    if doc_id and doc_id in doc_nodes:
        if not c_path:
            c_path = doc_nodes[doc_id].get("path")
        if not c_name:
            c_name = doc_nodes[doc_id].get("name")

    if not entity_info["document_path"] and c_path:
        entity_info["document_path"] = c_path
    if not entity_info["source_document"] and c_name:
        entity_info["source_document"] = c_name


def _enrich_entity_from_doc(
    entity_info: dict[str, Any],
    doc_info: dict[str, Any],
) -> None:
    if not entity_info["document_path"] and doc_info.get("path"):
        entity_info["document_path"] = doc_info["path"]
    if not entity_info["source_document"] and doc_info.get("name"):
        entity_info["source_document"] = doc_info["name"]


def _value(record: Any, key: str, default: Any = None) -> Any:
    if isinstance(record, dict):
        return record.get(key, default)
    return getattr(record, key, default)


def _dataset_name(user_id: str, repository_id: str) -> str:
    digest = hashlib.sha256(f"{user_id}:{repository_id}".encode("utf-8")).hexdigest()[:24]
    return f"adaptive_memory_{digest}"


def _dataset_id(add_result: Any) -> str | None:
    candidates = [add_result, _value(add_result, "payload"), _value(add_result, "result")]
    for candidate in candidates:
        value = _value(candidate, "dataset_id")
        if value is not None:
            return str(value)
    return None


def _has_dataset_provenance(properties: dict[str, Any], dataset_id: str) -> bool:
    values = [
        properties.get("source_dataset_ids"),
        properties.get("dataset_ids"),
        properties.get("dataset_id"),
        properties.get("source_dataset_id"),
    ]
    return any(
        dataset_id == str(value)
        for item in values
        for value in (item if isinstance(item, (list, tuple, set)) else [item])
        if value is not None
    )


def _prepare_cognee_environment() -> None:
    """Configure Cognee's LLM and embedding providers from available API keys."""
    load_dotenv()
    openai_key = os.environ.get("OPENAI_API_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    generic_key = os.environ.get("LLM_API_KEY")

    llm_provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if not llm_provider:
        llm_provider = "openai" if openai_key else "gemini"
    elif llm_provider == "openai" and not openai_key and not generic_key and gemini_key:
        llm_provider = "gemini"
    elif llm_provider == "gemini" and not gemini_key and not generic_key and openai_key:
        llm_provider = "openai"

    provider_key = openai_key if llm_provider == "openai" else gemini_key if llm_provider == "gemini" else None
    api_key = provider_key or generic_key or openai_key or gemini_key
    if not api_key:
        raise EnvironmentError(
            "Set OPENAI_API_KEY, GEMINI_API_KEY, or LLM_API_KEY for Cognee document ingestion."
        )

    default_llm_model = "gpt-4o-mini" if llm_provider == "openai" else "gemini/gemini-2.0-flash"
    llm_model = os.environ.get("LLM_MODEL") or default_llm_model
    if llm_provider == "openai" and llm_model.startswith("gemini/"):
        llm_model = default_llm_model

    embedding_provider = os.environ.get("EMBEDDING_PROVIDER", llm_provider).strip().lower()
    if embedding_provider == "openai" and not openai_key and not generic_key and gemini_key:
        embedding_provider = "gemini"
    elif embedding_provider == "gemini" and not gemini_key and not generic_key and openai_key:
        embedding_provider = "openai"
    embedding_key = (
        os.environ.get("EMBEDDING_API_KEY")
        or (openai_key if embedding_provider == "openai" else None)
        or (gemini_key if embedding_provider == "gemini" else None)
        or generic_key
        or api_key
    )
    default_embedding_model = (
        "openai/text-embedding-3-small"
        if embedding_provider == "openai"
        else "gemini/text-embedding-004"
    )
    embedding_model = os.environ.get("EMBEDDING_MODEL") or default_embedding_model
    if embedding_provider == "openai" and embedding_model.startswith("gemini/"):
        embedding_model = default_embedding_model

    os.environ["LLM_PROVIDER"] = llm_provider
    os.environ["LLM_MODEL"] = llm_model
    os.environ["LLM_API_KEY"] = api_key
    os.environ["EMBEDDING_PROVIDER"] = embedding_provider
    os.environ["EMBEDDING_MODEL"] = embedding_model
    os.environ["EMBEDDING_API_KEY"] = embedding_key

    import cognee

    if hasattr(cognee, "config"):
        if hasattr(cognee.config, "set_llm_config"):
            cognee.config.set_llm_config({
                "llm_provider": llm_provider,
                "llm_model": llm_model,
                "llm_api_key": api_key,
            })
        if hasattr(cognee.config, "set_embedding_config"):
            cognee.config.set_embedding_config({
                "embedding_provider": embedding_provider,
                "embedding_model": embedding_model,
                "embedding_api_key": embedding_key,
            })


def normalize_cognee_graph(
    nodes: list[Any],
    edges: list[Any],
    *,
    user_id: str,
    repository_id: str,
    dataset_id: str | None = None,
) -> tuple[list[DocumentEntity], list[GraphEdge]]:
    """Convert Cognee graph data into scoped entities and semantic edges, preserving provenance."""
    entity_ids: dict[str, str] = {}
    raw_entities: dict[str, dict[str, Any]] = {}
    doc_nodes: dict[str, dict[str, str | None]] = {}
    chunk_nodes: dict[str, dict[str, str | None]] = {}
    provenance_seen = False

    # Pass 1: Parse and categorize nodes
    for node in nodes:
        if not isinstance(node, (tuple, list)) or len(node) < 2:
            continue
        source_id, properties = str(node[0]), node[1]
        if not isinstance(properties, dict):
            continue

        # Dataset provenance checking
        if dataset_id is not None:
            has_provenance = any(
                key in properties
                for key in ("source_dataset_ids", "dataset_ids", "dataset_id", "source_dataset_id")
            )
            provenance_seen = provenance_seen or has_provenance
            if not _has_dataset_provenance(properties, dataset_id):
                continue

        raw_type = str(properties.get("entity_type") or properties.get("type") or "Concept")
        norm_type = raw_type.lower().replace("_", "").replace("-", "").strip()

        # Document nodes
        if norm_type in DOC_NODE_TYPES:
            doc_name = (
                properties.get("name")
                or properties.get("title")
                or properties.get("label")
                or properties.get("file_name")
            )
            doc_path = (
                properties.get("document_path")
                or properties.get("file_path")
                or properties.get("path")
                or properties.get("source_file")
            )
            if doc_path and not doc_name:
                doc_name = Path(doc_path).name
            elif doc_name and not doc_path:
                doc_path = doc_name
            doc_nodes[source_id] = {
                "name": str(doc_name) if doc_name else None,
                "path": str(doc_path) if doc_path else None,
            }
            continue

        # Chunk nodes
        if norm_type in CHUNK_NODE_TYPES:
            chunk_text = (
                properties.get("text")
                or properties.get("chunk_text")
                or properties.get("content")
                or properties.get("snippet")
                or properties.get("body")
            )
            chunk_section = (
                properties.get("section")
                or properties.get("heading")
                or properties.get("header")
            )
            chunk_doc_id = (
                properties.get("document_id")
                or properties.get("source_document_id")
                or properties.get("doc_id")
            )
            chunk_path = (
                properties.get("document_path")
                or properties.get("file_path")
                or properties.get("path")
                or properties.get("source_file")
            )
            chunk_doc_name = (
                properties.get("source_document")
                or properties.get("document")
                or properties.get("document_name")
            )
            chunk_nodes[source_id] = {
                "text": str(chunk_text) if chunk_text else None,
                "section": str(chunk_section) if chunk_section else None,
                "doc_id": str(chunk_doc_id) if chunk_doc_id else None,
                "path": str(chunk_path) if chunk_path else None,
                "doc_name": str(chunk_doc_name) if chunk_doc_name else None,
            }
            continue

        # Skip other non-entity structural types
        if norm_type in NON_ENTITY_TYPES:
            continue

        name = properties.get("name") or properties.get("title") or properties.get("label")
        if not name:
            continue

        entity_id = scoped_id(user_id, repository_id, source_id)
        entity_ids[source_id] = entity_id

        # Extract direct properties
        desc = (
            properties.get("description")
            or properties.get("entity_description")
            or properties.get("summary")
        )
        src_doc = (
            properties.get("source_document")
            or properties.get("document")
            or properties.get("document_name")
            or properties.get("doc_name")
        )
        doc_path = (
            properties.get("document_path")
            or properties.get("file_path")
            or properties.get("path")
            or properties.get("source_file")
            or properties.get("file")
        )
        section = (
            properties.get("section")
            or properties.get("heading")
            or properties.get("header")
            or properties.get("chapter")
        )
        src_chunk = (
            properties.get("source_chunk")
            or properties.get("chunk")
            or properties.get("chunk_text")
            or properties.get("text_chunk")
            or properties.get("snippet")
        )
        resolved_dataset_id = dataset_id or _extract_dataset_id(properties)

        raw_entities[source_id] = {
            "id": entity_id,
            "name": str(name),
            "type": raw_type,
            "user_id": user_id,
            "repository_id": repository_id,
            "description": str(desc) if desc else None,
            "source_document": str(src_doc) if src_doc else None,
            "document_path": str(doc_path) if doc_path else None,
            "section": str(section) if section else None,
            "source_chunk": str(src_chunk) if src_chunk else None,
            "dataset_id": str(resolved_dataset_id) if resolved_dataset_id else None,
            "metadata": _clean_metadata(properties),
        }

    if dataset_id is not None and nodes and not provenance_seen:
        raise RuntimeError(
            "Cognee graph nodes do not expose dataset provenance; refusing to copy an unscoped graph"
        )

    # Pass 2: Process edges to link chunks to docs and enrich entities
    normalized_edges: list[GraphEdge] = []
    for edge in edges:
        if not isinstance(edge, (tuple, list)) or len(edge) < 3:
            continue
        source, target, raw_relation = str(edge[0]), str(edge[1]), str(edge[2])
        properties = edge[3] if len(edge) > 3 and isinstance(edge[3], dict) else {}

        if dataset_id is not None and properties and not _has_dataset_provenance(properties, dataset_id):
            continue

        # Chunk <-> Document linking
        if source in chunk_nodes and target in doc_nodes:
            if not chunk_nodes[source]["path"] and doc_nodes[target]["path"]:
                chunk_nodes[source]["path"] = doc_nodes[target]["path"]
            if not chunk_nodes[source]["doc_name"] and doc_nodes[target]["name"]:
                chunk_nodes[source]["doc_name"] = doc_nodes[target]["name"]
        elif target in chunk_nodes and source in doc_nodes:
            if not chunk_nodes[target]["path"] and doc_nodes[source]["path"]:
                chunk_nodes[target]["path"] = doc_nodes[source]["path"]
            if not chunk_nodes[target]["doc_name"] and doc_nodes[source]["name"]:
                chunk_nodes[target]["doc_name"] = doc_nodes[source]["name"]

        # Entity <-> Chunk linking
        if source in raw_entities and target in chunk_nodes:
            _enrich_entity_from_chunk(raw_entities[source], chunk_nodes[target], doc_nodes)
        elif target in raw_entities and source in chunk_nodes:
            _enrich_entity_from_chunk(raw_entities[target], chunk_nodes[source], doc_nodes)

        # Entity <-> Document linking
        if source in raw_entities and target in doc_nodes:
            _enrich_entity_from_doc(raw_entities[source], doc_nodes[target])
        elif target in raw_entities and source in doc_nodes:
            _enrich_entity_from_doc(raw_entities[target], doc_nodes[source])

        # Domain semantic edges between entities
        relation_key = re.sub(r"[^A-Z0-9]+", "_", raw_relation.upper()).strip("_")
        relation = DOCUMENT_RELATIONS.get(relation_key)
        if relation and source in entity_ids and target in entity_ids:
            normalized_edges.append(
                GraphEdge(
                    source_id=entity_ids[source],
                    target_id=entity_ids[target],
                    relation=relation,
                    metadata={
                        key: value
                        for key, value in properties.items()
                        if isinstance(value, (str, int, float, bool))
                        and key not in COGNEE_INTERNAL_KEYS
                    },
                )
            )

    # Pass 3: Finalize and instantiate DocumentEntity objects
    entities: list[DocumentEntity] = []
    for info in raw_entities.values():
        entities.append(
            DocumentEntity(
                id=info["id"],
                name=info["name"],
                type=info["type"],
                user_id=info["user_id"],
                repository_id=info["repository_id"],
                description=info["description"],
                source_document=info["source_document"],
                document_path=info["document_path"],
                section=info["section"],
                source_chunk=info["source_chunk"],
                dataset_id=info["dataset_id"],
                source_file=info["document_path"],
                metadata=info["metadata"],
            )
        )

    return entities, normalized_edges


async def ingest_documents(
    repository_path: str | Path,
    *,
    user_id: str,
    repository_id: str,
) -> tuple[list[DocumentEntity], list[GraphEdge]]:
    """Add all Markdown files to an isolated Cognee dataset and cognify them."""
    if not user_id.strip():
        raise ValueError("user_id must not be empty")
    _prepare_cognee_environment()
    root = Path(repository_path).expanduser().resolve()
    markdown_files = sorted(path for path in root.rglob("*.md") if ".venv" not in path.parts and "node_modules" not in path.parts)
    if not markdown_files:
        return [], []

    import cognee
    from cognee.infrastructure.databases.graph import get_graph_engine

    dataset_name = _dataset_name(user_id, repository_id)
    add_result = await cognee.add([str(path) for path in markdown_files], dataset_name=dataset_name)
    await cognee.cognify(datasets=[dataset_name])
    dataset_id = _dataset_id(add_result)
    if dataset_id is None:
        raise RuntimeError("Cognee did not return a dataset_id; cannot safely isolate its graph data")
    graph_engine = await get_graph_engine()
    nodes, edges = await graph_engine.get_graph_data()
    return normalize_cognee_graph(
        nodes,
        edges,
        user_id=user_id,
        repository_id=repository_id,
        dataset_id=dataset_id,
    )

# At the bottom of backend/ingestion_docs.py
if __name__ == "__main__":
    import asyncio
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    asyncio.run(
        ingest_documents(
            path,
            user_id="default_user",
            repository_id="default_repo",
        )
    )