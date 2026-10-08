"""Unit tests for the Adaptive Context Orchestrator (ACO)."""

import unittest
from backend.hybrid_retrieval import HybridRetrievalService
from backend.models import (
    CodeSymbol,
    DocumentEntity,
    GraphEdge,
    HybridRetrievalResult,
    IntentAnalysisResult,
    OrchestratedContext,
    QueryIntent,
    RelationshipHop,
    ResultProvenance,
)
from backend.orchestrator import (
    AdaptiveContextOrchestrator,
    AdaptiveContextPacker,
    BaseContextPacker,
    BaseContextRanker,
    BaseIntentClassifier,
    DEFAULT_INTENT_STRATEGIES,
    IntentAwareContextRanker,
    RuleBasedIntentClassifier,
    deduplicate_results,
    orchestrate_context,
)
from backend.semantic_matcher import MockEmbeddingProvider


class TestRuleBasedIntentClassifier(unittest.TestCase):
    """Test suite for deterministic intent classification across all 8 supported intents."""

    def setUp(self):
        self.classifier = RuleBasedIntentClassifier()

    def test_classify_code_navigation(self):
        """Verify code_navigation queries are accurately identified."""
        queries = [
            "where is AuthService defined?",
            "locate calculate_tax function",
            "find definition of UserRepo",
            "goto definition of JwtManager",
            "AuthService",
        ]
        for q in queries:
            result = self.classifier.classify(q)
            self.assertEqual(
                result.primary_intent,
                QueryIntent.CODE_NAVIGATION,
                f"Failed on query: '{q}' (got {result.primary_intent.value})",
            )
            self.assertGreater(result.confidence, 0.5)

    def test_classify_explanation(self):
        """Verify explanation queries are accurately identified."""
        queries = [
            "How does authentication work?",
            "explain the token refresh flow",
            "what does verify_signature do?",
            "how is JWT validation implemented?",
            "walk me through the checkout process",
        ]
        for q in queries:
            result = self.classifier.classify(q)
            self.assertEqual(
                result.primary_intent,
                QueryIntent.EXPLANATION,
                f"Failed on query: '{q}' (got {result.primary_intent.value})",
            )
            self.assertGreater(result.confidence, 0.5)

    def test_classify_debugging(self):
        """Verify debugging queries are accurately identified."""
        queries = [
            "fix NullPointerException in AuthService",
            "why is payment failing with timeout?",
            "crash in user_service.py with traceback",
            "debug error when validating JWT",
            "issue with token expiration exception",
        ]
        for q in queries:
            result = self.classifier.classify(q)
            self.assertEqual(
                result.primary_intent,
                QueryIntent.DEBUGGING,
                f"Failed on query: '{q}' (got {result.primary_intent.value})",
            )
            self.assertGreater(result.confidence, 0.6)

    def test_classify_feature_implementation(self):
        """Verify feature_implementation queries are accurately identified."""
        queries = [
            "implement oauth2 login with Google",
            "add new endpoint for user profile",
            "create new service StripePaymentHandler",
            "build webhook notification support",
            "support for MFA authentication",
        ]
        for q in queries:
            result = self.classifier.classify(q)
            self.assertEqual(
                result.primary_intent,
                QueryIntent.FEATURE_IMPLEMENTATION,
                f"Failed on query: '{q}' (got {result.primary_intent.value})",
            )
            self.assertGreater(result.confidence, 0.6)

    def test_classify_refactoring(self):
        """Verify refactoring queries are accurately identified."""
        queries = [
            "refactor AuthService to decouple database dependencies",
            "clean up code smell in jwt_manager.py",
            "extract method for token decoding",
            "simplify complex conditional in billing",
            "restructure authentication module",
        ]
        for q in queries:
            result = self.classifier.classify(q)
            self.assertEqual(
                result.primary_intent,
                QueryIntent.REFACTORING,
                f"Failed on query: '{q}' (got {result.primary_intent.value})",
            )
            self.assertGreater(result.confidence, 0.6)

    def test_classify_architecture(self):
        """Verify architecture queries are accurately identified."""
        queries = [
            "system architecture and high-level component design",
            "modules overview and system design topology",
            "overall structure and dependency diagram",
            "what is the layered architecture of the application?",
        ]
        for q in queries:
            result = self.classifier.classify(q)
            self.assertEqual(
                result.primary_intent,
                QueryIntent.ARCHITECTURE,
                f"Failed on query: '{q}' (got {result.primary_intent.value})",
            )
            self.assertGreater(result.confidence, 0.6)

    def test_classify_documentation(self):
        """Verify documentation queries are accurately identified."""
        queries = [
            "document the API endpoints and write docstrings",
            "generate docs for authentication service",
            "where are the swagger specs for billing?",
            "how to document module configuration in readme",
        ]
        for q in queries:
            result = self.classifier.classify(q)
            self.assertEqual(
                result.primary_intent,
                QueryIntent.DOCUMENTATION,
                f"Failed on query: '{q}' (got {result.primary_intent.value})",
            )
            self.assertGreater(result.confidence, 0.6)

    def test_classify_session_continuation(self):
        """Verify session_continuation queries are accurately identified."""
        queries = [
            "continue what we were working on",
            "what was I doing last time?",
            "resume previous task on payment gateway",
            "next step as we discussed earlier",
            "where did we leave off?",
        ]
        for q in queries:
            result = self.classifier.classify(q)
            self.assertEqual(
                result.primary_intent,
                QueryIntent.SESSION_CONTINUATION,
                f"Failed on query: '{q}' (got {result.primary_intent.value})",
            )
            self.assertGreater(result.confidence, 0.6)

    def test_secondary_intent_detection(self):
        """Verify multi-intent queries detect secondary intents."""
        q = "explain payment service and how to refactor it"
        result = self.classifier.classify(q)
        # Should detect both explanation and refactoring
        intents = [result.primary_intent] + [si[0] for si in result.secondary_intents]
        self.assertIn(QueryIntent.REFACTORING, intents)
        self.assertIn(QueryIntent.EXPLANATION, intents)

    def test_entity_extraction(self):
        """Verify entity extraction extracts PascalCase, camelCase, snake_case, and file paths."""
        q = "fix NullPointerException in AuthService and verify token_utils.py in src/auth_service.py"
        entities = self.classifier.extract_entities(q)
        self.assertIn("NullPointerException", entities)
        self.assertIn("AuthService", entities)
        self.assertIn("token_utils.py", entities)
        self.assertIn("src/auth_service.py", entities)

    def test_empty_query_default(self):
        """Verify empty query defaults gracefully."""
        result = self.classifier.classify("")
        self.assertEqual(result.primary_intent, QueryIntent.EXPLANATION)
        self.assertLessEqual(result.confidence, 0.5)


class TestIntentStrategySelection(unittest.TestCase):
    """Test verification of retrieval strategy configuration per intent."""

    def test_all_eight_intents_have_strategies(self):
        """Verify each of the 8 intents has defined strategy weights and preferred sources."""
        for intent in QueryIntent:
            strategy = DEFAULT_INTENT_STRATEGIES.get(intent)
            self.assertIsNotNone(strategy, f"Missing strategy for intent {intent.value}")
            self.assertIn("lexical_weight", strategy)
            self.assertIn("semantic_weight", strategy)
            self.assertIn("graph_weight", strategy)
            self.assertIn("recency_weight", strategy)
            self.assertIn("max_hops", strategy)
            self.assertIn("preferred_sources", strategy)

    def test_code_navigation_prioritizes_lexical(self):
        """Verify code_navigation uses high lexical weight and low hops."""
        strat = DEFAULT_INTENT_STRATEGIES[QueryIntent.CODE_NAVIGATION]
        self.assertGreater(strat["lexical_weight"], 0.5)
        self.assertEqual(strat["max_hops"], 1)

    def test_refactoring_prioritizes_graph(self):
        """Verify refactoring emphasizes graph traversal for dependency analysis."""
        strat = DEFAULT_INTENT_STRATEGIES[QueryIntent.REFACTORING]
        self.assertGreater(strat["graph_weight"], 0.35)

    def test_session_continuation_prioritizes_recency(self):
        """Verify session_continuation emphasizes recency weight."""
        strat = DEFAULT_INTENT_STRATEGIES[QueryIntent.SESSION_CONTINUATION]
        self.assertGreater(strat["recency_weight"], 0.25)


class TestContextRankerAndDeduplication(unittest.TestCase):
    """Test suite for intent-aware ranking and deduplication."""

    def setUp(self):
        self.ranker = IntentAwareContextRanker()

    def test_intent_aware_ranking_boosts_code_for_navigation(self):
        """Verify code symbols receive boost when intent is code_navigation."""
        code_res = HybridRetrievalResult(
            entity="AuthService",
            type="CodeSymbol (class)",
            source="code",
            node_id="sym-auth",
            score=0.80,
            file_or_document="services/auth.py",
        )
        doc_res = HybridRetrievalResult(
            entity="Auth Documentation",
            type="DocumentEntity (concept)",
            source="documentation",
            node_id="doc-auth",
            score=0.82,  # Starts slightly higher
            file_or_document="docs/auth.md",
        )
        intent = IntentAnalysisResult(
            primary_intent=QueryIntent.CODE_NAVIGATION,
            confidence=0.90,
            extracted_entities=["AuthService"],
        )

        ranked = self.ranker.rank([doc_res, code_res], intent, "where is AuthService?")
        # Code symbol should be boosted past doc entity
        self.assertEqual(ranked[0].entity, "AuthService")
        self.assertIn("intent_boost", ranked[0].scores_breakdown)
        self.assertGreater(ranked[0].scores_breakdown["intent_boost"], 0.0)

    def test_intent_aware_ranking_boosts_specifications_for_explanation(self):
        """Verify specifications and doc entities receive boost for explanation intent."""
        hop = RelationshipHop(
            source_id="doc-1",
            source_name="Auth Spec",
            relation="SPECIFIES",
            target_id="sym-1",
            target_name="AuthService",
        )
        spec_res = HybridRetrievalResult(
            entity="Auth Spec",
            type="DocumentEntity (spec)",
            source="documentation",
            node_id="doc-1",
            score=0.75,
            relationship_path=[hop],
        )
        plain_res = HybridRetrievalResult(
            entity="OtherClass",
            type="CodeSymbol (class)",
            source="code",
            node_id="sym-2",
            score=0.78,
        )
        intent = IntentAnalysisResult(
            primary_intent=QueryIntent.EXPLANATION,
            confidence=0.85,
        )

        ranked = self.ranker.rank([plain_res, spec_res], intent, "how does authentication work?")
        self.assertEqual(ranked[0].entity, "Auth Spec")

    def test_deduplicate_results_merges_duplicate_nodes(self):
        """Verify duplicate node IDs are merged with highest score and unified paths."""
        hop1 = RelationshipHop("s1", "A", "CALLS", "s2", "B")
        hop2 = RelationshipHop("s1", "A", "IMPORTS", "s3", "C")

        res1 = HybridRetrievalResult(
            entity="AuthService",
            type="CodeSymbol (class)",
            source="code",
            node_id="sym-auth",
            score=0.70,
            relationship_path=[hop1],
            content_snippet="short snippet",
        )
        res2 = HybridRetrievalResult(
            entity="AuthService",
            type="CodeSymbol (class)",
            source="code",
            node_id="sym-auth",
            score=0.90,
            relationship_path=[hop2],
            content_snippet="much longer and richer content snippet",
        )

        deduped = deduplicate_results([res1, res2])
        self.assertEqual(len(deduped), 1)
        merged = deduped[0]
        self.assertEqual(merged.score, 0.90)
        self.assertEqual(len(merged.relationship_path), 2)
        self.assertEqual(merged.content_snippet, "much longer and richer content snippet")


class TestAdaptiveContextPacker(unittest.TestCase):
    """Test suite for token budgeting and Markdown context construction."""

    def setUp(self):
        self.packer = AdaptiveContextPacker()
        self.intent = IntentAnalysisResult(
            primary_intent=QueryIntent.EXPLANATION,
            confidence=0.88,
            reasoning="Balancing semantic search and specs.",
        )

    def test_pack_without_budget_includes_all_sections(self):
        """Verify packing without token budget creates organized sections."""
        res_doc = HybridRetrievalResult(
            entity="Auth Architecture",
            type="DocumentEntity (concept)",
            source="documentation",
            file_or_document="docs/arch.md",
            score=0.9,
            content_snippet="Authentication uses JWT tokens signed with RS256.",
        )
        res_code = HybridRetrievalResult(
            entity="AuthService",
            type="CodeSymbol (class)",
            source="code",
            file_or_document="auth/service.py",
            score=0.85,
            content_snippet="class AuthService: def login(self): pass",
        )
        hop = RelationshipHop("doc", "Auth Architecture", "SPECIFIES", "code", "AuthService")
        res_graph = HybridRetrievalResult(
            entity="AuthService",
            type="CodeSymbol (class)",
            source="code",
            file_or_document="auth/service.py",
            score=0.80,
            relationship_path=[hop],
        )

        text, tokens = self.packer.pack([res_doc, res_code, res_graph], self.intent)
        self.assertIn("### Engineering Memory Context (Intent: Explanation)", text)
        self.assertIn("Architecture & Specifications", text)
        self.assertIn("Code Symbols & Interfaces", text)
        self.assertIn("System Relationships & Traversal Paths", text)
        self.assertGreater(tokens, 10)

    def test_pack_with_strict_token_budget_truncates_or_compacts(self):
        """Verify strict token budget is respected."""
        results = [
            HybridRetrievalResult(
                entity=f"Entity_{i}",
                type="CodeSymbol",
                source="code",
                file_or_document=f"src/file_{i}.py",
                score=0.9 - (i * 0.05),
                content_snippet=f"Detailed implementation notes and symbol breakdown for entity {i} " * 10,
            )
            for i in range(10)
        ]

        token_budget = 80
        text, tokens = self.packer.pack(results, self.intent, token_budget=token_budget)
        self.assertLessEqual(tokens, token_budget + 15)  # Safe bounded approximation


class TestAdaptiveContextOrchestratorEndToEnd(unittest.TestCase):
    """End-to-end integration tests for the Adaptive Context Orchestrator."""

    def setUp(self):
        # Deterministic concept vectors for semantic matching
        concept_vectors = {
            "authentication": [1.0, 0.9, 0.8, 0.0],
            "auth": [0.95, 0.9, 0.8, 0.0],
            "jwt": [0.9, 0.85, 0.75, 0.0],
            "security": [0.85, 0.8, 0.7, 0.0],
            "billing": [0.0, 0.1, 0.0, 1.0],
        }
        self.embedding_provider = MockEmbeddingProvider(concept_vectors=concept_vectors, dimension=4)
        self.hybrid_service = HybridRetrievalService(
            retrieval_service=None,
            embedding_provider=self.embedding_provider,
        )
        self.orchestrator = AdaptiveContextOrchestrator(
            retrieval_service=self.hybrid_service,
        )

    def test_orchestrate_how_does_authentication_work(self):
        """Verify the exact prompt scenario: 'How does authentication work?'

        Executes end-to-end ACO pipeline:
        1. Classifies as explanation intent.
        2. Selects appropriate weights and sources.
        3. Retrieves relevant entities (AuthService, JWTManager, Auth Spec, CryptoUtils).
        4. Ranks with explanation affinity boosts.
        5. Deduplicates results.
        6. Constructs formatted Markdown context.
        7. Returns complete OrchestratedContext with metadata.
        """
        user_id = "user-team"
        repo_id = "repo-core"

        symbols = [
            CodeSymbol(
                id="sym-auth",
                name="AuthService",
                file="services/auth.py",
                type="class",
                source_file="services/auth.py",
                documentation="Handles user login and token validation.",
            ),
            CodeSymbol(
                id="sym-jwt",
                name="JWTManager",
                file="security/jwt.py",
                type="class",
                source_file="security/jwt.py",
                documentation="Encodes and verifies JWT tokens.",
            ),
            CodeSymbol(
                id="sym-crypto",
                name="CryptoUtils",
                file="utils/crypto.py",
                type="module",
                source_file="utils/crypto.py",
                documentation="Cryptographic primitives and hashing.",
            ),
            CodeSymbol(
                id="sym-billing",
                name="BillingEngine",
                file="billing/engine.py",
                type="class",
                source_file="billing/engine.py",
                documentation="Processes invoices and credit card charges.",
            ),
        ]

        entities = [
            DocumentEntity(
                id="doc-auth-spec",
                name="Authentication RFC",
                type="spec",
                description="Specification for authentication, session tokens, and security policies.",
                document_path="docs/rfcs/001-auth.md",
            )
        ]

        edges = [
            GraphEdge("doc-auth-spec", "sym-auth", "SPECIFIES", {"confidence": 0.95, "matching_method": "lexical+semantic"}),
            GraphEdge("sym-auth", "sym-jwt", "CALLS", {"confidence": 1.0}),
            GraphEdge("sym-auth", "sym-crypto", "IMPORTS", {"confidence": 1.0}),
        ]

        query = "How does authentication work?"
        orchestrated = self.orchestrator.orchestrate(
            query=query,
            user_id=user_id,
            repository_id=repo_id,
            max_tokens=1500,
            symbols=symbols,
            entities=entities,
            edges=edges,
        )

        # 1. Verification of Intent Analysis
        self.assertEqual(orchestrated.intent.primary_intent, QueryIntent.EXPLANATION)
        self.assertGreater(orchestrated.intent.confidence, 0.6)

        # 2. Verification of Retrieved Results
        entities_found = [r.entity for r in orchestrated.retrieved_results]
        self.assertIn("AuthService", entities_found)
        self.assertIn("Authentication RFC", entities_found)
        self.assertNotIn("BillingEngine", entities_found)  # Billing should not pollute auth query

        # 3. Verification of Context Text
        self.assertIn("### Engineering Memory Context (Intent: Explanation)", orchestrated.context_text)
        self.assertIn("AuthService", orchestrated.context_text)
        self.assertIn("Authentication RFC", orchestrated.context_text)

        # 4. Verification of Detailed Metadata
        meta = orchestrated.retrieval_metadata
        self.assertEqual(meta["intent_detected"], "explanation")
        self.assertGreater(meta["intent_confidence"], 0.6)
        self.assertEqual(meta["token_budget"], 1500)
        self.assertGreater(meta["estimated_tokens"], 0)
        self.assertLessEqual(meta["estimated_tokens"], 1500)
        self.assertEqual(meta["total_candidates_after_dedup"], len(orchestrated.retrieved_results))

    def test_convenience_function_orchestrate_context(self):
        """Verify standalone convenience function orchestrate_context."""
        sym = CodeSymbol(id="s1", name="find_user", file="repo.py", type="function")
        res = orchestrate_context(
            query="where is find_user?",
            user_id="u1",
            repository_id="r1",
            symbols=[sym],
            retrieval_service=self.hybrid_service,
        )
        self.assertEqual(res.intent.primary_intent, QueryIntent.CODE_NAVIGATION)
        self.assertIn("find_user", res.context_text)

    def test_serialization_lossless_roundtrip(self):
        """Verify IntentAnalysisResult and OrchestratedContext to_dict / from_dict serialization."""
        intent = IntentAnalysisResult(
            primary_intent=QueryIntent.DEBUGGING,
            confidence=0.88,
            secondary_intents=[(QueryIntent.EXPLANATION, 0.45)],
            extracted_entities=["AuthService", "error.py"],
            reasoning="Found error pattern.",
            selected_sources=["code_symbols"],
            retrieval_strategy={"limit": 10},
        )
        intent_dict = intent.to_dict()
        intent_rebuilt = IntentAnalysisResult.from_dict(intent_dict)
        self.assertEqual(intent_rebuilt.primary_intent, QueryIntent.DEBUGGING)
        self.assertEqual(intent_rebuilt.confidence, 0.88)
        self.assertEqual(len(intent_rebuilt.secondary_intents), 1)
        self.assertEqual(intent_rebuilt.secondary_intents[0][0], QueryIntent.EXPLANATION)

        context = OrchestratedContext(
            context_text="### Context Header\n\n- Entity 1",
            query="fix error",
            user_id="u1",
            repository_id="r1",
            intent=intent,
            token_budget=500,
            estimated_tokens=45,
            retrieved_results=[],
            retrieval_metadata={"status": "ok"},
        )
        context_dict = context.to_dict()
        context_rebuilt = OrchestratedContext.from_dict(context_dict)
        self.assertEqual(context_rebuilt.query, "fix error")
        self.assertEqual(context_rebuilt.intent.primary_intent, QueryIntent.DEBUGGING)
        self.assertEqual(context_rebuilt.token_budget, 500)
        self.assertEqual(context_rebuilt.estimated_tokens, 45)


if __name__ == "__main__":
    unittest.main()
