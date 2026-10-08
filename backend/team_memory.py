"""Unified Team Memory service for Adaptive Engineering Memory.

Orchestrates multi-tenant scoping (personal, team, repository), structured
knowledge distillation from AI sessions, immutable revision tracking, and
dual-persistence in document/relational storage and the Neo4j graph.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterable
from typing import Any

from .db_loader import Neo4jMemoryStore
from .models import (
    AISession,
    EngineeringDecision,
    EngineeringEvent,
    HybridRetrievalResult,
    Memory,
    MemoryCategory,
    MemoryProvenance,
    MemoryScope,
    Project,
    Repository,
    ResultProvenance,
    Team,
    TeamMembership,
    User,
    utc_now_iso,
)
from .team_memory_store import TeamMemoryDocumentStore


class SessionKnowledgeDistiller:
    """Extracts distilled, reusable engineering knowledge from AI sessions.

    Filters out conversational noise, syntax trials, and conversational banter,
    distilling actionable engineering decisions, root causes, refactors, and patterns.
    """

    # Regex patterns for identifying files and symbols mentioned in discussions
    FILE_REGEX = re.compile(r"[\w\-\./]+\.(?:py|ts|js|jsx|tsx|go|rs|java|cpp|c|h|sql|json|yaml|yml|md)")
    SYMBOL_REGEX = re.compile(r"\b(?:class|def|function|interface|type)?\s*([A-Z][a-zA-Z0-9_]{2,}|[a-z_]{3,}\(\))\b")

    def __init__(self, custom_extractor: Callable[[AISession, list[dict[str, Any]]], list[Memory]] | None = None) -> None:
        self.custom_extractor = custom_extractor

    def distill(
        self,
        session: AISession,
        messages: list[dict[str, Any]] | None = None,
        *,
        author_id: str | None = None,
        scope: str = "repository",
    ) -> list[Memory]:
        """Distill structured engineering memories from session metadata and messages."""
        if self.custom_extractor is not None:
            return self.custom_extractor(session, messages or [])

        effective_author = author_id or session.user_id or "system_distiller"
        messages = messages or []
        combined_text = f"{session.title}\n{session.summary}\n" + "\n".join(
            m.get("content", "") for m in messages if isinstance(m, dict)
        )

        extracted_files = list(set(self.FILE_REGEX.findall(combined_text)))[:10]
        extracted_symbols_raw = self.SYMBOL_REGEX.findall(combined_text)
        cleaned_symbols = list(
            {s.replace("()", "").strip() for s in extracted_symbols_raw if len(s.strip()) > 3}
        )[:10]

        memories: list[Memory] = []
        lowered = combined_text.lower()

        # 1. Architecture Decision check
        if any(w in lowered for w in ["decided to", "architecture", "trade-off", "adr", "architectural choice"]):
            mem_id = f"mem_arch_{session.id[:8]}_{uuid.uuid4().hex[:6]}"
            content = self._extract_relevant_sentences(
                combined_text, ["decided", "architecture", "choice", "trade-off", "design", "rationale"]
            ) or (session.summary or f"Architectural decision established during {session.title}")
            memories.append(
                Memory(
                    id=mem_id,
                    title=f"Architectural Decision: {session.title}",
                    content=content,
                    category=MemoryCategory.ARCHITECTURE_DECISION.value,
                    scope=scope,
                    author_id=effective_author,
                    team_id=session.team_id,
                    repository_id=session.repository_id,
                    session_id=session.id,
                    impact_areas=extracted_files,
                    actionable_takeaways=[
                        f"Adopted design consensus documented in session {session.id}",
                        f"Targeted modules: {', '.join(extracted_files[:3]) or 'Core codebase'}",
                    ],
                    provenance=MemoryProvenance(
                        source_type="ai_session_distillation",
                        source_id=session.id,
                        file_paths=extracted_files,
                        symbol_names=cleaned_symbols,
                        confidence=0.92,
                    ),
                )
            )

        # 2. Bug Root Cause check
        if any(w in lowered for w in ["root cause", "fixed bug", "race condition", "deadlock", "memory leak", "null pointer", "regression"]):
            mem_id = f"mem_bug_{session.id[:8]}_{uuid.uuid4().hex[:6]}"
            content = self._extract_relevant_sentences(
                combined_text, ["cause", "root", "bug", "fix", "failed", "resolved", "error"]
            ) or (session.summary or f"Bug resolution and diagnostic analysis from {session.title}")
            memories.append(
                Memory(
                    id=mem_id,
                    title=f"Bug Root Cause & Resolution: {session.title}",
                    content=content,
                    category=MemoryCategory.BUG_ROOT_CAUSE.value,
                    scope=scope,
                    author_id=effective_author,
                    team_id=session.team_id,
                    repository_id=session.repository_id,
                    session_id=session.id,
                    impact_areas=extracted_files,
                    actionable_takeaways=[
                        "Prevent regression by validating boundary assertions",
                        f"Key affected symbols: {', '.join(cleaned_symbols[:3]) or 'Domain logic'}",
                    ],
                    provenance=MemoryProvenance(
                        source_type="ai_session_distillation",
                        source_id=session.id,
                        file_paths=extracted_files,
                        symbol_names=cleaned_symbols,
                        confidence=0.95,
                    ),
                )
            )

        # 3. Important Refactor check
        if any(w in lowered for w in ["refactor", "migrated", "extracted service", "cleaned up architecture", "restructured"]):
            mem_id = f"mem_refactor_{session.id[:8]}_{uuid.uuid4().hex[:6]}"
            content = self._extract_relevant_sentences(
                combined_text, ["refactor", "migrat", "restructur", "extract", "clean"]
            ) or (session.summary or f"Refactoring summary from {session.title}")
            memories.append(
                Memory(
                    id=mem_id,
                    title=f"Refactoring Summary: {session.title}",
                    content=content,
                    category=MemoryCategory.IMPORTANT_REFACTOR.value,
                    scope=scope,
                    author_id=effective_author,
                    team_id=session.team_id,
                    repository_id=session.repository_id,
                    session_id=session.id,
                    impact_areas=extracted_files,
                    actionable_takeaways=[
                        f"Refactored components: {', '.join(extracted_files[:3]) or 'Standard modules'}",
                    ],
                    provenance=MemoryProvenance(
                        source_type="ai_session_distillation",
                        source_id=session.id,
                        file_paths=extracted_files,
                        symbol_names=cleaned_symbols,
                        confidence=0.90,
                    ),
                )
            )

        # 4. Fallback: If no specialized category triggered, create an Implementation Summary
        if not memories and (session.summary or combined_text.strip()):
            mem_id = f"mem_impl_{session.id[:8]}_{uuid.uuid4().hex[:6]}"
            content = session.summary or self._extract_relevant_sentences(
                combined_text, ["implemented", "built", "created", "added", "updated", "solution"]
            ) or f"Implementation summary for session: {session.title}"
            memories.append(
                Memory(
                    id=mem_id,
                    title=f"Implementation Summary: {session.title}",
                    content=content,
                    category=MemoryCategory.IMPLEMENTATION_SUMMARY.value,
                    scope=scope,
                    author_id=effective_author,
                    team_id=session.team_id,
                    repository_id=session.repository_id,
                    session_id=session.id,
                    impact_areas=extracted_files,
                    actionable_takeaways=[
                        f"Implementation scope: {session.title}",
                    ],
                    provenance=MemoryProvenance(
                        source_type="ai_session_distillation",
                        source_id=session.id,
                        file_paths=extracted_files,
                        symbol_names=cleaned_symbols,
                        confidence=0.88,
                    ),
                )
            )

        return memories

    def _extract_relevant_sentences(self, text: str, keywords: list[str]) -> str:
        """Extract key sentences matching engineering knowledge keywords."""
        sentences = re.split(r"(?<=[.!?])\s+", text)
        matched = []
        for s in sentences:
            s_clean = s.strip()
            # Filter conversational greetings or noise
            if any(greeting in s_clean.lower() for greeting in ["hello", "hi there", "thanks", "you're welcome", "okay sure"]):
                continue
            if any(k in s_clean.lower() for k in keywords) and len(s_clean) > 20:
                matched.append(s_clean)
            if len(matched) >= 4:
                break
        return " ".join(matched)


class TeamMemoryService:
    """Unified service for capturing, scoping, retrieving, and synchronizing Team Memory."""

    def __init__(
        self,
        doc_store: TeamMemoryDocumentStore | None = None,
        neo4j_store: Neo4jMemoryStore | None = None,
        distiller: SessionKnowledgeDistiller | None = None,
    ) -> None:
        self.doc_store = doc_store or TeamMemoryDocumentStore()
        self.neo4j_store = neo4j_store
        self.distiller = distiller or SessionKnowledgeDistiller()

    # --- Multi-tenant Scoping Helpers ---

    def register_user(self, user: User) -> User:
        return self.doc_store.save_user(user)

    def register_team(self, team: Team) -> Team:
        return self.doc_store.save_team(team)

    def add_team_member(self, user_id: str, team_id: str, role: str = "member") -> TeamMembership:
        membership = TeamMembership(user_id=user_id, team_id=team_id, role=role, joined_at=utc_now_iso())
        return self.doc_store.add_team_membership(membership)

    def register_repository(self, repository: Repository) -> Repository:
        return self.doc_store.save_repository(repository)

    def register_project(self, project: Project) -> Project:
        return self.doc_store.save_project(project)

    # --- Structured Engineering Knowledge Recording ---

    def record_architecture_decision(
        self,
        title: str,
        rationale: str,
        *,
        author_id: str,
        team_id: str | None = None,
        repository_id: str | None = None,
        project_id: str | None = None,
        scope: str = "team",
        alternatives_considered: list[str] | None = None,
        trade_offs: list[str] | None = None,
        impact_areas: list[str] | None = None,
        actionable_takeaways: list[str] | None = None,
        file_paths: list[str] | None = None,
        symbol_names: list[str] | None = None,
        confidence: float = 1.0,
    ) -> Memory:
        """Capture an architectural or design decision with rationale and alternatives."""
        decision_id = f"dec_{uuid.uuid4().hex[:8]}"
        decision = EngineeringDecision(
            id=decision_id,
            title=title,
            rationale=rationale,
            status="accepted",
            category="architecture",
            scope=scope,
            author_id=author_id,
            team_id=team_id,
            repository_id=repository_id,
            project_id=project_id,
            alternatives_considered=alternatives_considered or [],
            trade_offs=trade_offs or [],
            created_at=utc_now_iso(),
        )
        self.doc_store.save_decision(decision)

        memory_id = f"mem_arch_{uuid.uuid4().hex[:8]}"
        prov = MemoryProvenance(
            source_type="architecture_decision",
            source_id=decision_id,
            file_paths=file_paths or [],
            symbol_names=symbol_names or [],
            confidence=confidence,
        )
        memory = Memory(
            id=memory_id,
            title=f"Architecture Decision: {title}",
            content=rationale,
            category=MemoryCategory.ARCHITECTURE_DECISION.value,
            scope=scope,
            author_id=author_id,
            team_id=team_id,
            repository_id=repository_id,
            project_id=project_id,
            decision_id=decision_id,
            impact_areas=impact_areas or (file_paths or []),
            actionable_takeaways=actionable_takeaways or [f"Implement agreed pattern: {title}"],
            provenance=prov,
            confidence=confidence,
        )
        saved = self.doc_store.save_memory(memory, changed_by=author_id, change_reason="Recorded architecture decision")

        if self.neo4j_store:
            try:
                self.neo4j_store.upsert_engineering_decisions([decision])
                self.neo4j_store.upsert_memories([saved])
            except Exception:
                pass
        return saved

    def record_bug_root_cause(
        self,
        title: str,
        root_cause: str,
        fix_summary: str,
        *,
        author_id: str,
        repository_id: str,
        team_id: str | None = None,
        scope: str = "repository",
        impact_areas: list[str] | None = None,
        actionable_takeaways: list[str] | None = None,
        file_paths: list[str] | None = None,
        symbol_names: list[str] | None = None,
        commit_shas: list[str] | None = None,
        event_ref: str | None = None,
        confidence: float = 1.0,
    ) -> Memory:
        """Capture root cause analysis and prevention takeaways for a non-trivial defect."""
        event_id = None
        if event_ref:
            event_id = f"evt_{uuid.uuid4().hex[:8]}"
            event = EngineeringEvent(
                id=event_id,
                event_type="incident_resolution",
                title=f"Bug Resolution: {title}",
                description=fix_summary,
                repository_id=repository_id,
                team_id=team_id,
                user_id=author_id,
                external_ref=event_ref,
            )
            self.doc_store.save_event(event)

        memory_id = f"mem_bug_{uuid.uuid4().hex[:8]}"
        content = f"Root Cause:\n{root_cause}\n\nResolution:\n{fix_summary}"
        prov = MemoryProvenance(
            source_type="bug_root_cause",
            source_id=event_id or event_ref or "",
            file_paths=file_paths or [],
            symbol_names=symbol_names or [],
            commit_shas=commit_shas or [],
            confidence=confidence,
        )
        memory = Memory(
            id=memory_id,
            title=f"Bug Root Cause: {title}",
            content=content,
            category=MemoryCategory.BUG_ROOT_CAUSE.value,
            scope=scope,
            author_id=author_id,
            team_id=team_id,
            repository_id=repository_id,
            event_id=event_id,
            impact_areas=impact_areas or (file_paths or []),
            actionable_takeaways=actionable_takeaways or ["Guard against this defect in subsequent PR reviews"],
            provenance=prov,
            confidence=confidence,
        )
        saved = self.doc_store.save_memory(memory, changed_by=author_id, change_reason="Recorded bug root cause")

        if self.neo4j_store:
            try:
                self.neo4j_store.upsert_memories([saved])
            except Exception:
                pass
        return saved

    def record_implementation_summary(
        self,
        title: str,
        summary: str,
        *,
        author_id: str,
        repository_id: str,
        team_id: str | None = None,
        scope: str = "repository",
        impact_areas: list[str] | None = None,
        actionable_takeaways: list[str] | None = None,
        file_paths: list[str] | None = None,
        symbol_names: list[str] | None = None,
        confidence: float = 1.0,
    ) -> Memory:
        """Capture high-level summary of a complex feature or module implementation."""
        memory_id = f"mem_impl_{uuid.uuid4().hex[:8]}"
        prov = MemoryProvenance(
            source_type="implementation_summary",
            source_id="",
            file_paths=file_paths or [],
            symbol_names=symbol_names or [],
            confidence=confidence,
        )
        memory = Memory(
            id=memory_id,
            title=title,
            content=summary,
            category=MemoryCategory.IMPLEMENTATION_SUMMARY.value,
            scope=scope,
            author_id=author_id,
            team_id=team_id,
            repository_id=repository_id,
            impact_areas=impact_areas or (file_paths or []),
            actionable_takeaways=actionable_takeaways or [],
            provenance=prov,
            confidence=confidence,
        )
        saved = self.doc_store.save_memory(memory, changed_by=author_id, change_reason="Recorded implementation summary")
        if self.neo4j_store:
            try:
                self.neo4j_store.upsert_memories([saved])
            except Exception:
                pass
        return saved

    def record_api_decision(
        self,
        title: str,
        contract_change: str,
        rationale: str,
        *,
        author_id: str,
        repository_id: str,
        team_id: str | None = None,
        scope: str = "team",
        impact_areas: list[str] | None = None,
        actionable_takeaways: list[str] | None = None,
        file_paths: list[str] | None = None,
        symbol_names: list[str] | None = None,
        confidence: float = 1.0,
    ) -> Memory:
        """Capture API contract modifications, breaking changes, or versioning agreements."""
        memory_id = f"mem_api_{uuid.uuid4().hex[:8]}"
        content = f"Contract Specification:\n{contract_change}\n\nDesign Rationale:\n{rationale}"
        prov = MemoryProvenance(
            source_type="api_decision",
            source_id="",
            file_paths=file_paths or [],
            symbol_names=symbol_names or [],
            confidence=confidence,
        )
        memory = Memory(
            id=memory_id,
            title=f"API Decision: {title}",
            content=content,
            category=MemoryCategory.API_DECISION.value,
            scope=scope,
            author_id=author_id,
            team_id=team_id,
            repository_id=repository_id,
            impact_areas=impact_areas or (file_paths or []),
            actionable_takeaways=actionable_takeaways or ["Maintain backward compatibility in client SDKs"],
            provenance=prov,
            confidence=confidence,
        )
        saved = self.doc_store.save_memory(memory, changed_by=author_id, change_reason="Recorded API decision")
        if self.neo4j_store:
            try:
                self.neo4j_store.upsert_memories([saved])
            except Exception:
                pass
        return saved

    def record_important_refactor(
        self,
        title: str,
        motivation: str,
        changes_made: str,
        *,
        author_id: str,
        repository_id: str,
        team_id: str | None = None,
        scope: str = "repository",
        impact_areas: list[str] | None = None,
        actionable_takeaways: list[str] | None = None,
        file_paths: list[str] | None = None,
        symbol_names: list[str] | None = None,
        confidence: float = 1.0,
    ) -> Memory:
        """Capture structural refactoring details and architectural intentions."""
        memory_id = f"mem_refactor_{uuid.uuid4().hex[:8]}"
        content = f"Motivation:\n{motivation}\n\nStructural Changes:\n{changes_made}"
        prov = MemoryProvenance(
            source_type="important_refactor",
            source_id="",
            file_paths=file_paths or [],
            symbol_names=symbol_names or [],
            confidence=confidence,
        )
        memory = Memory(
            id=memory_id,
            title=f"Refactor: {title}",
            content=content,
            category=MemoryCategory.IMPORTANT_REFACTOR.value,
            scope=scope,
            author_id=author_id,
            team_id=team_id,
            repository_id=repository_id,
            impact_areas=impact_areas or (file_paths or []),
            actionable_takeaways=actionable_takeaways or ["Adopt new interfaces across remaining legacy callers"],
            provenance=prov,
            confidence=confidence,
        )
        saved = self.doc_store.save_memory(memory, changed_by=author_id, change_reason="Recorded important refactor")
        if self.neo4j_store:
            try:
                self.neo4j_store.upsert_memories([saved])
            except Exception:
                pass
        return saved

    def record_deployment_lesson(
        self,
        title: str,
        lesson: str,
        incident_summary: str,
        *,
        author_id: str,
        team_id: str | None = None,
        repository_id: str | None = None,
        scope: str = "team",
        impact_areas: list[str] | None = None,
        actionable_takeaways: list[str] | None = None,
        file_paths: list[str] | None = None,
        confidence: float = 1.0,
    ) -> Memory:
        """Capture deployment experiences, configuration nuances, or outage lessons."""
        memory_id = f"mem_deploy_{uuid.uuid4().hex[:8]}"
        content = f"Incident/Release Summary:\n{incident_summary}\n\nKey Deployment Lesson:\n{lesson}"
        prov = MemoryProvenance(
            source_type="deployment_lesson",
            source_id="",
            file_paths=file_paths or [],
            confidence=confidence,
        )
        memory = Memory(
            id=memory_id,
            title=f"Deployment Lesson: {title}",
            content=content,
            category=MemoryCategory.DEPLOYMENT_LESSON.value,
            scope=scope,
            author_id=author_id,
            team_id=team_id,
            repository_id=repository_id,
            impact_areas=impact_areas or (file_paths or []),
            actionable_takeaways=actionable_takeaways or ["Verify staging canary before production migration rollout"],
            provenance=prov,
            confidence=confidence,
        )
        saved = self.doc_store.save_memory(memory, changed_by=author_id, change_reason="Recorded deployment lesson")
        if self.neo4j_store:
            try:
                self.neo4j_store.upsert_memories([saved])
            except Exception:
                pass
        return saved

    def record_recurring_solution(
        self,
        title: str,
        problem_pattern: str,
        recommended_solution: str,
        *,
        author_id: str,
        team_id: str | None = None,
        repository_id: str | None = None,
        scope: str = "team",
        impact_areas: list[str] | None = None,
        actionable_takeaways: list[str] | None = None,
        confidence: float = 1.0,
    ) -> Memory:
        """Capture a recurring engineering problem and recommended idiom or solution."""
        memory_id = f"mem_solution_{uuid.uuid4().hex[:8]}"
        content = f"Problem Pattern:\n{problem_pattern}\n\nStandard Solution:\n{recommended_solution}"
        prov = MemoryProvenance(
            source_type="recurring_solution",
            source_id="",
            confidence=confidence,
        )
        memory = Memory(
            id=memory_id,
            title=f"Engineering Solution: {title}",
            content=content,
            category=MemoryCategory.RECURRING_SOLUTION.value,
            scope=scope,
            author_id=author_id,
            team_id=team_id,
            repository_id=repository_id,
            impact_areas=impact_areas or [],
            actionable_takeaways=actionable_takeaways or ["Use this standard solution instead of reinventing custom logic"],
            provenance=prov,
            confidence=confidence,
        )
        saved = self.doc_store.save_memory(memory, changed_by=author_id, change_reason="Recorded recurring solution")
        if self.neo4j_store:
            try:
                self.neo4j_store.upsert_memories([saved])
            except Exception:
                pass
        return saved

    def record_personal_note(
        self,
        title: str,
        note: str,
        *,
        author_id: str,
        repository_id: str | None = None,
        impact_areas: list[str] | None = None,
        actionable_takeaways: list[str] | None = None,
        confidence: float = 1.0,
    ) -> Memory:
        """Capture private, user-scoped engineering note or scratchpad knowledge."""
        memory_id = f"mem_pers_{uuid.uuid4().hex[:8]}"
        prov = MemoryProvenance(
            source_type="personal_note",
            source_id="",
            confidence=confidence,
        )
        memory = Memory(
            id=memory_id,
            title=title,
            content=note,
            category=MemoryCategory.GENERAL.value,
            scope=MemoryScope.PERSONAL.value,
            author_id=author_id,
            repository_id=repository_id,
            impact_areas=impact_areas or [],
            actionable_takeaways=actionable_takeaways or [],
            provenance=prov,
            confidence=confidence,
        )
        saved = self.doc_store.save_memory(memory, changed_by=author_id, change_reason="Recorded personal note")
        if self.neo4j_store:
            try:
                self.neo4j_store.upsert_memories([saved])
            except Exception:
                pass
        return saved

    # --- Session Distillation Execution ---

    def distill_and_record_session(
        self,
        session: AISession,
        messages: list[dict[str, Any]] | None = None,
        *,
        scope: str = "repository",
        auto_sync_neo4j: bool = False,
    ) -> list[Memory]:
        """Distill structured engineering knowledge from a completed session and persist."""
        self.doc_store.save_session(session)
        distilled = self.distiller.distill(session, messages, scope=scope)
        saved_items: list[Memory] = []
        for m in distilled:
            saved = self.doc_store.save_memory(m, changed_by=session.user_id, change_reason="AI Session Distillation")
            saved_items.append(saved)

        if auto_sync_neo4j and self.neo4j_store and saved_items:
            try:
                self.neo4j_store.upsert_ai_sessions([session])
                self.neo4j_store.upsert_memories(saved_items)
            except Exception:
                pass

        return saved_items

    # --- Retrieval & Scoping Query ---

    def retrieve_memories(
        self,
        user_id: str,
        *,
        team_id: str | None = None,
        repository_id: str | None = None,
        query: str | None = None,
        categories: list[str] | None = None,
        scopes: list[str] | None = None,
        include_superseded: bool = False,
        limit: int = 20,
    ) -> list[Memory]:
        """Retrieve memories accessible to user_id across personal, team, and repository scopes."""
        user_team_ids = self.doc_store.get_user_teams(user_id)
        return self.doc_store.query_memories(
            user_id=user_id,
            team_id=team_id,
            user_team_ids=user_team_ids,
            repository_id=repository_id,
            scopes=scopes,
            categories=categories,
            query=query,
            include_superseded=include_superseded,
            limit=limit,
        )

    def search_as_hybrid_results(
        self,
        user_id: str,
        query: str,
        *,
        team_id: str | None = None,
        repository_id: str | None = None,
        categories: list[str] | None = None,
        scopes: list[str] | None = None,
        limit: int = 10,
    ) -> list[HybridRetrievalResult]:
        """Search memories and convert to HybridRetrievalResult for ACO ranking integration."""
        memories = self.retrieve_memories(
            user_id=user_id,
            team_id=team_id,
            repository_id=repository_id,
            query=query,
            categories=categories,
            scopes=scopes,
            limit=limit,
        )
        results: list[HybridRetrievalResult] = []
        query_terms = [t.lower() for t in query.split() if len(t) > 2]

        for m in memories:
            # Score based on keyword coverage in title & content
            matches = sum(
                1 for t in query_terms if t in m.title.lower() or t in m.content.lower()
            )
            score = round(0.5 + 0.5 * (matches / max(1, len(query_terms))), 4)

            hybrid_item = m.to_hybrid_result(score=score)
            results.append(hybrid_item)

        results.sort(key=lambda r: r.score, reverse=True)
        return results

    # --- Evolution & Superseding ---

    def supersede_memory(
        self,
        old_memory_id: str,
        new_memory: Memory,
        *,
        changed_by: str = "",
        reason: str = "Superseded by updated engineering standard",
    ) -> tuple[bool, Memory]:
        """Supersede an existing memory with an updated memory."""
        saved_new = self.doc_store.save_memory(
            new_memory,
            changed_by=changed_by or new_memory.author_id,
            change_reason=f"Supersedes {old_memory_id}: {reason}",
        )
        ok = self.doc_store.supersede_memory(
            old_memory_id,
            new_memory.id,
            changed_by=changed_by,
            reason=reason,
        )
        if self.neo4j_store:
            try:
                self.neo4j_store.upsert_memories([saved_new])
                old = self.doc_store.get_memory(old_memory_id)
                if old:
                    self.neo4j_store.upsert_memories([old])
            except Exception:
                pass
        return ok, saved_new

    # --- Neo4j Graph Synchronization ---

    def sync_to_neo4j(self, neo4j_store: Neo4jMemoryStore | None = None) -> dict[str, int]:
        """Synchronize all entities and memories from document store into Neo4j graph."""
        store = neo4j_store or self.neo4j_store
        if not store:
            raise ValueError("No Neo4jMemoryStore provided for graph synchronization.")

        users = self.doc_store.list_users()
        user_count = store.upsert_users(users)

        cursor = self.doc_store._conn.execute("SELECT * FROM teams")
        teams = [
            Team(
                id=r["id"],
                name=r["name"],
                description=r["description"],
                created_at=r["created_at"],
            )
            for r in cursor.fetchall()
        ]
        team_count = store.upsert_teams(teams)

        cursor = self.doc_store._conn.execute("SELECT * FROM team_memberships")
        memberships = [
            TeamMembership(
                user_id=r["user_id"],
                team_id=r["team_id"],
                role=r["role"],
                joined_at=r["joined_at"],
            )
            for r in cursor.fetchall()
        ]
        membership_count = store.upsert_team_memberships(memberships)

        cursor = self.doc_store._conn.execute("SELECT * FROM memories")
        memories = [self.doc_store._row_to_memory(r) for r in cursor.fetchall()]
        memory_count = store.upsert_memories(memories)

        return {
            "users": user_count,
            "teams": team_count,
            "team_memberships": membership_count,
            "memories": memory_count,
        }
