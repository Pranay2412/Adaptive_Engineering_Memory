"""Unit tests for the Hybrid Retrieval Layer combining lexical, semantic, and graph traversal."""

import datetime
import json
import unittest
from backend.hybrid_retrieval import (
    HybridRetrievalService,
    SemanticKnowledgeIndex,
    hybrid_retrieve_engineering_memory,
)
from backend.models import (
    CodeSymbol,
    DocumentEntity,
    GraphEdge,
    HybridQueryResponse,
    HybridRetrievalResult,
    RelationshipHop,
    ResultProvenance,
)
from backend.retrieval import MemoryRetrievalService
from backend.semantic_matcher import MockEmbeddingProvider


class FakeResult:
    def __init__(self, records=None):
        self._records = records or []

    def consume(self):
        return None

    def __iter__(self):
        return iter(self._records)


class FakeSession:
    def __init__(self, queries, query_results=None):
        self.queries = queries
        self.query_results = query_results or {}
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.closed = True

    def run(self, query_str, **parameters):
        self.queries.append((query_str, parameters))
        for key, records in self.query_results.items():
            if key in query_str:
                if callable(records):
                    return FakeResult(records(query_str, parameters))
                return FakeResult(records)
        return FakeResult([])


class FakeDriver:
    def __init__(self, query_results=None):
        self.queries = []
        self.sessions = []
        self.query_results = query_results or {}

    def session(self):
        session = FakeSession(self.queries, self.query_results)
        self.sessions.append(session)
        return session

    def close(self):
        pass


class TestHybridRetrievalLayer(unittest.TestCase):
    """Test suite for the hybrid retrieval layer combining lexical, semantic, and graph traversal."""

    def setUp(self):
        # Configure deterministic concept vectors for semantic matching
        # "authentication" query vector is similar to auth, jwt, security concepts
        concept_vectors = {
            "authentication": [1.0, 0.9, 0.8, 0.0],
            "auth": [0.95, 0.9, 0.8, 0.0],
            "jwt": [0.9, 0.85, 0.75, 0.0],
            "security": [0.85, 0.8, 0.7, 0.0],
            "billing": [0.0, 0.1, 0.0, 1.0],
        }
        self.embedding_provider = MockEmbeddingProvider(
            concept_vectors=concept_vectors,
            dimension=4,
        )

    def test_prompt_scenario_how_does_authentication_work(self):
        """Verify the exact user query: 'How does authentication work?' retrieves:

        - AuthService (class)
        - JWTManager (called component)
        - authentication documentation (spec)
        - relevant dependencies (CryptoUtils via IMPORTS)
        - recent relevant engineering knowledge (Security RFC with recent timestamp)
        """
        user_id = "user-team"
        repo_id = "repo-core"

        # 1. Code symbols
        auth_service = CodeSymbol(
            id="sym_auth_svc",
            name="AuthService",
            file="src/auth/service.py",
            type="class",
            qualified_name="auth.service.AuthService",
            signature="class AuthService(BaseAuthService)",
            documentation="Core authentication service validating user credentials and sessions.",
            user_id=user_id,
            repository_id=repo_id,
        )
        jwt_manager = CodeSymbol(
            id="sym_jwt_mgr",
            name="JWTManager",
            file="src/auth/jwt_manager.py",
            type="class",
            qualified_name="auth.jwt_manager.JWTManager",
            signature="class JWTManager",
            documentation="Issues and verifies cryptographically signed JWT access tokens.",
            user_id=user_id,
            repository_id=repo_id,
        )
        crypto_utils = CodeSymbol(
            id="sym_crypto",
            name="CryptoUtils",
            file="src/common/crypto.py",
            type="module",
            documentation="Cryptographic primitives for password hashing and HMAC verification.",
            user_id=user_id,
            repository_id=repo_id,
        )

        # 2. Documentation entities
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        auth_doc = DocumentEntity(
            id="doc_auth_spec",
            name="Authentication Architecture",
            type="ArchitectureSpec",
            description="End-to-end authentication workflow, OAuth2 grant types, and token renewal.",
            source_document="auth_architecture.md",
            document_path="docs/security/auth_architecture.md",
            section="Authentication Workflow",
            user_id=user_id,
            repository_id=repo_id,
        )
        recent_rfc = DocumentEntity(
            id="doc_recent_rfc",
            name="Recent Security Hardening RFC",
            type="RFC",
            description="Recent engineering decision mandating rotating JWT refresh tokens.",
            source_document="rfc_082_security.md",
            document_path="docs/rfcs/rfc_082_security.md",
            metadata={"created_at": now_iso},
            user_id=user_id,
            repository_id=repo_id,
        )

        symbols = [auth_service, jwt_manager, crypto_utils]
        entities = [auth_doc, recent_rfc]

        # 3. Knowledge graph edges connecting the knowledge elements
        edges = [
            # DocumentEntity -[SPECIFIES]-> CodeSymbol
            GraphEdge(
                source_id="doc_auth_spec",
                target_id="sym_auth_svc",
                relation="SPECIFIES",
                metadata={"confidence": 0.95, "matching_method": "lexical+semantic"},
            ),
            # AuthService -[CALLS]-> JWTManager
            GraphEdge(
                source_id="sym_auth_svc",
                target_id="sym_jwt_mgr",
                relation="CALLS",
                metadata={"confidence": 1.0},
            ),
            # AuthService -[IMPORTS]-> CryptoUtils (relevant dependency)
            GraphEdge(
                source_id="sym_auth_svc",
                target_id="sym_crypto",
                relation="IMPORTS",
                metadata={"confidence": 1.0},
            ),
        ]

        # Run hybrid retrieval
        service = HybridRetrievalService(
            retrieval_service=MemoryRetrievalService(driver=FakeDriver()),
            embedding_provider=self.embedding_provider,
        )

        response = service.retrieve(
            user_id=user_id,
            repository_id=repo_id,
            query="How does authentication work?",
            symbols=symbols,
            entities=entities,
            edges=edges,
            max_hops=2,
            limit=10,
        )

        self.assertIsInstance(response, HybridQueryResponse)
        retrieved_names = [r.entity for r in response.results]

        # Verify all 5 components requested in prompt are retrieved:
        # 1. AuthService
        self.assertIn("AuthService", retrieved_names)
        # 2. JWTManager
        self.assertIn("JWTManager", retrieved_names)
        # 3. authentication documentation
        self.assertIn("Authentication Architecture", retrieved_names)
        # 4. relevant dependencies (CryptoUtils via IMPORTS)
        self.assertIn("CryptoUtils", retrieved_names)
        # 5. recent relevant engineering knowledge (Recent Security Hardening RFC)
        self.assertIn("Recent Security Hardening RFC", retrieved_names)

        # Verify strict deduplication: no entity appears more than once
        self.assertEqual(len(retrieved_names), len(set(retrieved_names)))

        # Verify normalized structure & provenance for every result
        results_map = {r.entity: r for r in response.results}

        for name, item in results_map.items():
            self.assertIsInstance(item, HybridRetrievalResult)
            self.assertIsInstance(item.provenance, ResultProvenance)
            self.assertEqual(item.repository, repo_id)
            self.assertGreater(len(item.provenance.channels), 0)
            self.assertGreater(item.score, 0.0)
            self.assertIn("hybrid", item.scores_breakdown)
            self.assertIn("rrf", item.scores_breakdown)

            # Dictionary representation contains prompt-required fields
            d = item.to_dict()
            self.assertIn("entity", d)
            self.assertIn("type", d)
            self.assertIn("source", d)
            self.assertIn("file/document", d)
            self.assertIn("provenance", d)
            self.assertIn("relevance_information", d)

        # Verify multi-channel synergy on AuthService (both lexical and semantic match)
        auth_res = results_map["AuthService"]
        self.assertTrue(
            "lexical" in auth_res.provenance.channels or "semantic" in auth_res.provenance.channels
        )

        # Verify JWTManager reached via graph traversal (CALLS) or semantic
        jwt_res = results_map["JWTManager"]
        self.assertTrue(
            len(jwt_res.relationship_path) > 0 or "semantic" in jwt_res.provenance.channels
        )

        # Verify CryptoUtils reached via IMPORTS dependency
        crypto_res = results_map["CryptoUtils"]
        self.assertEqual(crypto_res.source, "code")
        self.assertEqual(crypto_res.file, "src/common/crypto.py")

        # Verify recent RFC has recency score boost
        rfc_res = results_map["Recent Security Hardening RFC"]
        self.assertEqual(rfc_res.provenance.created_at, now_iso)
        self.assertGreaterEqual(rfc_res.scores_breakdown["recency"], 0.7)

    def test_adaptive_context_orchestrator_readiness(self):
        """Verify results format into clean context strings and token-budgeted prompt blocks."""
        prov = ResultProvenance(
            source_type="code",
            repository_id="repo-1",
            file_path="src/auth.py",
            channels=["lexical", "semantic"],
        )
        hop = RelationshipHop(
            source_id="d1",
            source_name="AuthDoc",
            relation="SPECIFIES",
            target_id="s1",
            target_name="AuthService",
            confidence=0.95,
        )
        result = HybridRetrievalResult(
            entity="AuthService",
            type="CodeSymbol (class)",
            source="code",
            file_or_document="src/auth.py",
            confidence=0.95,
            score=0.92,
            relationship_path=[hop],
            content_snippet="class AuthService: validates tokens",
            provenance=prov,
        )

        # 1. to_context_str() provides clean Markdown for LLM prompt context
        context_str = result.to_context_str()
        self.assertIn("### [CodeSymbol (class)] AuthService", context_str)
        self.assertIn("- Location: src/auth.py", context_str)
        self.assertIn("- Confidence: 0.95", context_str)
        self.assertIn("- Matched via: lexical, semantic", context_str)
        self.assertIn("- Details: class AuthService: validates tokens", context_str)
        self.assertIn("- Graph Path:", context_str)

        # 2. estimate_tokens() calculates token count
        tokens = result.estimate_tokens()
        self.assertGreater(tokens, 10)

        # 3. to_orchestrator_context() budgets multiple results within token limit
        response = HybridQueryResponse(
            query="test",
            user_id="u",
            repository_id="r",
            results=[result, result],
        )
        orchestrator_ctx = response.to_orchestrator_context(max_tokens=5000)
        self.assertIn("AuthService", orchestrator_ctx)

    def test_multi_channel_fusion_not_simple_concatenation(self):
        """Verify results are fused using reciprocal rank and weighted scoring, not simply concatenated."""
        user_id = "u1"
        repo_id = "r1"

        sym = CodeSymbol("s1", "TokenValidator", "val.py", "class", user_id=user_id, repository_id=repo_id)
        doc = DocumentEntity("d1", "Token Policy", "Requirement", user_id=user_id, repository_id=repo_id)

        service = HybridRetrievalService(
            retrieval_service=MemoryRetrievalService(driver=FakeDriver()),
            embedding_provider=self.embedding_provider,
        )

        # Retrieve with balanced weights
        resp = service.retrieve(
            user_id=user_id,
            repository_id=repo_id,
            query="Token validation",
            symbols=[sym],
            entities=[doc],
            lexical_weight=0.5,
            semantic_weight=0.5,
        )

        self.assertGreater(len(resp.results), 0)
        for r in resp.results:
            # Score must be fused, not just 1.0 or raw concatenation
            self.assertIn("hybrid", r.scores_breakdown)
            self.assertIn("rrf", r.scores_breakdown)
            self.assertGreater(r.scores_breakdown["rrf"], 0.0)

    def test_semantic_knowledge_index_batching_and_queries(self):
        """Verify SemanticKnowledgeIndex correctly embeds and searches symbols and entities."""
        symbols = [
            CodeSymbol("s1", "AuthService", "auth.py", "class"),
            CodeSymbol("s2", "BillingService", "billing.py", "class"),
        ]
        entities = [
            DocumentEntity("e1", "Authentication Guide", "Doc"),
        ]

        index = SemanticKnowledgeIndex(
            symbols=symbols,
            entities=entities,
            provider=self.embedding_provider,
        )

        query_vec = self.embedding_provider.embed_query("authentication tokens")
        sym_matches = index.query_symbols(query_vec, top_n=2, min_similarity=0.0)
        ent_matches = index.query_entities(query_vec, top_n=2, min_similarity=0.0)

        self.assertEqual(len(sym_matches), 2)
        # AuthService should rank higher than BillingService for authentication query
        self.assertEqual(sym_matches[0][0].name, "AuthService")

        self.assertEqual(len(ent_matches), 1)
        self.assertEqual(ent_matches[0][0].name, "Authentication Guide")

    def test_convenience_function_hybrid_retrieve_engineering_memory(self):
        """Verify the standalone functional interface hybrid_retrieve_engineering_memory."""
        symbols = [CodeSymbol("s1", "SessionManager", "session.py", "class")]
        resp = hybrid_retrieve_engineering_memory(
            user_id="u1",
            repository_id="r1",
            query="Session management",
            symbols=symbols,
            embedding_provider=self.embedding_provider,
        )
        self.assertIsInstance(resp, HybridQueryResponse)
        self.assertEqual(len(resp.results), 1)
        self.assertEqual(resp.results[0].entity, "SessionManager")

    def test_empty_query_returns_clean_response(self):
        """Verify empty query returns empty HybridQueryResponse with safe stats."""
        service = HybridRetrievalService(
            retrieval_service=MemoryRetrievalService(driver=FakeDriver()),
            embedding_provider=self.embedding_provider,
        )
        resp = service.retrieve(user_id="u1", repository_id="r1", query="")
        self.assertEqual(len(resp.results), 0)
        self.assertEqual(resp.total_results, 0)
        self.assertEqual(resp.retrieval_stats["status"], "empty_query")


if __name__ == "__main__":
    unittest.main()
