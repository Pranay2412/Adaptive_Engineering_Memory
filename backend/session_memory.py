"""AI Session Memory subsystem for Adaptive Engineering Memory.

Provides APIs for recording AI-assisted development sessions, an intelligent
summarization layer that distills raw session interactions into reusable engineering
knowledge, and seamless retrieval integration for the Adaptive Context Orchestrator (ACO).
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from typing import Any

from .db_loader import Neo4jMemoryStore
from .models import (
    AISessionRecord,
    EngineeringDecision,
    HybridRetrievalResult,
    Memory,
    MemoryCategory,
    MemoryProvenance,
    MemoryScope,
    RecordSessionResponse,
    ResultProvenance,
    utc_now_iso,
)
from .team_memory_store import TeamMemoryDocumentStore

logger = logging.getLogger(__name__)


class AISessionKnowledgeSummarizer:
    """Summarization layer converting raw session information into reusable engineering knowledge.

    Filters out conversational pleasantries, iterative syntax trial-and-error, and banter,
    distilling actionable architectural decisions, root causes, refactors, and solutions.
    """

    FILE_REGEX = re.compile(r"[\w\-\./]+\.(?:py|ts|js|jsx|tsx|go|rs|java|cpp|c|h|sql|json|yaml|yml|md)")
    SYMBOL_REGEX = re.compile(r"\b(?:class|def|function|interface|type)?\s*([A-Z][a-zA-Z0-9_]{2,}|[a-z_]{3,}\(\))\b")

    def __init__(
        self,
        custom_summarizer: Callable[[AISessionRecord, list[dict[str, Any]] | None], list[Memory]] | None = None,
    ) -> None:
        self.custom_summarizer = custom_summarizer

    def summarize(
        self,
        record: AISessionRecord,
        raw_messages: list[dict[str, Any]] | None = None,
    ) -> tuple[list[Memory], list[EngineeringDecision]]:
        """Distill an AISessionRecord into structured Memory and EngineeringDecision objects."""
        if self.custom_summarizer is not None:
            memories = self.custom_summarizer(record, raw_messages)
            return memories, []

        # Combine text for signal analysis
        raw_text_parts = [record.prompt, record.response_summary]
        if record.engineering_decisions:
            raw_text_parts.extend(record.engineering_decisions)
        if raw_messages:
            for m in raw_messages:
                if isinstance(m, dict) and "content" in m:
                    raw_text_parts.append(str(m["content"]))

        combined_text = "\n".join(raw_text_parts)
        lowered = combined_text.lower()

        # Extract files and symbols
        detected_files = set(record.affected_files)
        for f in self.FILE_REGEX.findall(combined_text):
            detected_files.add(f)
        all_files = sorted(list(detected_files))[:15]

        detected_symbols = set(record.affected_symbols)
        for s in self.SYMBOL_REGEX.findall(combined_text):
            clean_s = s.replace("()", "").strip()
            if len(clean_s) > 3:
                detected_symbols.add(clean_s)
        all_symbols = sorted(list(detected_symbols))[:15]

        # Classify engineering category
        category = self._detect_category(lowered, record.engineering_decisions)

        # Build clean headline/title
        title = self._build_title(record, category)

        # Distill core content
        content = self._build_distilled_content(record, category, combined_text)

        # Actionable takeaways
        takeaways = self._extract_actionable_takeaways(record, category, all_symbols, all_files)

        # Build EngineeringDecision entities if decisions provided
        decisions: list[EngineeringDecision] = []
        primary_decision_id: str | None = None

        if record.engineering_decisions:
            for idx, dec_text in enumerate(record.engineering_decisions):
                dec_id = f"dec_{record.session_id[:8]}_{idx}_{uuid.uuid4().hex[:4]}"
                if primary_decision_id is None:
                    primary_decision_id = dec_id
                dec_obj = EngineeringDecision(
                    id=dec_id,
                    title=dec_text[:120],
                    rationale=f"Established in AI session {record.session_id}: {record.response_summary[:200]}",
                    status="accepted",
                    category="architecture" if category == MemoryCategory.ARCHITECTURE_DECISION.value else category,
                    scope=record.visibility,
                    author_id=record.user_id,
                    team_id=record.team_id,
                    repository_id=record.repository_id,
                    created_at=record.timestamp,
                )
                decisions.append(dec_obj)
        elif category == MemoryCategory.ARCHITECTURE_DECISION.value:
            # Create decision from prompt/summary
            dec_id = f"dec_{record.session_id[:8]}_{uuid.uuid4().hex[:4]}"
            primary_decision_id = dec_id
            dec_obj = EngineeringDecision(
                id=dec_id,
                title=title.replace("Architecture Decision: ", "")[:120],
                rationale=record.response_summary[:300] or record.prompt,
                status="accepted",
                category="architecture",
                scope=record.visibility,
                author_id=record.user_id,
                team_id=record.team_id,
                repository_id=record.repository_id,
                created_at=record.timestamp,
            )
            decisions.append(dec_obj)

        # Construct Memory card
        memory_id = f"mem_sess_{record.session_id[:8]}_{uuid.uuid4().hex[:6]}"
        provenance = MemoryProvenance(
            source_type="ai_session",
            source_id=record.session_id,
            file_paths=all_files,
            symbol_names=all_symbols,
            confidence=0.95,
            created_at=record.timestamp,
        )

        memory = Memory(
            id=memory_id,
            title=title,
            content=content,
            category=category,
            scope=record.visibility,
            author_id=record.user_id,
            team_id=record.team_id,
            repository_id=record.repository_id,
            session_id=record.session_id,
            decision_id=primary_decision_id,
            impact_areas=all_files,
            actionable_takeaways=takeaways,
            provenance=provenance,
            confidence=0.95,
            created_at=record.timestamp,
            updated_at=record.timestamp,
            metadata={
                "session_id": record.session_id,
                "prompt": record.prompt,
                "engineering_decisions": record.engineering_decisions,
            },
        )

        return [memory], decisions

    def _detect_category(self, lowered: str, decisions: list[str]) -> str:
        """Heuristically identify the primary engineering knowledge category."""
        if decisions or any(w in lowered for w in ["architecture", "adr", "trade-off", "tradeoff", "decided to", "choice between", "design pattern"]):
            return MemoryCategory.ARCHITECTURE_DECISION.value
        if any(w in lowered for w in ["bug", "root cause", "race condition", "deadlock", "memory leak", "segfault", "null pointer", "fix", "regression"]):
            return MemoryCategory.BUG_ROOT_CAUSE.value
        if any(w in lowered for w in ["api", "endpoint", "contract", "payload", "graphql", "rest", "grpc", "schema change"]):
            return MemoryCategory.API_DECISION.value
        if any(w in lowered for w in ["refactor", "migrated", "extracted service", "clean architecture", "restructure", "technical debt"]):
            return MemoryCategory.IMPORTANT_REFACTOR.value
        if any(w in lowered for w in ["deploy", "docker", "kubernetes", "k8s", "ci/cd", "env var", "production outage", "rollback"]):
            return MemoryCategory.DEPLOYMENT_LESSON.value
        if any(w in lowered for w in ["solution", "pattern", "recipe", "best practice", "convention", "idiom"]):
            return MemoryCategory.RECURRING_SOLUTION.value
        return MemoryCategory.IMPLEMENTATION_SUMMARY.value

    def _build_title(self, record: AISessionRecord, category: str) -> str:
        """Construct a clean, professional headline for the distilled memory card."""
        prefix_map = {
            MemoryCategory.ARCHITECTURE_DECISION.value: "Architecture Decision",
            MemoryCategory.BUG_ROOT_CAUSE.value: "Bug Root Cause",
            MemoryCategory.API_DECISION.value: "API Decision",
            MemoryCategory.IMPORTANT_REFACTOR.value: "Important Refactor",
            MemoryCategory.DEPLOYMENT_LESSON.value: "Deployment Lesson",
            MemoryCategory.RECURRING_SOLUTION.value: "Recurring Solution",
            MemoryCategory.IMPLEMENTATION_SUMMARY.value: "Implementation Summary",
        }
        prefix = prefix_map.get(category, "Engineering Knowledge")

        # Pick the most informative headline source
        if record.engineering_decisions:
            core = record.engineering_decisions[0]
        elif record.prompt:
            core = record.prompt
        else:
            core = record.response_summary

        # Clean punctuation and truncate
        cleaned = re.sub(r"^(?:please\s+|can\s+you\s+|how\s+to\s+|fix\s+|implement\s+)", "", core.strip(), flags=re.IGNORECASE)
        cleaned = cleaned.rstrip(".?!").strip()
        if len(cleaned) > 75:
            cleaned = cleaned[:72] + "..."
        if cleaned:
            cleaned = cleaned[0].upper() + cleaned[1:]

        return f"{prefix}: {cleaned}"

    def _build_distilled_content(self, record: AISessionRecord, category: str, combined_text: str) -> str:
        """Synthesize structured technical content without conversational fluff."""
        sections = []

        # Goal / Prompt context
        clean_prompt = record.prompt.strip()
        if clean_prompt:
            sections.append(f"Context & Objective:\n{clean_prompt}")

        # Decisions / Technical Resolution
        if record.engineering_decisions:
            dec_list = "\n".join(f"- {d}" for d in record.engineering_decisions)
            sections.append(f"Key Engineering Decisions:\n{dec_list}")

        # Response Summary / Implementation Details
        clean_resp = record.response_summary.strip()
        if clean_resp:
            sections.append(f"Technical Summary & Rationale:\n{clean_resp}")

        return "\n\n".join(sections)

    def _extract_actionable_takeaways(
        self,
        record: AISessionRecord,
        category: str,
        symbols: list[str],
        files: list[str],
    ) -> list[str]:
        """Derive actionable guidance for future developer queries."""
        takeaways = []
        if record.engineering_decisions:
            for d in record.engineering_decisions[:3]:
                takeaways.append(f"Enforce decision: {d}")

        if category == MemoryCategory.BUG_ROOT_CAUSE.value:
            takeaways.append("Validate boundary conditions to prevent defect recurrence.")
        elif category == MemoryCategory.API_DECISION.value:
            takeaways.append("Preserve backward compatibility across client callers.")

        if symbols:
            takeaways.append(f"Affected symbols: {', '.join(symbols[:3])}")
        if files:
            takeaways.append(f"Key files touched: {', '.join(files[:3])}")

        return takeaways[:4]


class AISessionMemoryService:
    """Primary API service for recording AI sessions and retrieving them via ACO."""

    def __init__(
        self,
        doc_store: TeamMemoryDocumentStore | None = None,
        neo4j_store: Neo4jMemoryStore | None = None,
        summarizer: AISessionKnowledgeSummarizer | None = None,
    ) -> None:
        self.doc_store = doc_store or TeamMemoryDocumentStore()
        self.neo4j_store = neo4j_store
        self.summarizer = summarizer or AISessionKnowledgeSummarizer()

    def record_session(
        self,
        session_id: str,
        user_id: str,
        repository_id: str,
        prompt: str,
        response_summary: str,
        *,
        affected_files: list[str] | None = None,
        affected_symbols: list[str] | None = None,
        engineering_decisions: list[str] | None = None,
        timestamp: str | None = None,
        visibility: str = "repository",
        team_id: str | None = None,
        raw_messages: list[dict[str, Any]] | None = None,
        store_raw_transcript: bool = False,
        auto_summarize: bool = True,
        sync_neo4j: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> RecordSessionResponse:
        """Record an AI-assisted development session.

        - Stores: session_id, user_id, repository_id, prompt, response summary,
          affected files, affected symbols, engineering decisions, timestamp, visibility, team_id.
        - Does NOT store unnecessary full conversation data by default (store_raw_transcript=False).
        - Executes summarization layer to convert session into reusable engineering knowledge.
        - Synchronizes with Neo4j graph if enabled.
        """
        # Validate inputs
        if not session_id or not session_id.strip():
            raise ValueError("session_id is required.")
        if not user_id or not user_id.strip():
            raise ValueError("user_id is required.")
        if not repository_id or not repository_id.strip():
            raise ValueError("repository_id is required.")
        if not prompt or not prompt.strip():
            raise ValueError("prompt is required.")
        if not response_summary or not response_summary.strip():
            raise ValueError("response_summary is required.")

        clean_visibility = visibility.lower() if visibility else "repository"
        if clean_visibility not in (MemoryScope.PERSONAL.value, MemoryScope.TEAM.value, MemoryScope.REPOSITORY.value):
            clean_visibility = "repository"

        ts = timestamp or utc_now_iso()

        # Build session record. raw_transcript is strictly None unless store_raw_transcript is True!
        record = AISessionRecord(
            session_id=session_id.strip(),
            user_id=user_id.strip(),
            repository_id=repository_id.strip(),
            prompt=prompt.strip(),
            response_summary=response_summary.strip(),
            affected_files=list(affected_files or []),
            affected_symbols=list(affected_symbols or []),
            engineering_decisions=list(engineering_decisions or []),
            timestamp=ts,
            visibility=clean_visibility,
            team_id=team_id.strip() if team_id else None,
            raw_transcript=raw_messages if store_raw_transcript else None,
            metadata=dict(metadata or {}),
        )

        # 1. Save Session Record in Document Store
        self.doc_store.save_session_record(record)

        distilled_memories: list[Memory] = []
        decisions: list[EngineeringDecision] = []

        # 2. Convert raw session information into reusable engineering knowledge
        if auto_summarize:
            distilled_memories, decisions = self.summarizer.summarize(record, raw_messages=raw_messages)

            # Persist decisions and memories in document store
            for dec in decisions:
                self.doc_store.save_decision(dec)

            for mem in distilled_memories:
                self.doc_store.save_memory(
                    mem,
                    changed_by=user_id,
                    change_reason=f"AI Session Distillation: {session_id}",
                )

        # 3. Synchronize to Neo4j graph if enabled
        if sync_neo4j and self.neo4j_store:
            try:
                self.neo4j_store.upsert_ai_sessions([record])
                if decisions:
                    self.neo4j_store.upsert_engineering_decisions(decisions)
                if distilled_memories:
                    self.neo4j_store.upsert_memories(distilled_memories)
            except Exception as e:
                logger.warning("Neo4j graph synchronization failed during record_session: %s", e)

        return RecordSessionResponse(
            session=record,
            distilled_memories=distilled_memories,
            decisions=decisions,
            status="recorded",
            message=f"Session {session_id} recorded and {len(distilled_memories)} engineering memory cards distilled.",
        )

    def get_session(self, session_id: str) -> AISessionRecord | None:
        """Retrieve recorded AI session by ID."""
        return self.doc_store.get_session_record(session_id)

    def list_sessions(
        self,
        *,
        user_id: str | None = None,
        repository_id: str | None = None,
        team_id: str | None = None,
        visibility: str | None = None,
        limit: int = 50,
    ) -> list[AISessionRecord]:
        """List recorded AI sessions with filtering."""
        return self.doc_store.list_session_records(
            user_id=user_id,
            repository_id=repository_id,
            team_id=team_id,
            visibility=visibility,
            limit=limit,
        )

    def get_session_memories(self, session_id: str) -> list[Memory]:
        """Get all distilled engineering memories originating from a session."""
        cursor = self.doc_store._conn.execute(
            "SELECT * FROM memories WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        )
        return [self.doc_store._row_to_memory(r) for r in cursor.fetchall()]

    def retrieve_for_aco(
        self,
        query: str,
        user_id: str,
        repository_id: str,
        *,
        team_id: str | None = None,
        limit: int = 10,
    ) -> list[HybridRetrievalResult]:
        """Retrieve distilled session memories scored for consumption by the Adaptive Context Orchestrator."""
        user_team_ids = self.doc_store.get_user_teams(user_id)
        memories = self.doc_store.query_memories(
            user_id=user_id,
            team_id=team_id,
            user_team_ids=user_team_ids,
            repository_id=repository_id,
            query=query,
            limit=limit,
        )

        query_terms = [t.lower() for t in query.split() if len(t) > 2]
        results: list[HybridRetrievalResult] = []

        for m in memories:
            m_text = f"{m.title} {m.content}".lower()
            matches = sum(1 for t in query_terms if t in m_text)
            term_score = matches / max(1, len(query_terms))
            # Hybrid score incorporating match density and confidence
            final_score = round(0.55 + 0.45 * term_score * m.confidence, 4)

            prov = ResultProvenance(
                source_type="session_memory",
                repository_id=repository_id,
                created_at=m.created_at,
                channels=["session_memory", "lexical"],
                matched_terms=[t for t in query_terms if t in m_text],
            )

            hybrid_item = HybridRetrievalResult(
                entity=m.title,
                type=f"Memory ({m.category})",
                source="session_memory",
                repository=m.repository_id or repository_id,
                file_or_document=m.repository_id or "session_memory",
                content_snippet=m.content,
                node_id=m.id,
                confidence=m.confidence,
                score=final_score,
                scores_breakdown={
                    "lexical": round(term_score, 4),
                    "confidence": m.confidence,
                    "session_affinity": 0.90,
                },
                provenance=prov,
                metadata={
                    "session_id": m.session_id,
                    "memory_id": m.id,
                    "category": m.category,
                    "scope": m.scope,
                    "impact_areas": m.impact_areas,
                    "actionable_takeaways": m.actionable_takeaways,
                    "engineering_decisions": [m.title] if "decision" in m.category.lower() else [],
                },
            )
            results.append(hybrid_item)

        results.sort(key=lambda r: r.score, reverse=True)
        return results


# ---------------------------------------------------------------------------
# Standalone JSON/REST-compatible API Endpoints
# ---------------------------------------------------------------------------

_GLOBAL_SESSION_SERVICE: AISessionMemoryService | None = None


def get_default_session_service() -> AISessionMemoryService:
    global _GLOBAL_SESSION_SERVICE
    if _GLOBAL_SESSION_SERVICE is None:
        _GLOBAL_SESSION_SERVICE = AISessionMemoryService()
    return _GLOBAL_SESSION_SERVICE


def record_ai_session_api(
    payload: dict[str, Any],
    service: AISessionMemoryService | None = None,
) -> dict[str, Any]:
    """JSON API handler for recording an AI session.

    Accepts dictionary matching session recording schema and returns JSON response.
    """
    svc = service or get_default_session_service()
    res = svc.record_session(
        session_id=str(payload["session_id"]),
        user_id=str(payload["user_id"]),
        repository_id=str(payload["repository_id"]),
        prompt=str(payload["prompt"]),
        response_summary=str(payload["response_summary"]),
        affected_files=payload.get("affected_files"),
        affected_symbols=payload.get("affected_symbols"),
        engineering_decisions=payload.get("engineering_decisions"),
        timestamp=payload.get("timestamp"),
        visibility=payload.get("visibility", "repository"),
        team_id=payload.get("team_id"),
        raw_messages=payload.get("raw_messages"),
        store_raw_transcript=bool(payload.get("store_raw_transcript", False)),
        auto_summarize=bool(payload.get("auto_summarize", True)),
        sync_neo4j=bool(payload.get("sync_neo4j", False)),
        metadata=payload.get("metadata"),
    )
    return res.to_dict()


def get_ai_session_api(
    session_id: str,
    service: AISessionMemoryService | None = None,
) -> dict[str, Any] | None:
    """JSON API handler for getting a recorded AI session by ID."""
    svc = service or get_default_session_service()
    record = svc.get_session(session_id)
    return record.to_dict() if record else None


def query_ai_sessions_api(
    params: dict[str, Any],
    service: AISessionMemoryService | None = None,
) -> list[dict[str, Any]]:
    """JSON API handler for querying recorded AI sessions."""
    svc = service or get_default_session_service()
    sessions = svc.list_sessions(
        user_id=params.get("user_id"),
        repository_id=params.get("repository_id"),
        team_id=params.get("team_id"),
        visibility=params.get("visibility"),
        limit=int(params.get("limit", 50)),
    )
    return [s.to_dict() for s in sessions]
