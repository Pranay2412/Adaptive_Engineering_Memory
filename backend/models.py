"""Normalized records shared by the ingestion components."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
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
    qualified_name: str | None = None
    module: str | None = None
    package: str | None = None
    source_file: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    parent_symbol: str | None = None
    signature: str | None = None
    documentation: str | None = None
    language: str | None = None

    def __post_init__(self) -> None:
        if not self.source_file and self.file:
            object.__setattr__(self, "source_file", self.file)
        elif not self.file and self.source_file:
            object.__setattr__(self, "file", self.source_file)

    def to_dict(self) -> dict[str, Any]:
        """Serialize CodeSymbol to a clean dictionary representation."""
        return {
            "id": self.id,
            "name": self.name,
            "file": self.file,
            "type": self.type,
            "user_id": self.user_id,
            "repository_id": self.repository_id,
            "qualified_name": self.qualified_name,
            "module": self.module,
            "package": self.package,
            "source_file": self.source_file,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "parent_symbol": self.parent_symbol,
            "signature": self.signature,
            "documentation": self.documentation,
            "language": self.language,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CodeSymbol:
        """Reconstruct CodeSymbol from dictionary representation."""
        line_start = data.get("line_start")
        line_end = data.get("line_end")
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            file=str(data.get("file") or data.get("source_file") or ""),
            type=str(data.get("type") or data.get("symbol_type") or "symbol"),
            user_id=data.get("user_id"),
            repository_id=data.get("repository_id"),
            qualified_name=data.get("qualified_name"),
            module=data.get("module"),
            package=data.get("package"),
            source_file=data.get("source_file") or data.get("file"),
            line_start=int(line_start) if line_start is not None else None,
            line_end=int(line_end) if line_end is not None else None,
            parent_symbol=data.get("parent_symbol"),
            signature=data.get("signature"),
            documentation=data.get("documentation") or data.get("docstring"),
            language=data.get("language"),
        )


@dataclass(frozen=True)
class DocumentEntity:
    id: str
    name: str
    type: str
    user_id: str | None = None
    repository_id: str | None = None
    description: str | None = None
    source_file: str | None = None
    section: str | None = None
    source_document: str | None = None
    document_path: str | None = None
    source_chunk: str | None = None
    dataset_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Keep source_file and document_path bidirectionally synchronized
        if not self.document_path and self.source_file:
            object.__setattr__(self, "document_path", self.source_file)
        elif not self.source_file and self.document_path:
            object.__setattr__(self, "source_file", self.document_path)

        # Infer source_document name if missing but document_path is present
        if not self.source_document and self.document_path:
            from pathlib import Path

            doc_name = Path(self.document_path).name
            if doc_name and doc_name != ".":
                object.__setattr__(self, "source_document", doc_name)

    @property
    def entity_type(self) -> str:
        """Alias for type to match domain terminology."""
        return self.type

    @property
    def entity_description(self) -> str | None:
        """Alias for description to match domain terminology."""
        return self.description

    def to_retrieval_text(self) -> str:
        """Format entity into a descriptive text passage for hybrid (BM25 + vector) retrieval."""
        parts = [f"Documentation Concept: {self.name}", f"Type: {self.type}"]
        if self.description:
            parts.append(f"Description: {self.description}")
        if self.source_document:
            parts.append(f"Document: {self.source_document}")
        if self.document_path:
            parts.append(f"Path: {self.document_path}")
        if self.section:
            parts.append(f"Section: {self.section}")
        if self.source_chunk:
            parts.append(f"Context: {self.source_chunk}")
        return " | ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """Serialize DocumentEntity to a dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "user_id": self.user_id,
            "repository_id": self.repository_id,
            "description": self.description,
            "source_file": self.source_file,
            "section": self.section,
            "source_document": self.source_document,
            "document_path": self.document_path,
            "source_chunk": self.source_chunk,
            "dataset_id": self.dataset_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DocumentEntity:
        """Reconstruct DocumentEntity from dictionary representation."""
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            type=str(data.get("type") or data.get("entity_type") or "entity"),
            user_id=data.get("user_id"),
            repository_id=data.get("repository_id"),
            description=data.get("description") or data.get("entity_description"),
            source_file=data.get("source_file") or data.get("document_path"),
            section=data.get("section"),
            source_document=data.get("source_document"),
            document_path=data.get("document_path") or data.get("source_file"),
            source_chunk=data.get("source_chunk"),
            dataset_id=data.get("dataset_id"),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class GraphEdge:
    source_id: str
    target_id: str
    relation: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize GraphEdge to a dictionary."""
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation": self.relation,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GraphEdge:
        """Reconstruct GraphEdge from dictionary representation."""
        return cls(
            source_id=str(data["source_id"]),
            target_id=str(data["target_id"]),
            relation=str(data["relation"]),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class Repository:
    id: str
    name: str
    user_id: str | None = None
    repository_id: str | None = None
    url: str | None = None
    default_branch: str | None = "main"
    language: str | None = None
    description: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize Repository to a dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "user_id": self.user_id,
            "repository_id": self.repository_id or self.id,
            "url": self.url,
            "default_branch": self.default_branch,
            "language": self.language,
            "description": self.description,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Repository:
        """Reconstruct Repository from dictionary representation."""
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            user_id=data.get("user_id"),
            repository_id=data.get("repository_id") or data.get("id"),
            url=data.get("url"),
            default_branch=data.get("default_branch", "main"),
            language=data.get("language"),
            description=data.get("description"),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class Module:
    id: str
    name: str
    package: str | None = None
    path: str | None = None
    language: str | None = None
    user_id: str | None = None
    repository_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize Module to a dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "package": self.package,
            "path": self.path,
            "language": self.language,
            "user_id": self.user_id,
            "repository_id": self.repository_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Module:
        """Reconstruct Module from dictionary representation."""
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            package=data.get("package"),
            path=data.get("path"),
            language=data.get("language"),
            user_id=data.get("user_id"),
            repository_id=data.get("repository_id"),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class Document:
    id: str
    name: str
    path: str
    format: str | None = "markdown"
    dataset_id: str | None = None
    user_id: str | None = None
    repository_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize Document to a dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "format": self.format,
            "dataset_id": self.dataset_id,
            "user_id": self.user_id,
            "repository_id": self.repository_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Document:
        """Reconstruct Document from dictionary representation."""
        from pathlib import Path

        path = str(data["path"])
        name = str(data.get("name") or data.get("title") or Path(path).name)
        return cls(
            id=str(data["id"]),
            name=name,
            path=path,
            format=data.get("format", "markdown"),
            dataset_id=data.get("dataset_id"),
            user_id=data.get("user_id"),
            repository_id=data.get("repository_id"),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class RelationshipHop:
    """A single directed edge hop within a knowledge graph retrieval traversal path."""

    source_id: str
    source_name: str
    relation: str
    target_id: str
    target_name: str
    confidence: float | None = None
    matching_method: str | None = None
    explanation: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize hop to a dictionary."""
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "relation": self.relation,
            "target_id": self.target_id,
            "target_name": self.target_name,
            "confidence": self.confidence,
            "matching_method": self.matching_method,
            "explanation": self.explanation,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RelationshipHop:
        """Reconstruct hop from dictionary representation."""
        confidence = data.get("confidence")
        return cls(
            source_id=str(data["source_id"]),
            source_name=str(data["source_name"]),
            relation=str(data["relation"]),
            target_id=str(data["target_id"]),
            target_name=str(data["target_name"]),
            confidence=float(confidence) if confidence is not None else None,
            matching_method=data.get("matching_method"),
            explanation=data.get("explanation"),
            metadata=dict(data.get("metadata", {})),
        )

    def format_hop(self) -> str:
        """Format hop into human-readable path string."""
        conf_str = f" [{self.confidence:.2f}]" if self.confidence is not None else ""
        return f"({self.source_name}) -[:{self.relation}{conf_str}]-> ({self.target_name})"


@dataclass
class RetrievalResult:
    """Normalized engineering memory retrieval result."""

    entity: str
    type: str
    source: str
    relationship_path: list[RelationshipHop] = field(default_factory=list)
    relevance_information: str = ""
    repository: str = ""
    file_or_document: str = ""
    confidence: float | None = None
    node_id: str = ""
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def file(self) -> str:
        """Alias for file_or_document when entity is from code."""
        return self.file_or_document

    @property
    def document(self) -> str:
        """Alias for file_or_document when entity is from documentation."""
        return self.file_or_document

    def to_dict(self) -> dict[str, Any]:
        """Serialize retrieval result to dictionary containing all required prompt keys."""
        return {
            "entity": self.entity,
            "type": self.type,
            "source": self.source,
            "relationship_path": [hop.to_dict() for hop in self.relationship_path],
            "relevance_information": self.relevance_information,
            "repository": self.repository,
            "file/document": self.file_or_document,
            "file_or_document": self.file_or_document,
            "confidence": self.confidence,
            "node_id": self.node_id,
            "score": round(self.score, 4),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RetrievalResult:
        """Reconstruct retrieval result from dictionary representation."""
        hops_data = data.get("relationship_path", [])
        hops = [
            RelationshipHop.from_dict(h) if isinstance(h, dict) else h
            for h in hops_data
        ]
        file_doc = (
            data.get("file_or_document")
            or data.get("file/document")
            or data.get("file")
            or data.get("document")
            or ""
        )
        confidence = data.get("confidence")
        return cls(
            entity=str(data["entity"]),
            type=str(data["type"]),
            source=str(data["source"]),
            relationship_path=hops,
            relevance_information=str(data.get("relevance_information", "")),
            repository=str(data.get("repository", "")),
            file_or_document=str(file_doc),
            confidence=float(confidence) if confidence is not None else None,
            node_id=str(data.get("node_id", "")),
            score=float(data.get("score", 0.0)),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class MemoryQueryResponse:
    """Container for multi-result engineering memory query responses."""

    query: str
    user_id: str
    repository_id: str
    results: list[RetrievalResult] = field(default_factory=list)
    total_results: int = 0
    query_terms: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize query response to dictionary."""
        return {
            "query": self.query,
            "user_id": self.user_id,
            "repository_id": self.repository_id,
            "total_results": len(self.results),
            "query_terms": list(self.query_terms),
            "results": [r.to_dict() for r in self.results],
        }


@dataclass
class ResultProvenance:
    """Detailed provenance tracking for retrieved engineering knowledge."""

    source_type: str  # "code", "documentation", "graph_traversal", "rationale"
    repository_id: str
    file_path: str | None = None
    document_path: str | None = None
    section: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    dataset_id: str | None = None
    created_at: str | None = None
    channels: list[str] = field(default_factory=list)  # ["lexical", "semantic", "graph"]
    matched_terms: list[str] = field(default_factory=list)
    graph_hops_count: int = 0
    traversal_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize provenance to a dictionary."""
        return {
            "source_type": self.source_type,
            "repository_id": self.repository_id,
            "file_path": self.file_path,
            "document_path": self.document_path,
            "section": self.section,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "dataset_id": self.dataset_id,
            "created_at": self.created_at,
            "channels": list(self.channels),
            "matched_terms": list(self.matched_terms),
            "graph_hops_count": self.graph_hops_count,
            "traversal_path": self.traversal_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResultProvenance:
        """Reconstruct provenance from dictionary representation."""
        line_start = data.get("line_start")
        line_end = data.get("line_end")
        return cls(
            source_type=str(data.get("source_type", "unknown")),
            repository_id=str(data.get("repository_id", "")),
            file_path=data.get("file_path"),
            document_path=data.get("document_path"),
            section=data.get("section"),
            line_start=int(line_start) if line_start is not None else None,
            line_end=int(line_end) if line_end is not None else None,
            dataset_id=data.get("dataset_id"),
            created_at=data.get("created_at"),
            channels=list(data.get("channels", [])),
            matched_terms=list(data.get("matched_terms", [])),
            graph_hops_count=int(data.get("graph_hops_count", 0)),
            traversal_path=data.get("traversal_path"),
        )


@dataclass
class HybridRetrievalResult:
    """Normalized hybrid retrieval result with multi-channel scores and provenance."""

    entity: str
    type: str
    source: str
    relationship_path: list[RelationshipHop] = field(default_factory=list)
    relevance_information: str = ""
    repository: str = ""
    file_or_document: str = ""
    confidence: float | None = None
    score: float = 0.0
    scores_breakdown: dict[str, float] = field(default_factory=dict)
    provenance: ResultProvenance = field(default_factory=lambda: ResultProvenance("unknown", ""))
    content_snippet: str = ""
    node_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def file(self) -> str:
        """Alias for file_or_document when entity originates from code."""
        return self.file_or_document

    @property
    def document(self) -> str:
        """Alias for file_or_document when entity originates from documentation."""
        return self.file_or_document

    def to_context_str(self) -> str:
        """Format result as prompt-ready context block for the Adaptive Context Orchestrator."""
        lines = [f"### [{self.type}] {self.entity}"]
        if self.file_or_document:
            lines.append(f"- Location: {self.file_or_document}")
        if self.confidence is not None:
            lines.append(f"- Confidence: {self.confidence:.2f}")
        if self.provenance.channels:
            lines.append(f"- Matched via: {', '.join(self.provenance.channels)}")
        if self.content_snippet:
            lines.append(f"- Details: {self.content_snippet}")
        if self.relationship_path:
            path_str = " -> ".join([h.format_hop() for h in self.relationship_path])
            lines.append(f"- Graph Path: {path_str}")
        return "\n".join(lines)

    def estimate_tokens(self) -> int:
        """Rough token estimate for context budget allocation (approx 4 chars per token)."""
        text = self.to_context_str()
        return max(1, len(text) // 4)

    def to_dict(self) -> dict[str, Any]:
        """Serialize hybrid result to dictionary."""
        return {
            "entity": self.entity,
            "type": self.type,
            "source": self.source,
            "relationship_path": [hop.to_dict() for hop in self.relationship_path],
            "relevance_information": self.relevance_information,
            "repository": self.repository,
            "file/document": self.file_or_document,
            "file_or_document": self.file_or_document,
            "confidence": self.confidence,
            "score": round(self.score, 4),
            "scores_breakdown": {k: round(v, 4) for k, v in self.scores_breakdown.items()},
            "provenance": self.provenance.to_dict(),
            "content_snippet": self.content_snippet,
            "node_id": self.node_id,
            "estimated_tokens": self.estimate_tokens(),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HybridRetrievalResult:
        """Reconstruct hybrid result from dictionary representation."""
        hops_data = data.get("relationship_path", [])
        hops = [
            RelationshipHop.from_dict(h) if isinstance(h, dict) else h
            for h in hops_data
        ]
        file_doc = (
            data.get("file_or_document")
            or data.get("file/document")
            or data.get("file")
            or data.get("document")
            or ""
        )
        prov_data = data.get("provenance", {})
        provenance = (
            ResultProvenance.from_dict(prov_data)
            if isinstance(prov_data, dict)
            else ResultProvenance("unknown", "")
        )
        confidence = data.get("confidence")
        return cls(
            entity=str(data["entity"]),
            type=str(data["type"]),
            source=str(data["source"]),
            relationship_path=hops,
            relevance_information=str(data.get("relevance_information", "")),
            repository=str(data.get("repository", "")),
            file_or_document=str(file_doc),
            confidence=float(confidence) if confidence is not None else None,
            score=float(data.get("score", 0.0)),
            scores_breakdown=dict(data.get("scores_breakdown", {})),
            provenance=provenance,
            content_snippet=str(data.get("content_snippet", "")),
            node_id=str(data.get("node_id", "")),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class HybridQueryResponse:
    """Container for multi-channel hybrid engineering memory query responses."""

    query: str
    user_id: str
    repository_id: str
    results: list[HybridRetrievalResult] = field(default_factory=list)
    total_results: int = 0
    query_terms: list[str] = field(default_factory=list)
    retrieval_stats: dict[str, Any] = field(default_factory=dict)

    def to_orchestrator_context(self, max_tokens: int = 2000) -> str:
        """Format top retrieval results into a token-budgeted prompt context for the Adaptive Context Orchestrator."""
        context_blocks = []
        tokens_used = 0
        for r in self.results:
            block = r.to_context_str()
            block_tokens = r.estimate_tokens()
            if tokens_used + block_tokens > max_tokens and context_blocks:
                break
            context_blocks.append(block)
            tokens_used += block_tokens
        return "\n\n".join(context_blocks)

    def to_dict(self) -> dict[str, Any]:
        """Serialize response to dictionary."""
        return {
            "query": self.query,
            "user_id": self.user_id,
            "repository_id": self.repository_id,
            "total_results": len(self.results),
            "query_terms": list(self.query_terms),
            "retrieval_stats": dict(self.retrieval_stats),
            "results": [r.to_dict() for r in self.results],
        }


class QueryIntent(str, Enum):
    """Categorized engineering intents for user queries."""

    CODE_NAVIGATION = "code_navigation"
    EXPLANATION = "explanation"
    DEBUGGING = "debugging"
    FEATURE_IMPLEMENTATION = "feature_implementation"
    REFACTORING = "refactoring"
    ARCHITECTURE = "architecture"
    DOCUMENTATION = "documentation"
    SESSION_CONTINUATION = "session_continuation"


@dataclass
class IntentAnalysisResult:
    """Detailed outcome of query intent analysis."""

    primary_intent: QueryIntent
    confidence: float
    secondary_intents: list[tuple[QueryIntent, float]] = field(default_factory=list)
    extracted_entities: list[str] = field(default_factory=list)
    reasoning: str = ""
    selected_sources: list[str] = field(default_factory=list)
    retrieval_strategy: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize intent analysis result to dictionary."""
        return {
            "primary_intent": (
                self.primary_intent.value
                if isinstance(self.primary_intent, QueryIntent)
                else str(self.primary_intent)
            ),
            "confidence": round(self.confidence, 4),
            "secondary_intents": [
                (
                    (intent.value if isinstance(intent, QueryIntent) else str(intent)),
                    round(score, 4),
                )
                for intent, score in self.secondary_intents
            ],
            "extracted_entities": list(self.extracted_entities),
            "reasoning": self.reasoning,
            "selected_sources": list(self.selected_sources),
            "retrieval_strategy": dict(self.retrieval_strategy),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IntentAnalysisResult:
        """Reconstruct IntentAnalysisResult from dictionary."""
        raw_primary = data.get("primary_intent", QueryIntent.EXPLANATION.value)
        try:
            primary = QueryIntent(raw_primary)
        except ValueError:
            primary = QueryIntent.EXPLANATION

        secondaries: list[tuple[QueryIntent, float]] = []
        for item in data.get("secondary_intents", []):
            if isinstance(item, (list, tuple)) and len(item) == 2:
                try:
                    s_intent = QueryIntent(item[0])
                except ValueError:
                    continue
                secondaries.append((s_intent, float(item[1])))

        return cls(
            primary_intent=primary,
            confidence=float(data.get("confidence", 0.0)),
            secondary_intents=secondaries,
            extracted_entities=list(data.get("extracted_entities", [])),
            reasoning=str(data.get("reasoning", "")),
            selected_sources=list(data.get("selected_sources", [])),
            retrieval_strategy=dict(data.get("retrieval_strategy", {})),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class OrchestratedContext:
    """Optimized context assembled by the Adaptive Context Orchestrator."""

    context_text: str
    query: str
    user_id: str
    repository_id: str
    intent: IntentAnalysisResult
    token_budget: int | None = None
    estimated_tokens: int = 0
    retrieved_results: list[HybridRetrievalResult] = field(default_factory=list)
    retrieval_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize OrchestratedContext to dictionary."""
        return {
            "context_text": self.context_text,
            "query": self.query,
            "user_id": self.user_id,
            "repository_id": self.repository_id,
            "intent": self.intent.to_dict(),
            "token_budget": self.token_budget,
            "estimated_tokens": self.estimated_tokens,
            "total_results": len(self.retrieved_results),
            "retrieved_results": [r.to_dict() for r in self.retrieved_results],
            "retrieval_metadata": dict(self.retrieval_metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OrchestratedContext:
        """Reconstruct OrchestratedContext from dictionary."""
        intent_data = data.get("intent", {})
        intent = (
            IntentAnalysisResult.from_dict(intent_data)
            if isinstance(intent_data, dict)
            else IntentAnalysisResult(
                primary_intent=QueryIntent.EXPLANATION, confidence=0.0
            )
        )
        results = [
            HybridRetrievalResult.from_dict(r)
            for r in data.get("retrieved_results", [])
            if isinstance(r, dict)
        ]
        return cls(
            context_text=str(data.get("context_text", "")),
            query=str(data.get("query", "")),
            user_id=str(data.get("user_id", "")),
            repository_id=str(data.get("repository_id", "")),
            intent=intent,
            token_budget=data.get("token_budget"),
            estimated_tokens=int(data.get("estimated_tokens", 0)),
            retrieved_results=results,
            retrieval_metadata=dict(data.get("retrieval_metadata", {})),
        )


@dataclass
class RankingWeights:
    """Configurable weights for scoring candidate context items.

    Each weight determines the relative contribution of that signal to the final composite score.
    """

    semantic_relevance: float = 0.25
    lexical_relevance: float = 0.20
    graph_relevance: float = 0.15
    freshness: float = 0.10
    confidence: float = 0.10
    repository_match: float = 0.10
    source_reliability: float = 0.05
    token_cost: float = 0.05

    def to_dict(self) -> dict[str, float]:
        """Serialize weights to dictionary."""
        return {
            "semantic_relevance": round(self.semantic_relevance, 4),
            "lexical_relevance": round(self.lexical_relevance, 4),
            "graph_relevance": round(self.graph_relevance, 4),
            "freshness": round(self.freshness, 4),
            "confidence": round(self.confidence, 4),
            "repository_match": round(self.repository_match, 4),
            "source_reliability": round(self.source_reliability, 4),
            "token_cost": round(self.token_cost, 4),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RankingWeights:
        """Reconstruct RankingWeights from dictionary representation."""
        return cls(
            semantic_relevance=float(data.get("semantic_relevance", 0.25)),
            lexical_relevance=float(data.get("lexical_relevance", 0.20)),
            graph_relevance=float(data.get("graph_relevance", 0.15)),
            freshness=float(data.get("freshness", 0.10)),
            confidence=float(data.get("confidence", 0.10)),
            repository_match=float(data.get("repository_match", 0.10)),
            source_reliability=float(data.get("source_reliability", 0.05)),
            token_cost=float(data.get("token_cost", 0.05)),
        )

    def normalized(self) -> RankingWeights:
        """Return normalized weights where all non-negative weights sum to 1.0."""
        d = self.to_dict()
        total = sum(max(0.0, v) for v in d.values())
        if total <= 0.0:
            return RankingWeights()
        return RankingWeights(**{k: max(0.0, v) / total for k, v in d.items()})


@dataclass
class CandidateRankingScore:
    """Detailed score breakdown for a single candidate context item across all 8 ranking signals."""

    total_score: float
    semantic_relevance: float
    lexical_relevance: float
    graph_relevance: float
    freshness: float
    confidence: float
    repository_match: float
    source_reliability: float
    token_cost: float
    weights: dict[str, float] = field(default_factory=dict)
    explanation: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize candidate ranking score to dictionary."""
        return {
            "total_score": round(self.total_score, 4),
            "semantic_relevance": round(self.semantic_relevance, 4),
            "lexical_relevance": round(self.lexical_relevance, 4),
            "graph_relevance": round(self.graph_relevance, 4),
            "freshness": round(self.freshness, 4),
            "confidence": round(self.confidence, 4),
            "repository_match": round(self.repository_match, 4),
            "source_reliability": round(self.source_reliability, 4),
            "token_cost": round(self.token_cost, 4),
            "weights": {k: round(v, 4) for k, v in self.weights.items()},
            "explanation": self.explanation,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CandidateRankingScore:
        """Reconstruct CandidateRankingScore from dictionary."""
        return cls(
            total_score=float(data.get("total_score", 0.0)),
            semantic_relevance=float(data.get("semantic_relevance", 0.0)),
            lexical_relevance=float(data.get("lexical_relevance", 0.0)),
            graph_relevance=float(data.get("graph_relevance", 0.0)),
            freshness=float(data.get("freshness", 0.0)),
            confidence=float(data.get("confidence", 0.0)),
            repository_match=float(data.get("repository_match", 0.0)),
            source_reliability=float(data.get("source_reliability", 0.0)),
            token_cost=float(data.get("token_cost", 0.0)),
            weights=dict(data.get("weights", {})),
            explanation=str(data.get("explanation", "")),
        )


@dataclass
class OptimizedItemRecord:
    """Record of a context item retained after token optimization."""

    entity: str
    type: str
    file_or_document: str
    tier: str  # "full", "compact", "minimal"
    tokens: int
    utility: float
    utility_per_token: float
    relevance_information: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize retained item record to dictionary."""
        return {
            "entity": self.entity,
            "type": self.type,
            "file_or_document": self.file_or_document,
            "tier": self.tier,
            "tokens": self.tokens,
            "utility": round(self.utility, 4),
            "utility_per_token": round(self.utility_per_token, 4),
            "relevance_information": self.relevance_information,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OptimizedItemRecord:
        """Reconstruct record from dictionary representation."""
        return cls(
            entity=str(data.get("entity", "")),
            type=str(data.get("type", "")),
            file_or_document=str(data.get("file_or_document", "")),
            tier=str(data.get("tier", "full")),
            tokens=int(data.get("tokens", 0)),
            utility=float(data.get("utility", 0.0)),
            utility_per_token=float(data.get("utility_per_token", 0.0)),
            relevance_information=str(data.get("relevance_information", "")),
        )


@dataclass
class RemovedItemRecord:
    """Record of a candidate item removed during token optimization."""

    entity: str
    type: str
    file_or_document: str
    reason: str  # "budget_exceeded", "redundant_content", "subsumed"
    original_tokens: int
    utility: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize removed item record to dictionary."""
        return {
            "entity": self.entity,
            "type": self.type,
            "file_or_document": self.file_or_document,
            "reason": self.reason,
            "original_tokens": self.original_tokens,
            "utility": round(self.utility, 4),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RemovedItemRecord:
        """Reconstruct removed item record from dictionary representation."""
        return cls(
            entity=str(data.get("entity", "")),
            type=str(data.get("type", "")),
            file_or_document=str(data.get("file_or_document", "")),
            reason=str(data.get("reason", "unknown")),
            original_tokens=int(data.get("original_tokens", 0)),
            utility=float(data.get("utility", 0.0)),
        )


@dataclass
class TokenOptimizationResult:
    """Output from the Token Optimization Engine for Synapse."""

    optimized_context: str
    estimated_input_tokens: int
    original_estimated_tokens: int
    tokens_saved: int
    percentage_reduction: float
    items_removed: list[dict[str, Any]] = field(default_factory=list)
    items_retained: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize token optimization result to dictionary."""
        return {
            "optimized_context": self.optimized_context,
            "estimated_input_tokens": self.estimated_input_tokens,
            "original_estimated_tokens": self.original_estimated_tokens,
            "tokens_saved": self.tokens_saved,
            "percentage_reduction": round(self.percentage_reduction, 2),
            "items_removed": list(self.items_removed),
            "items_retained": list(self.items_retained),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenOptimizationResult:
        """Reconstruct token optimization result from dictionary representation."""
        return cls(
            optimized_context=str(data.get("optimized_context", "")),
            estimated_input_tokens=int(data.get("estimated_input_tokens", 0)),
            original_estimated_tokens=int(data.get("original_estimated_tokens", 0)),
            tokens_saved=int(data.get("tokens_saved", 0)),
            percentage_reduction=float(data.get("percentage_reduction", 0.0)),
            items_removed=list(data.get("items_removed", [])),
            items_retained=list(data.get("items_retained", [])),
            metadata=dict(data.get("metadata", {})),
        )



