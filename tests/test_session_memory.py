"""Unit and integration tests for AI Session Memory subsystem.

Validates:
- Data models (AISessionRecord, RecordSessionResponse) and lossless serialization.
- Default behavior: does not store unnecessary full conversation transcripts.
- Intelligent summarization layer converting raw session info into reusable engineering knowledge.
- Multi-tenant visibility scoping (personal, team, repository).
- Direct retrievability of session memories by the Adaptive Context Orchestrator (ACO).
- Graph synchronization in Neo4j with touch edges (TOUCHES_SYMBOL, TOUCHES_FILE).
- Standalone JSON API endpoint helpers.
"""

import unittest
from backend.db_loader import Neo4jMemoryStore
from backend.models import (
    AISessionRecord,
    EngineeringDecision,
    HybridRetrievalResult,
    Memory,
    MemoryCategory,
    MemoryScope,
    RecordSessionResponse,
    utc_now_iso,
)
from backend.orchestrator import AdaptiveContextOrchestrator, orchestrate_context
from backend.session_memory import (
    AISessionKnowledgeSummarizer,
    AISessionMemoryService,
    get_ai_session_api,
    query_ai_sessions_api,
    record_ai_session_api,
)
from backend.team_memory_store import TeamMemoryDocumentStore
from tests.test_schema import FakeDriver


class TestAISessionRecordModel(unittest.TestCase):
    """Test suite for AISessionRecord and response serialization."""

    def test_session_record_serialization(self):
        record = AISessionRecord(
            session_id="sess-100",
            user_id="user-alice",
            repository_id="repo-checkout",
            prompt="Optimize payment checkout latency under peak load",
            response_summary="Implemented async payment validation queue with Redis backing.",
            affected_files=["services/checkout.py", "services/payment.py"],
            affected_symbols=["CheckoutService", "PaymentQueue"],
            engineering_decisions=[
                "Use Redis Streams instead of RabbitMQ for lightweight in-memory delivery",
                "Set idempotent transaction window to 15 minutes",
            ],
            timestamp=utc_now_iso(),
            visibility="team",
            team_id="team-payments",
            metadata={"latency_target_ms": 120},
        )
        d = record.to_dict()
        self.assertEqual(d["session_id"], "sess-100")
        self.assertEqual(d["user_id"], "user-alice")
        self.assertEqual(d["visibility"], "team")
        self.assertEqual(d["team_id"], "team-payments")
        self.assertIsNone(d["raw_transcript"], "Raw conversation transcript must be None by default")
        self.assertEqual(len(d["affected_files"]), 2)
        self.assertEqual(len(d["engineering_decisions"]), 2)

        reconstructed = AISessionRecord.from_dict(d)
        self.assertEqual(reconstructed.session_id, record.session_id)
        self.assertEqual(reconstructed.engineering_decisions, record.engineering_decisions)
        self.assertEqual(reconstructed.metadata, {"latency_target_ms": 120})

    def test_record_session_response_serialization(self):
        record = AISessionRecord(
            session_id="sess-101",
            user_id="user-bob",
            repository_id="repo-auth",
            prompt="Add JWT refresh endpoint",
            response_summary="Added refresh token rotation.",
        )
        resp = RecordSessionResponse(
            session=record,
            distilled_memories=[],
            decisions=[],
            status="recorded",
            message="Success",
        )
        d = resp.to_dict()
        self.assertEqual(d["status"], "recorded")
        self.assertEqual(d["session"]["session_id"], "sess-101")


class TestAISessionKnowledgeSummarizer(unittest.TestCase):
    """Test suite for the summarization layer extracting reusable engineering knowledge."""

    def setUp(self):
        self.summarizer = AISessionKnowledgeSummarizer()

    def test_distill_architecture_decision_from_session(self):
        record = AISessionRecord(
            session_id="sess-arch-01",
            user_id="user-1",
            repository_id="repo-core",
            prompt="Decide between Redis Cluster vs Memcached for session cache",
            response_summary="We evaluated Redis Cluster vs Memcached. The team decided to use Redis Cluster because persistence and pub/sub capabilities are required.",
            affected_files=["src/cache/redis.py", "config/cache.yaml"],
            affected_symbols=["RedisCacheManager", "init_cache"],
            engineering_decisions=["Adopt Redis Cluster for multi-region replication"],
            visibility="team",
            team_id="team-infra",
        )
        memories, decisions = self.summarizer.summarize(record)

        self.assertEqual(len(memories), 1)
        mem = memories[0]
        self.assertEqual(mem.category, MemoryCategory.ARCHITECTURE_DECISION.value)
        self.assertIn("Architecture Decision", mem.title)
        self.assertIn("Context & Objective:", mem.content)
        self.assertIn("Key Engineering Decisions:", mem.content)
        self.assertEqual(mem.scope, "team")
        self.assertEqual(mem.team_id, "team-infra")
        self.assertIn("src/cache/redis.py", mem.provenance.file_paths)
        self.assertIn("RedisCacheManager", mem.provenance.symbol_names)

        # Check created EngineeringDecision object
        self.assertEqual(len(decisions), 1)
        dec = decisions[0]
        self.assertEqual(dec.title, "Adopt Redis Cluster for multi-region replication")
        self.assertEqual(dec.status, "accepted")
        self.assertEqual(dec.author_id, "user-1")

    def test_distill_bug_root_cause_from_session(self):
        record = AISessionRecord(
            session_id="sess-bug-01",
            user_id="user-2",
            repository_id="repo-db",
            prompt="Debug race condition during concurrent row updates",
            response_summary="Identified root cause: missing SELECT FOR UPDATE lock causing lost updates in transaction block. Fixed bug with pessimistic locking.",
            affected_files=["db/transaction.py"],
            affected_symbols=["update_balance", "AccountRepository"],
            visibility="repository",
        )
        memories, decisions = self.summarizer.summarize(record)

        self.assertEqual(len(memories), 1)
        mem = memories[0]
        self.assertEqual(mem.category, MemoryCategory.BUG_ROOT_CAUSE.value)
        self.assertIn("Bug Root Cause", mem.title)
        self.assertIn("db/transaction.py", mem.impact_areas)
        self.assertTrue(any("defect recurrence" in t for t in mem.actionable_takeaways))

    def test_distill_api_decision_from_session(self):
        record = AISessionRecord(
            session_id="sess-api-01",
            user_id="user-3",
            repository_id="repo-gateway",
            prompt="Create v2 API contract for user profile retrieval",
            response_summary="Designed GET /v2/profiles with pagination envelope and ISO timestamps.",
            affected_files=["api/v2/routes.py"],
            visibility="repository",
        )
        memories, _ = self.summarizer.summarize(record)
        self.assertEqual(memories[0].category, MemoryCategory.API_DECISION.value)

    def test_filters_out_conversational_fluff(self):
        record = AISessionRecord(
            session_id="sess-clean-01",
            user_id="user-4",
            repository_id="repo-core",
            prompt="Can you please implement rate limiting?",
            response_summary="Implemented token bucket rate limiter in middleware/rate_limit.py.",
        )
        raw_msgs = [
            {"role": "user", "content": "Hello there! Can you please help me?"},
            {"role": "assistant", "content": "Hi! Certainly, I would be glad to help."},
            {"role": "assistant", "content": "I have created the token bucket rate limiter in middleware/rate_limit.py."},
        ]
        memories, _ = self.summarizer.summarize(record, raw_messages=raw_msgs)
        self.assertEqual(len(memories), 1)
        mem = memories[0]
        self.assertNotIn("Hello there!", mem.content)
        self.assertNotIn("Hi! Certainly", mem.content)
        self.assertIn("middleware/rate_limit.py", mem.impact_areas)


class TestAISessionMemoryService(unittest.TestCase):
    """Test suite for AISessionMemoryService recording, storage, and scoping."""

    def setUp(self):
        self.doc_store = TeamMemoryDocumentStore(db_path=":memory:")
        self.service = AISessionMemoryService(doc_store=self.doc_store)

    def tearDown(self):
        self.doc_store.close()

    def test_record_session_stores_all_11_fields_and_no_raw_data_by_default(self):
        """Verify recording session stores all 11 required fields and omits raw chat by default."""
        resp = self.service.record_session(
            session_id="sess-dev-001",
            user_id="u-alice",
            repository_id="repo-auth",
            prompt="How does token revocation work?",
            response_summary="Implemented token blacklist in Redis with TTL matching JWT expiration.",
            affected_files=["auth/revocation.py"],
            affected_symbols=["TokenBlacklist", "revoke_token"],
            engineering_decisions=["Store revoked JTI in Redis with automatic TTL expiry"],
            timestamp="2026-10-08T12:00:00Z",
            visibility="repository",
            team_id="team-security",
            raw_messages=[{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi"}],
            store_raw_transcript=False,  # default
        )

        self.assertEqual(resp.status, "recorded")
        self.assertEqual(len(resp.distilled_memories), 1)
        self.assertEqual(len(resp.decisions), 1)

        # Retrieve record from database
        stored = self.service.get_session("sess-dev-001")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.session_id, "sess-dev-001")
        self.assertEqual(stored.user_id, "u-alice")
        self.assertEqual(stored.repository_id, "repo-auth")
        self.assertEqual(stored.prompt, "How does token revocation work?")
        self.assertEqual(stored.response_summary, "Implemented token blacklist in Redis with TTL matching JWT expiration.")
        self.assertEqual(stored.affected_files, ["auth/revocation.py"])
        self.assertEqual(stored.affected_symbols, ["TokenBlacklist", "revoke_token"])
        self.assertEqual(stored.engineering_decisions, ["Store revoked JTI in Redis with automatic TTL expiry"])
        self.assertEqual(stored.timestamp, "2026-10-08T12:00:00Z")
        self.assertEqual(stored.visibility, "repository")
        self.assertEqual(stored.team_id, "team-security")

        # Crucial requirement: raw conversation transcripts must NOT be stored by default
        self.assertIsNone(stored.raw_transcript)

        # But distilled memories must be stored
        session_mems = self.service.get_session_memories("sess-dev-001")
        self.assertEqual(len(session_mems), 1)
        self.assertEqual(session_mems[0].session_id, "sess-dev-001")

    def test_store_raw_transcript_only_when_explicitly_requested(self):
        """Verify raw_messages are preserved only if store_raw_transcript=True."""
        msgs = [{"role": "user", "content": "debug trace"}]
        resp = self.service.record_session(
            session_id="sess-audit-002",
            user_id="u-alice",
            repository_id="repo-core",
            prompt="Audit trace test",
            response_summary="Trace verified",
            raw_messages=msgs,
            store_raw_transcript=True,
        )
        stored = self.service.get_session("sess-audit-002")
        self.assertEqual(stored.raw_transcript, msgs)

    def test_validation_rejects_missing_required_fields(self):
        """Verify API raises ValueError if required parameters are missing."""
        with self.assertRaises(ValueError):
            self.service.record_session(
                session_id="",
                user_id="u1",
                repository_id="r1",
                prompt="Test",
                response_summary="Test",
            )
        with self.assertRaises(ValueError):
            self.service.record_session(
                session_id="s1",
                user_id="",
                repository_id="r1",
                prompt="Test",
                response_summary="Test",
            )

    def test_multi_tenant_visibility_scoping(self):
        """Verify personal, team, and repository visibility rules for session memories."""
        # Personal session by Alice
        self.service.record_session(
            session_id="sess-pers-alice",
            user_id="alice",
            repository_id="repo-main",
            prompt="Personal profiling test",
            response_summary="Identified slow SQL query.",
            visibility="personal",
        )
        # Team session for team-alpha
        self.service.record_session(
            session_id="sess-team-alpha",
            user_id="alice",
            repository_id="repo-main",
            prompt="Alpha team service configuration",
            response_summary="Set worker threads to 8.",
            visibility="team",
            team_id="team-alpha",
        )

        # Alice retrieves (member of team-alpha)
        alice_results = self.service.retrieve_for_aco(
            query="SQL profiling service configuration",
            user_id="alice",
            repository_id="repo-main",
            team_id="team-alpha",
        )
        alice_entities = [r.entity for r in alice_results]
        self.assertTrue(any("Personal" in e or "SQL" in e for e in alice_entities))
        self.assertTrue(any("Alpha" in e or "service" in e.lower() for e in alice_entities))

        # Bob retrieves (member of team-beta, not alice)
        bob_results = self.service.retrieve_for_aco(
            query="SQL profiling service configuration",
            user_id="bob",
            repository_id="repo-main",
            team_id="team-beta",
        )
        bob_entities = [r.entity for r in bob_results]
        self.assertFalse(any("Personal" in e for e in bob_entities), "Bob must NOT see Alice's personal memory")
        self.assertFalse(any("Alpha" in e for e in bob_entities), "Bob must NOT see Team Alpha's memory")


class TestACORetrievability(unittest.TestCase):
    """Test suite ensuring that AI Session Memories are retrievable by the ACO."""

    def setUp(self):
        self.doc_store = TeamMemoryDocumentStore(db_path=":memory:")
        self.service = AISessionMemoryService(doc_store=self.doc_store)

    def tearDown(self):
        self.doc_store.close()

    def test_recorded_session_retrieved_and_packed_by_aco(self):
        """Verify that after recording an AI session, ACO retrieves and packs the memory."""
        # 1. Record an AI development session
        self.service.record_session(
            session_id="sess-auth-42",
            user_id="dev-charlie",
            repository_id="repo-auth",
            prompt="How does authentication work with JWT?",
            response_summary="The AuthService issues RS256 signed JWT access tokens and rotates refresh tokens every 24 hours.",
            affected_files=["services/auth_service.py", "security/jwt_manager.py"],
            affected_symbols=["AuthService", "JWTManager", "issue_token"],
            engineering_decisions=["Sign JWT tokens with asymmetric RS256 private keys"],
            visibility="repository",
        )

        # 2. Query through orchestrate_context using the session_memory_service
        orchestrated = orchestrate_context(
            query="How does authentication work with JWT?",
            user_id="dev-charlie",
            repository_id="repo-auth",
            session_memory_service=self.service,
            max_tokens=2000,
        )

        # 3. Assertions on orchestrated context
        self.assertIsNotNone(orchestrated)
        retrieved = orchestrated.retrieved_results
        self.assertGreater(len(retrieved), 0, "ACO must retrieve at least one result")

        # Find the session memory item
        session_items = [r for r in retrieved if r.source == "session_memory"]
        self.assertGreater(len(session_items), 0, "ACO must contain session_memory item")

        mem_item = session_items[0]
        self.assertIn("Architecture Decision", mem_item.entity)
        self.assertTrue(
            any("jwt_manager.py" in str(p) for p in (mem_item.provenance.file_paths or mem_item.metadata.get("impact_areas", [])))
        )
        self.assertEqual(mem_item.repository, "repo-auth")

        # Verify that prompt context contains the dedicated Engineering Decisions section
        context_text = orchestrated.context_text
        self.assertIn("Engineering Decisions & AI Session Memory", context_text)
        self.assertIn("RS256", context_text)

    def test_direct_memories_parameter_in_orchestrator(self):
        """Verify passing a list of memories directly to orchestrate() works."""
        mem = Memory(
            id="m-direct-1",
            title="Architecture Decision: Direct Memory Test",
            content="Direct memory passed into orchestrator.",
            category=MemoryCategory.ARCHITECTURE_DECISION.value,
            author_id="u1",
            repository_id="repo-test",
        )
        orchestrator = AdaptiveContextOrchestrator()
        result = orchestrator.orchestrate(
            query="Direct Memory Test",
            user_id="u1",
            repository_id="repo-test",
            memories=[mem],
            max_tokens=1500,
        )
        retrieved_entities = [r.entity for r in result.retrieved_results]
        self.assertIn("Architecture Decision: Direct Memory Test", retrieved_entities)


class TestNeo4jSessionSynchronization(unittest.TestCase):
    """Test suite for Neo4j persistence and relationship graph links."""

    def test_neo4j_session_recording_links_user_repo_symbols_and_files(self):
        """Verify upserting an AISessionRecord links INITIATED_SESSION, IN_REPOSITORY, TOUCHES_SYMBOL, TOUCHES_FILE."""
        driver = FakeDriver()
        store = Neo4jMemoryStore(driver=driver)
        service = AISessionMemoryService(neo4j_store=store)

        service.record_session(
            session_id="sess-graph-01",
            user_id="usr-graph",
            repository_id="repo-graph",
            prompt="Integrate graph sync",
            response_summary="Linked session to AST symbols and docs.",
            affected_files=["docs/spec.md"],
            affected_symbols=["GraphSyncService"],
            engineering_decisions=["Always sync Neo4j idempotently"],
            sync_neo4j=True,
        )

        queries = [q[0] for q in driver.queries]
        self.assertTrue(any("MERGE (s:AISession {id: row.id})" in q for q in queries))
        self.assertTrue(any("MERGE (u)-[:INITIATED_SESSION]->(s)" in q for q in queries))
        self.assertTrue(any("MERGE (s)-[:IN_REPOSITORY]->(r)" in q for q in queries))
        self.assertTrue(any("MERGE (s)-[:TOUCHES_SYMBOL]->(c)" in q for q in queries))
        self.assertTrue(any("MERGE (s)-[:TOUCHES_FILE]->(doc)" in q for q in queries))


class TestJSONAPIEndpoints(unittest.TestCase):
    """Test suite for standalone JSON API handlers."""

    def test_record_and_query_api_handlers(self):
        payload = {
            "session_id": "api-sess-001",
            "user_id": "api-user",
            "repository_id": "api-repo",
            "prompt": "Test JSON API endpoint",
            "response_summary": "Created endpoint wrappers",
            "affected_files": ["api/routes.py"],
            "engineering_decisions": ["Standardize JSON payload envelops"],
        }
        res = record_ai_session_api(payload)
        self.assertEqual(res["status"], "recorded")
        self.assertEqual(res["session"]["session_id"], "api-sess-001")

        # Get API
        fetched = get_ai_session_api("api-sess-001")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["user_id"], "api-user")

        # Query API
        listed = query_ai_sessions_api({"user_id": "api-user"})
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["session_id"], "api-sess-001")


if __name__ == "__main__":
    unittest.main()
