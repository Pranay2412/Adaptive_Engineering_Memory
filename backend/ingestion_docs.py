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
NON_ENTITY_TYPES = {
    "document", "textdocument", "documentchunk", "chunk", "textsummary",
    "datapoint", "data", "codefiledocument",
}


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
    """
    Configures Cognee environment variables to route LLM and embedding requests
    through Google Gemini via LiteLLM.
    """
    load_dotenv()
    api_key = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )
    if not api_key:
        raise EnvironmentError(
            "GEMINI_API_KEY or LLM_API_KEY is required in .env for Cognee document ingestion."
        )

    llm_provider = os.environ.get("LLM_PROVIDER", "gemini")
    llm_model = os.environ.get("LLM_MODEL", "gemini/gemini-2.0-flash")
    embedding_provider = os.environ.get("EMBEDDING_PROVIDER", "gemini")
    embedding_model = os.environ.get("EMBEDDING_MODEL", "gemini/text-embedding-004")

    # Pass configuration into environment for Cognee / LiteLLM
    os.environ["LLM_PROVIDER"] = llm_provider
    os.environ["LLM_MODEL"] = llm_model
    os.environ["LLM_API_KEY"] = api_key
    os.environ["GEMINI_API_KEY"] = api_key

    os.environ["EMBEDDING_PROVIDER"] = embedding_provider
    os.environ["EMBEDDING_MODEL"] = embedding_model
    os.environ["EMBEDDING_API_KEY"] = api_key

    import cognee
    if hasattr(cognee, "config"):
        if hasattr(cognee.config, "set_llm_config"):
            try:
                cognee.config.set_llm_config({
                    "llm_provider": llm_provider,
                    "llm_model": llm_model,
                    "llm_api_key": api_key,
                })
            except TypeError:
                cognee.config.set_llm_config(
                    llm_provider=llm_provider,
                    llm_model=llm_model,
                    llm_api_key=api_key,
                )


def normalize_cognee_graph(
    nodes: list[Any],
    edges: list[Any],
    *,
    user_id: str,
    repository_id: str,
    dataset_id: str | None = None,
) -> tuple[list[DocumentEntity], list[GraphEdge]]:
    """Convert Cognee graph data into scoped entities and semantic edges."""
    entity_ids: dict[str, str] = {}
    entities: list[DocumentEntity] = []
    provenance_seen = False
    for node in nodes:
        if not isinstance(node, (tuple, list)) or len(node) < 2:
            continue
        source_id, properties = str(node[0]), node[1]
        if not isinstance(properties, dict):
            continue
        if dataset_id is not None:
            has_provenance = any(
                key in properties for key in ("source_dataset_ids", "dataset_ids", "dataset_id", "source_dataset_id")
            )
            provenance_seen = provenance_seen or has_provenance
            if not _has_dataset_provenance(properties, dataset_id):
                continue
        entity_type = str(properties.get("entity_type") or properties.get("type") or "Concept")
        if entity_type.lower().replace("_", "") in NON_ENTITY_TYPES:
            continue
        name = properties.get("name") or properties.get("title") or properties.get("label")
        if not name:
            continue
        entity_id = scoped_id(user_id, repository_id, source_id)
        entity_ids[source_id] = entity_id
        entities.append(
            DocumentEntity(
                id=entity_id,
                name=str(name),
                type=entity_type,
                user_id=user_id,
                repository_id=repository_id,
            )
        )
    if dataset_id is not None and nodes and not provenance_seen:
        raise RuntimeError(
            "Cognee graph nodes do not expose dataset provenance; refusing to copy an unscoped graph"
        )

    normalized_edges: list[GraphEdge] = []
    for edge in edges:
        if not isinstance(edge, (tuple, list)) or len(edge) < 3:
            continue
        source, target, raw_relation = str(edge[0]), str(edge[1]), str(edge[2])
        properties = edge[3] if len(edge) > 3 and isinstance(edge[3], dict) else {}
        if dataset_id is not None and properties and not _has_dataset_provenance(properties, dataset_id):
            continue
        relation_key = re.sub(r"[^A-Z0-9]+", "_", raw_relation.upper()).strip("_")
        relation = DOCUMENT_RELATIONS.get(relation_key)
        if relation and source in entity_ids and target in entity_ids:
            normalized_edges.append(
                GraphEdge(
                    source_id=entity_ids[source],
                    target_id=entity_ids[target],
                    relation=relation,
                    metadata={key: value for key, value in properties.items() if isinstance(value, (str, int, float, bool))},
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