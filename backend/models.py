"""Normalized records shared by the ingestion components."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import NAMESPACE_URL, uuid5


def scoped_id(user_id: str, repository_id: str, source_id: str) -> str:
    """Make source identifiers unique across users and repositories."""
    return str(uuid5(NAMESPACE_URL, f"adaptive-memory:{user_id}:{repository_id}:{source_id}"))


@dataclass(frozen=True)
class CodeSymbol:
    id: str
    name: str
    file: str
    type: str
    user_id: str | None = None
    repository_id: str | None = None


@dataclass(frozen=True)
class DocumentEntity:
    id: str
    name: str
    type: str
    user_id: str | None = None
    repository_id: str | None = None


@dataclass(frozen=True)
class GraphEdge:
    source_id: str
    target_id: str
    relation: str
    metadata: dict[str, Any] = field(default_factory=dict)