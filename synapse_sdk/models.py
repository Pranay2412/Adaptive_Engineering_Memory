"""Typed Response and Domain Models for Synapse Python SDK (Phase 11).

Provides immutable, typed dataclasses with serialization and deserialization
helpers for all Synapse API operations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class HealthStatus:
    """Service liveness status response."""
    status: str
    service: str
    version: str
    mode: str
    timestamp: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HealthStatus:
        return cls(
            status=data.get("status", "unknown"),
            service=data.get("service", ""),
            version=data.get("version", ""),
            mode=data.get("mode", ""),
            timestamp=data.get("timestamp", ""),
        )


@dataclass(frozen=True)
class ReadinessStatus:
    """Service readiness status response."""
    status: str
    service: str
    checks: dict[str, str]
    timestamp: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReadinessStatus:
        return cls(
            status=data.get("status", "unknown"),
            service=data.get("service", ""),
            checks=data.get("checks", {}),
            timestamp=data.get("timestamp", ""),
        )


@dataclass(frozen=True)
class SessionInfo:
    """Active or resumed development session."""
    session_id: str
    repository_id: str
    status: str
    created_at: str
    ttl_seconds: int = 86400
    title: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    closed_at: str | None = None
    close_reason: str | None = None
    summary: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionInfo:
        repo = data.get("repository", {})
        repo_id = repo.get("repository_id") if isinstance(repo, dict) else str(repo)
        return cls(
            session_id=data.get("session_id", ""),
            repository_id=repo_id or "",
            status=data.get("status", "unknown"),
            created_at=data.get("created_at", ""),
            ttl_seconds=data.get("ttl_seconds", 86400),
            title=data.get("title", ""),
            metadata=data.get("metadata", {}),
            closed_at=data.get("closed_at"),
            close_reason=data.get("close_reason"),
            summary=data.get("summary"),
        )


@dataclass(frozen=True)
class ContextItem:
    """Individual item packed into orchestrated context."""
    id: str
    type: str
    source: str
    content: str
    tokens: int
    score: float
    provenance: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContextItem:
        return cls(
            id=data.get("id", data.get("symbol_name", data.get("source", ""))),
            type=data.get("type", data.get("category", "unknown")),
            source=data.get("source", ""),
            content=data.get("content", data.get("source_excerpt", "")),
            tokens=data.get("tokens", data.get("token_count", 0)),
            score=float(data.get("score", data.get("relevance_score", 0.0))),
            provenance=data.get("provenance", data),
        )


@dataclass(frozen=True)
class ContextResult:
    """Orchestrated, token-budgeted context package from ACO."""
    query: str
    budget_allocated: int
    tokens_used: int
    budget_utilization: float
    items: list[ContextItem]
    raw_context: str
    timing_ms: float = 0.0
    orchestration_mode: str = "deterministic"
    items_packed: int = 0
    intent: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any], query: str = "") -> ContextResult:
        raw_items = data.get("items") or data.get("provenance") or []
        parsed_items = [ContextItem.from_dict(it) for it in raw_items] if isinstance(raw_items, list) else []
        budget = data.get("token_budget", data.get("budget_allocated", 0))
        used = data.get("tokens_used", 0)
        util = float(data.get("budget_utilization_pct", data.get("budget_utilization", 0.0)))
        return cls(
            query=query or data.get("query", ""),
            budget_allocated=budget,
            tokens_used=used,
            budget_utilization=util,
            items=parsed_items,
            raw_context=data.get("formatted_context", data.get("raw_context", "")),
            timing_ms=float(data.get("latency_ms", data.get("timing_ms", 0.0))),
            orchestration_mode=data.get("orchestration_mode", "deterministic"),
            items_packed=data.get("items_packed", len(parsed_items)),
            intent=data.get("intent", ""),
        )


@dataclass(frozen=True)
class DecisionRecord:
    """Persisted architectural or engineering decision."""
    decision_id: str
    status: str = "stored"
    scope: str = "repository"
    success: bool = True
    title: str = ""
    rationale: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any], title: str = "", rationale: str = "") -> DecisionRecord:
        return cls(
            decision_id=data.get("decision_id", ""),
            status=data.get("status", "stored"),
            scope=data.get("scope", "repository"),
            success=data.get("success", True),
            title=title or data.get("title", ""),
            rationale=rationale or data.get("rationale", ""),
        )


@dataclass(frozen=True)
class FactRecord:
    """Persisted technical recipe, recurring solution, or root-cause fact."""
    memory_id: str
    status: str = "stored"
    success: bool = True
    title: str = ""
    content: str = ""

    @property
    def fact_id(self) -> str:
        """Alias for memory_id."""
        return self.memory_id

    @classmethod
    def from_dict(cls, data: dict[str, Any], title: str = "", content: str = "") -> FactRecord:
        return cls(
            memory_id=data.get("memory_id", data.get("fact_id", "")),
            status=data.get("status", "stored"),
            success=data.get("success", True),
            title=title or data.get("title", ""),
            content=content or data.get("content", ""),
        )


@dataclass(frozen=True)
class DistillationResult:
    """Summary distillation execution results."""
    success: bool
    status: str
    distilled_memories_count: int
    memory_ids: list[str] = field(default_factory=list)
    decisions_recorded: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DistillationResult:
        return cls(
            success=data.get("success", False),
            status=data.get("status", "unknown"),
            distilled_memories_count=data.get("distilled_memories_count", 0),
            memory_ids=data.get("memory_ids", []),
            decisions_recorded=data.get("decisions_recorded", []),
        )


@dataclass(frozen=True)
class ScopedMemoryItem:
    """Memory item returned from scoped query."""
    memory_id: str
    title: str
    content: str
    scope: str
    category: str
    relevance_score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScopedMemoryItem:
        return cls(
            memory_id=data.get("memory_id", data.get("id", "")),
            title=data.get("title", ""),
            content=data.get("content", ""),
            scope=data.get("scope", "repository"),
            category=data.get("category", ""),
            relevance_score=float(data.get("relevance_score", data.get("score", 0.0))),
            metadata=data.get("metadata", {}),
        )


@dataclass(frozen=True)
class ScopedQueryResult:
    """Result of querying engineering memory across permitted scopes."""
    success: bool
    total_found: int
    memories: list[ScopedMemoryItem]
    scopes_included: list[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScopedQueryResult:
        raw_mems = data.get("memories", [])
        parsed_mems = [ScopedMemoryItem.from_dict(m) for m in raw_mems] if isinstance(raw_mems, list) else []
        return cls(
            success=data.get("success", False),
            total_found=data.get("total_found", len(parsed_mems)),
            memories=parsed_mems,
            scopes_included=data.get("scopes_included", []),
        )
