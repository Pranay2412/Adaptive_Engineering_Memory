"""Adaptive Engineering Memory ingestion and storage components."""

from .bridge import build_cross_links
from .db_loader import Neo4jMemoryStore
from .ingestion_code import ingest_code, parse_graphify_graph

__all__ = [
    "Neo4jMemoryStore",
    "build_cross_links",
    "ingest_code",
    "ingest_documents",
    "ingest_repository",
    "parse_graphify_graph",
]


def ingest_documents(*args, **kwargs):
    """Lazy wrapper to prevent runpy RuntimeWarnings during direct module execution."""
    from .ingestion_docs import ingest_documents as _ingest_documents

    return _ingest_documents(*args, **kwargs)


def ingest_repository(*args, **kwargs):
    """Lazy wrapper to prevent runpy RuntimeWarnings during direct module execution."""
    from .pipeline import ingest_repository as _ingest_repository

    return _ingest_repository(*args, **kwargs)