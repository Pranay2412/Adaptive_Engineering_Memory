"""Comprehensive unit tests for the Team Memory subsystem.

Validates:
- Entity models, serialization, and HybridRetrievalResult conversion.
- Relational document store (SQLite & PostgreSQL compatibility), CRUD, and immutable revision tracking.
- Multi-tenant scoping isolation (personal, team, repository).
- Session knowledge distillation (distilling engineering cards, filtering noise).
- Unified TeamMemoryService and category helper methods.
- Mocked Neo4j graph persistence, relationship edges, and schema indexes.
"""

import json
import unittest
from backend.db_loader import RELATION_TYPES, Neo4jMemoryStore
from backend.models import (
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
    Team,
    TeamMembership,
    User,
    utc_now_iso,
)
from backend.team_memory import SessionKnowledgeDistiller, TeamMemoryService
from backend.team_memory_store import POSTGRES_SCHEMA_DDL, TeamMemoryDocumentStore
from tests.test_schema import FakeDriver


class TestTeamMemoryModels(unittest.TestCase):
    """Test suite for Team Memory entity models and serializations."""

    def test_user_serialization(self):
        user = User(
            id="usr-1",
            name="Alice Engineer",
            email="alice@example.com",
            role="Staff Engineer",
            created_at=utc_now_iso(),
            metadata={"department": "Platform"},
        )
        d = user.to_dict()
        self.assertEqual(d["id"], "usr-1")
        self.assertEqual(d["role"], "Staff Engineer")
        reconstructed = User.from_dict(d)
        self.assertEqual(reconstructed.id, user.id)
        self.assertEqual(reconstructed.name, user.name)
        self.assertEqual(reconstructed.metadata, {"department": "Platform"})

    def test_team_and_membership_serialization(self):
        team = Team(
            id="team-core",
            name="Core Infrastructure",
            description="Foundational backend systems",
            created_at=utc_now_iso(),
            metadata={"tier": 1},
        )
        d_team = team.to_dict()
        self.assertEqual(d_team["name"], "Core Infrastructure")
        self.assertEqual(Team.from_dict(d_team).name, "Core Infrastructure")

        membership = TeamMembership(
            user_id="usr-1",
            team_id="team-core",
            role="lead",
            joined_at=utc_now_iso(),
        )
        d_m = membership.to_dict()
        self.assertEqual(d_m["role"], "lead")
        self.assertEqual(TeamMembership.from_dict(d_m).team_id, "team-core")

    def test_engineering_decision_serialization(self):
        decision = EngineeringDecision(
            id="dec-101",
            title="Adopt Event-Driven Architecture for Ingestion",
            rationale="Decouples ingestion workers from database transaction locks.",
            status="accepted",
            category="architecture",
            scope="team",
            author_id="usr-1",
            team_id="team-core",
            alternatives_considered=["Synchronous REST pipeline", "Batch polling cron"],
            trade_offs=["Increased operational latency for message queue broker"],
            created_at=utc_now_iso(),
        )
        d = decision.to_dict()
        self.assertEqual(d["id"], "dec-101")
        self.assertEqual(len(d["alternatives_considered"]), 2)
        reconstructed = EngineeringDecision.from_dict(d)
        self.assertEqual(reconstructed.title, decision.title)
        self.assertEqual(reconstructed.alternatives_considered, decision.alternatives_considered)

    def test_memory_provenance_and_to_hybrid_result(self):
        prov = MemoryProvenance(
            source_type="architecture_decision",
            source_id="dec-101",
            file_paths=["backend/pipeline.py", "backend/models.py"],
            symbol_names=["IngestionWorker", "EventManager"],
            confidence=0.98,
        )
        memory = Memory(
            id="mem-1",
            title="Architecture Decision: Event-Driven Pipeline",
            content="Use Kafka or PubSub for async pipeline decoupling.",
            category=MemoryCategory.ARCHITECTURE_DECISION.value,
            scope=MemoryScope.TEAM.value,
            author_id="usr-1",
            team_id="team-core",
            repository_id="repo-main",
            impact_areas=["backend/pipeline.py"],
            actionable_takeaways=["Do not execute synchronous database writes inside ingestion workers."],
            provenance=prov,
            confidence=0.98,
        )
        d = memory.to_dict()
        reconstructed = Memory.from_dict(d)
        self.assertEqual(reconstructed.id, memory.id)
        self.assertEqual(reconstructed.provenance.file_paths, ["backend/pipeline.py", "backend/models.py"])

        # Test conversion to normalized HybridRetrievalResult
        hybrid = memory.to_hybrid_result(score=0.92)
        self.assertIsInstance(hybrid, HybridRetrievalResult)
        self.assertEqual(hybrid.entity, memory.title)
        self.assertEqual(hybrid.source, "team_memory")
        self.assertEqual(hybrid.repository, "repo-main")
        self.assertEqual(hybrid.score, 0.92)
        self.assertEqual(hybrid.provenance.channels, ["team_memory"])
        self.assertEqual(hybrid.metadata["memory_id"], "mem-1")
        self.assertEqual(hybrid.metadata["scope"], "team")


class TestTeamMemoryDocumentStore(unittest.TestCase):
    """Test suite for the SQLite/PostgreSQL document persistence layer."""

    def setUp(self):
        self.store = TeamMemoryDocumentStore(db_path=":memory:")

    def tearDown(self):
        self.store.close()

    def test_postgres_ddl_syntax_reference_exists(self):
        """Ensure PostgreSQL schema reference string is well-formed."""
        self.assertIn("CREATE TABLE IF NOT EXISTS memories", POSTGRES_SCHEMA_DDL)
        self.assertIn("CREATE TABLE IF NOT EXISTS engineering_decisions", POSTGRES_SCHEMA_DDL)
        self.assertIn("CREATE TABLE IF NOT EXISTS memory_revisions", POSTGRES_SCHEMA_DDL)

    def test_user_and_team_crud(self):
        user = User(id="u1", name="Bob", email="bob@test.com")
        self.store.save_user(user)
        self.assertEqual(self.store.get_user("u1").name, "Bob")

        team = Team(id="t1", name="DevOps", description="Infrastructure")
        self.store.save_team(team)
        self.assertEqual(self.store.get_team("t1").name, "DevOps")

        self.store.add_team_membership(TeamMembership(user_id="u1", team_id="t1", role="maintainer"))
        self.assertEqual(self.store.get_user_teams("u1"), ["t1"])
        members = self.store.get_team_members("t1")
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0].role, "maintainer")

    def test_memory_lifecycle_and_revisions(self):
        """Verify saving and updating memories records immutable revision history."""
        mem = Memory(
            id="m100",
            title="Database Connection Pool Sizing",
            content="Max 20 connections per pod.",
            category=MemoryCategory.ARCHITECTURE_DECISION.value,
            scope="team",
            author_id="u1",
            team_id="t1",
        )
        self.store.save_memory(mem, changed_by="u1", change_reason="Initial rule")
        self.assertEqual(self.store.count_memories(), 1)

        revs = self.store.get_memory_revisions("m100")
        self.assertEqual(len(revs), 1)
        self.assertEqual(revs[0]["revision_number"], 1)
        self.assertEqual(revs[0]["change_reason"], "Initial rule")

        # Update memory
        mem.content = "Max 50 connections per pod due to read replicas."
        self.store.save_memory(mem, changed_by="u1", change_reason="Scaled pool size")

        revs_after = self.store.get_memory_revisions("m100")
        self.assertEqual(len(revs_after), 2)
        self.assertEqual(revs_after[1]["revision_number"], 2)
        self.assertEqual(revs_after[1]["content"], "Max 50 connections per pod due to read replicas.")

    def test_supersede_memory(self):
        """Verify superseding marks old memory as superseded and links to newer memory."""
        old_mem = Memory(id="old-1", title="Use Polling", content="Poll every 5s", author_id="u1", scope="team", team_id="t1")
        new_mem = Memory(id="new-1", title="Use Webhooks", content="Push notifications", author_id="u1", scope="team", team_id="t1")
        self.store.save_memory(old_mem)
        self.store.save_memory(new_mem)

        ok = self.store.supersede_memory("old-1", "new-1", changed_by="u1", reason="Polling deprecated")
        self.assertTrue(ok)

        fetched_old = self.store.get_memory("old-1")
        self.assertEqual(fetched_old.status, "superseded")
        self.assertEqual(fetched_old.superseded_by, "new-1")

        # Default query hides superseded memories
        active = self.store.query_memories(user_id="u1", team_id="t1", include_superseded=False)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].id, "new-1")

        # include_superseded=True reveals both
        all_mems = self.store.query_memories(user_id="u1", team_id="t1", include_superseded=True)
        self.assertEqual(len(all_mems), 2)

    def test_scoping_isolation_personal_team_repo(self):
        """Test multi-tenant isolation rules for personal, team, and repository scopes."""
        # 1. Personal memory for Alice
        self.store.save_memory(
            Memory(id="p-alice", title="Alice Private Note", content="My notes", scope="personal", author_id="alice")
        )
        # 2. Team memory for Team Alpha
        self.store.save_memory(
            Memory(id="t-alpha", title="Alpha Team Standard", content="Alpha conventions", scope="team", author_id="alice", team_id="team-alpha")
        )
        # 3. Repository memory for Repo Main
        self.store.save_memory(
            Memory(id="r-main", title="Main Repo Setup", content="Setup steps", scope="repository", author_id="charlie", repository_id="repo-main")
        )

        # Alice's query (member of team-alpha)
        alice_results = self.store.query_memories(user_id="alice", user_team_ids=["team-alpha"], repository_id="repo-main")
        alice_ids = {m.id for m in alice_results}
        self.assertIn("p-alice", alice_ids)
        self.assertIn("t-alpha", alice_ids)
        self.assertIn("r-main", alice_ids)

        # Bob's query (not in team-alpha, not author of p-alice)
        bob_results = self.store.query_memories(user_id="bob", user_team_ids=["team-beta"], repository_id="repo-main")
        bob_ids = {m.id for m in bob_results}
        self.assertNotIn("p-alice", bob_ids, "Bob must NOT see Alice's personal memory")
        self.assertNotIn("t-alpha", bob_ids, "Bob must NOT see Team Alpha's memory")
        self.assertIn("r-main", bob_ids, "Bob SHOULD see repo-main memory when working on repo-main")


class TestSessionKnowledgeDistiller(unittest.TestCase):
    """Test suite for distilling structured engineering knowledge cards from AI sessions."""

    def setUp(self):
        self.distiller = SessionKnowledgeDistiller()

    def test_distill_architecture_decision(self):
        session = AISession(
            id="sess-001",
            title="Design Authentication Architecture",
            user_id="u1",
            repository_id="repo-auth",
            team_id="team-security",
            summary="We decided to use JWT tokens with RSA key rotation for stateless auth service.",
        )
        messages = [
            {"role": "user", "content": "How should we structure auth tokens in auth_service.py?"},
            {"role": "assistant", "content": "We evaluated sessions vs tokens. The team decided to use JWT tokens with RSA key rotation in auth_service.py for the AuthService class to avoid database lookups."},
        ]

        memories = self.distiller.distill(session, messages)
        self.assertEqual(len(memories), 1)
        mem = memories[0]
        self.assertEqual(mem.category, MemoryCategory.ARCHITECTURE_DECISION.value)
        self.assertEqual(mem.repository_id, "repo-auth")
        self.assertIn("auth_service.py", mem.provenance.file_paths)
        self.assertTrue(any("AuthService" in s for s in mem.provenance.symbol_names))
        self.assertEqual(mem.provenance.source_type, "ai_session_distillation")

    def test_distill_bug_root_cause(self):
        session = AISession(
            id="sess-002",
            title="Investigate Race Condition in CacheManager",
            user_id="u1",
            repository_id="repo-core",
            summary="Identified race condition causing dirty reads in CacheManager.",
        )
        messages = [
            {"role": "user", "content": "Why is the cache leaking stale entries under high concurrency?"},
            {"role": "assistant", "content": "The root cause was a missing mutex lock around cache_invalidation in cache_manager.py. We fixed bug by introducing a distributed redis lock."},
        ]

        memories = self.distiller.distill(session, messages)
        self.assertEqual(len(memories), 1)
        mem = memories[0]
        self.assertEqual(mem.category, MemoryCategory.BUG_ROOT_CAUSE.value)
        self.assertIn("cache_manager.py", mem.provenance.file_paths)
        self.assertTrue(len(mem.actionable_takeaways) > 0)

    def test_distill_refactor_summary(self):
        session = AISession(
            id="sess-003",
            title="Refactor Ingestion Pipeline",
            user_id="u1",
            repository_id="repo-core",
            summary="Refactored the legacy pipeline to use async generators.",
        )
        messages = [
            {"role": "assistant", "content": "We refactored pipeline.py and extracted service StreamProcessor to minimize memory footprints."},
        ]

        memories = self.distiller.distill(session, messages)
        self.assertEqual(len(memories), 1)
        mem = memories[0]
        self.assertEqual(mem.category, MemoryCategory.IMPORTANT_REFACTOR.value)


class TestTeamMemoryService(unittest.TestCase):
    """Test suite for the unified TeamMemoryService operations."""

    def setUp(self):
        self.doc_store = TeamMemoryDocumentStore(db_path=":memory:")
        self.service = TeamMemoryService(doc_store=self.doc_store)

    def tearDown(self):
        self.service.doc_store.close()

    def test_record_all_engineering_categories(self):
        """Test recording dedicated knowledge types across the 7 core categories + personal note."""
        # 1. Architecture Decision
        m_arch = self.service.record_architecture_decision(
            "Use gRPC for Inter-Service Calls",
            "Protobuf ensures strong type contracts and low serialization overhead.",
            author_id="usr-1",
            team_id="team-infra",
            repository_id="repo-backend",
            alternatives_considered=["JSON REST", "GraphQL"],
            trade_offs=["Requires HTTP/2 load balancing support"],
            file_paths=["services/gateway.py"],
            symbol_names=["GatewayClient"],
        )
        self.assertEqual(m_arch.category, MemoryCategory.ARCHITECTURE_DECISION.value)
        self.assertIsNotNone(m_arch.decision_id)
        # Verify EngineeringDecision entity was also saved
        dec = self.doc_store.get_decision(m_arch.decision_id)
        self.assertIsNotNone(dec)
        self.assertEqual(dec.title, "Use gRPC for Inter-Service Calls")

        # 2. Bug Root Cause
        m_bug = self.service.record_bug_root_cause(
            "Connection Leak in DB Pool",
            "Context manager was not closing connections on unhandled socket timeouts.",
            "Wrapped connection acquisition with try/finally block in db_pool.py.",
            author_id="usr-1",
            repository_id="repo-backend",
            event_ref="INCIDENT-404",
        )
        self.assertEqual(m_bug.category, MemoryCategory.BUG_ROOT_CAUSE.value)
        self.assertIsNotNone(m_bug.event_id)

        # 3. Implementation Summary
        m_impl = self.service.record_implementation_summary(
            "Implemented Token Bucket Rate Limiter",
            "Added in-memory token bucket rate limiter with sliding window fallback.",
            author_id="usr-1",
            repository_id="repo-backend",
            file_paths=["middleware/rate_limit.py"],
        )
        self.assertEqual(m_impl.category, MemoryCategory.IMPLEMENTATION_SUMMARY.value)

        # 4. API Decision
        m_api = self.service.record_api_decision(
            "Deprecate v1 User Endpoint",
            "Replaced GET /v1/user with GET /v2/users/{id} pagination envelope.",
            "Standardizes pagination across all entity APIs.",
            author_id="usr-1",
            repository_id="repo-backend",
        )
        self.assertEqual(m_api.category, MemoryCategory.API_DECISION.value)

        # 5. Important Refactor
        m_ref = self.service.record_important_refactor(
            "Decoupled Ingestion From Neo4j Driver",
            "Ingestion code now outputs normalized AST dataclasses instead of issuing Cypher directly.",
            "Extracted DbLoader into isolated persistence module.",
            author_id="usr-1",
            repository_id="repo-backend",
        )
        self.assertEqual(m_ref.category, MemoryCategory.IMPORTANT_REFACTOR.value)

        # 6. Deployment Lesson
        m_dep = self.service.record_deployment_lesson(
            "Rollback Strategy for Neo4j Schema Migrations",
            "Always use IF NOT EXISTS constraints to prevent migration failures on blue/green rollouts.",
            "Release 2.1 failed when secondary pod attempted to duplicate existing index.",
            author_id="usr-1",
            team_id="team-infra",
        )
        self.assertEqual(m_dep.category, MemoryCategory.DEPLOYMENT_LESSON.value)

        # 7. Recurring Solution
        m_sol = self.service.record_recurring_solution(
            "Graceful Shutdown Pattern for Background Workers",
            "Workers hanging during SIGTERM signals.",
            "Listen for SIGTERM, set threading.Event stop flag, and drain queue before process exit.",
            author_id="usr-1",
            team_id="team-infra",
        )
        self.assertEqual(m_sol.category, MemoryCategory.RECURRING_SOLUTION.value)

        # 8. Personal Note
        m_pers = self.service.record_personal_note(
            "My scratchpad notes for profiling",
            "Remember to run cProfile with -s cumtime flag.",
            author_id="usr-1",
        )
        self.assertEqual(m_pers.scope, "personal")

        self.assertEqual(self.doc_store.count_memories(), 8)

    def test_search_as_hybrid_results_for_aco_integration(self):
        """Verify search_as_hybrid_results produces ranked HybridRetrievalResult objects."""
        self.service.record_architecture_decision(
            "JWT Authentication Protocol",
            "Stateless authentication via RS256 signed JWT tokens in header.",
            author_id="usr-1",
            repository_id="repo-auth",
            team_id="team-sec",
            file_paths=["auth/jwt.py"],
        )
        self.service.record_architecture_decision(
            "Database Read Replica Routing",
            "Route analytical queries to read replica.",
            author_id="usr-1",
            repository_id="repo-db",
            team_id="team-data",
        )

        hybrid_results = self.service.search_as_hybrid_results(
            user_id="usr-1",
            query="How does authentication work with JWT?",
            repository_id="repo-auth",
        )
        self.assertGreater(len(hybrid_results), 0)
        top = hybrid_results[0]
        self.assertIn("JWT Authentication", top.entity)
        self.assertEqual(top.source, "team_memory")
        self.assertGreater(top.score, 0.5)
        self.assertEqual(top.provenance.channels, ["team_memory"])

    def test_supersede_memory_flow(self):
        """Verify superseding an old decision with an updated decision."""
        old = self.service.record_architecture_decision(
            "Original Storage: Local Disk",
            "Store uploaded artifacts on local persistent disk.",
            author_id="usr-1",
            team_id="team-core",
        )
        new = Memory(
            id="mem-new-storage",
            title="Updated Storage: Cloud Object Storage (GCS/S3)",
            content="Store artifacts in distributed bucket storage.",
            category=MemoryCategory.ARCHITECTURE_DECISION.value,
            scope="team",
            author_id="usr-1",
            team_id="team-core",
        )
        ok, saved_new = self.service.supersede_memory(old.id, new, reason="Scale past single node disk")
        self.assertTrue(ok)
        self.assertEqual(saved_new.id, "mem-new-storage")

        active = self.service.retrieve_memories(user_id="usr-1", team_id="team-core", include_superseded=False)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].id, "mem-new-storage")


class TestTeamMemoryNeo4jPersistence(unittest.TestCase):
    """Test suite for Neo4j persistence and relationship graph synchronization."""

    def test_team_memory_relationships_in_relation_types(self):
        """Verify all new relationship types are recognized in RELATION_TYPES."""
        expected = {
            "MEMBER_OF",
            "OWNS_PROJECT",
            "CONTAINS_REPO",
            "INITIATED_SESSION",
            "IN_REPOSITORY",
            "AUTHORED",
            "SHARED_WITH_TEAM",
            "SCOPED_TO_REPO",
            "PART_OF_PROJECT",
            "EXTRACTED_FROM_SESSION",
            "ORIGINATED_FROM_EVENT",
            "JUSTIFIED_BY",
            "REFERENCES_SYMBOL",
            "REFERENCES_DOC",
            "SUPERSEDES",
            "AUTHORED_BY",
            "AFFECTS_REPO",
            "APPLIES_TO_TEAM",
        }
        self.assertTrue(expected.issubset(RELATION_TYPES))

    def test_initialize_schema_indexes_for_team_memory_labels(self):
        """Verify initialize_schema sets up indexes on User, Team, Project, AISession, etc."""
        driver = FakeDriver()
        store = Neo4jMemoryStore(driver=driver)
        store.initialize_schema()

        queries = [q[0] for q in driver.queries]
        self.assertTrue(any("user_id" in q for q in queries))
        self.assertTrue(any("team_id" in q for q in queries))
        self.assertTrue(any("project_id" in q for q in queries))
        self.assertTrue(any("ai_session_id" in q for q in queries))
        self.assertTrue(any("eng_event_id" in q for q in queries))
        self.assertTrue(any("eng_decision_id" in q for q in queries))
        self.assertTrue(any("memory_id" in q for q in queries))
        self.assertTrue(any("memory_scope" in q for q in queries))

    def test_upsert_team_memory_graph_entities(self):
        """Verify upserting users, teams, sessions, decisions, and memories writes Cypher correctly."""
        driver = FakeDriver()
        store = Neo4jMemoryStore(driver=driver)

        # 1. Users & Teams
        users = [User(id="u-10", name="Alice", email="alice@test.com")]
        teams = [Team(id="t-10", name="Core Team")]
        memberships = [TeamMembership(user_id="u-10", team_id="t-10", role="lead")]

        store.upsert_users(users)
        store.upsert_teams(teams)
        store.upsert_team_memberships(memberships)

        queries = [q[0] for q in driver.queries]
        self.assertTrue(any("MERGE (u:User {id: row.id})" in q for q in queries))
        self.assertTrue(any("MERGE (t:Team {id: row.id})" in q for q in queries))
        self.assertTrue(any("MERGE (u)-[r:MEMBER_OF]->(t)" in q for q in queries))

        # 2. Memories and relationship linking
        prov = MemoryProvenance(
            source_type="ai_session_distillation",
            symbol_names=["AuthManager"],
            document_paths=["docs/auth.md"],
        )
        memories = [
            Memory(
                id="m-500",
                title="Auth Architecture",
                content="Use JWT tokens",
                author_id="u-10",
                team_id="t-10",
                repository_id="repo-1",
                session_id="s-1",
                decision_id="d-1",
                provenance=prov,
            )
        ]
        store.upsert_memories(memories)

        mem_queries = [q[0] for q in driver.queries]
        self.assertTrue(any("MERGE (m:Memory {id: row.id})" in q for q in mem_queries))
        self.assertTrue(any("MERGE (u)-[:AUTHORED]->(m)" in q for q in mem_queries))
        self.assertTrue(any("MERGE (m)-[:SHARED_WITH_TEAM]->(t)" in q for q in mem_queries))
        self.assertTrue(any("MERGE (m)-[:SCOPED_TO_REPO]->(r)" in q for q in mem_queries))
        self.assertTrue(any("MERGE (m)-[:REFERENCES_SYMBOL]->(c)" in q for q in mem_queries))
        self.assertTrue(any("MERGE (m)-[:REFERENCES_DOC]->(doc)" in q for q in mem_queries))

    def test_sync_to_neo4j_service_method(self):
        """Verify TeamMemoryService.sync_to_neo4j synchronizes all persisted entities."""
        driver = FakeDriver()
        store = Neo4jMemoryStore(driver=driver)
        service = TeamMemoryService(neo4j_store=store)

        service.register_user(User(id="u-sync", name="Sync User"))
        service.register_team(Team(id="t-sync", name="Sync Team"))
        service.record_architecture_decision(
            "Sync Decision",
            "Verify graph synchronization",
            author_id="u-sync",
            team_id="t-sync",
        )

        counts = service.sync_to_neo4j()
        self.assertEqual(counts["users"], 1)
        self.assertEqual(counts["teams"], 1)
        self.assertEqual(counts["memories"], 1)


if __name__ == "__main__":
    unittest.main()
