"""Unit tests for the Token Optimization Engine for Synapse."""

import unittest
from backend.models import (
    HybridRetrievalResult,
    RelationshipHop,
    TokenOptimizationResult,
)
from backend.token_optimizer import (
    ContextTier,
    RedundancyDetector,
    TokenEstimator,
    TokenOptimizationEngine,
    optimize_tokens,
    render_candidate_item,
)


class TestTokenEstimation(unittest.TestCase):
    """Test suite for deterministic token estimation and multi-tier rendering."""

    def setUp(self):
        self.item = HybridRetrievalResult(
            entity="AuthService",
            type="class",
            source="code",
            file_or_document="services/auth.py",
            content_snippet="Handles user authentication, password hashing, and token issuance with RSA256 signature verification.",
            relationship_path=[
                RelationshipHop("s1", "AuthService", "CALLS", "s2", "JWTManager")
            ],
            confidence=0.95,
            score=0.88,
        )

    def test_estimate_text_tokens(self):
        """Verify token estimation scales properly with character and word length."""
        self.assertEqual(TokenEstimator.estimate_text(""), 0)
        self.assertGreater(TokenEstimator.estimate_text("AuthService"), 1)
        text_50_words = " ".join(["token"] * 50)
        self.assertGreater(TokenEstimator.estimate_text(text_50_words), 40)

    def test_multi_tier_rendering_hierarchy(self):
        """Verify FULL > COMPACT > MINIMAL in token footprint."""
        full_text = render_candidate_item(self.item, ContextTier.FULL)
        compact_text = render_candidate_item(self.item, ContextTier.COMPACT)
        minimal_text = render_candidate_item(self.item, ContextTier.MINIMAL)

        full_tokens = TokenEstimator.estimate_text(full_text)
        compact_tokens = TokenEstimator.estimate_text(compact_text)
        minimal_tokens = TokenEstimator.estimate_text(minimal_text)

        self.assertGreater(full_tokens, compact_tokens)
        self.assertGreater(compact_tokens, minimal_tokens)
        self.assertIn("Location:", full_text)
        self.assertIn("Summary:", compact_text)
        self.assertIn("[score:", minimal_text)


class TestUtilityPerTokenRanking(unittest.TestCase):
    """Test suite for utility-per-token density calculation and candidate ordering."""

    def test_prefers_concise_high_value_items(self):
        """Verify compact items with high utility-per-token are prioritized."""
        # Item A: High utility (0.90), but large verbose footprint (~500 chars -> ~125 tokens)
        item_verbose = HybridRetrievalResult(
            entity="VerboseModule",
            type="module",
            source="code",
            file_or_document="verbose.py",
            score=0.90,
            content_snippet="word " * 120,
        )

        # Item B: Slightly lower utility (0.80), but very compact (~30 chars -> ~8 tokens)
        item_compact = HybridRetrievalResult(
            entity="CompactHelper",
            type="function",
            source="code",
            file_or_document="helper.py",
            score=0.80,
            content_snippet="def help(): pass",
        )

        engine = TokenOptimizationEngine()
        result = engine.optimize(
            user_query="find helpers",
            ranked_context_items=[item_verbose, item_compact],
            max_token_budget=80,
        )

        # CompactHelper should be packed because its utility per token is much higher
        retained_entities = [r["entity"] for r in result.items_retained]
        self.assertIn("CompactHelper", retained_entities)


class TestRedundancyRemoval(unittest.TestCase):
    """Test suite for detecting and pruning redundant context items."""

    def setUp(self):
        self.detector = RedundancyDetector(jaccard_threshold=0.70)

    def test_exact_duplicate_pruning(self):
        """Verify identical node/file entities are detected as redundant."""
        item1 = HybridRetrievalResult(
            entity="AuthService",
            type="class",
            source="code",
            file_or_document="auth.py",
            node_id="sym-auth-1",
        )
        item2 = HybridRetrievalResult(
            entity="AuthService",
            type="class",
            source="code",
            file_or_document="auth.py",
            node_id="sym-auth-1",
        )

        is_red, reason = self.detector.check_redundancy(item2, [item1])
        self.assertTrue(is_red)
        self.assertIn("duplicate_node_id", reason)

    def test_lexical_content_overlap_pruning(self):
        """Verify high Jaccard similarity between content snippets triggers redundancy pruning."""
        snippet1 = "validates user credentials against database and checks jwt expiration policy"
        snippet2 = "validates user credentials against database and checks jwt expiration policy renewal"

        item1 = HybridRetrievalResult(
            entity="AuthDocPart1",
            type="spec",
            source="docs",
            file_or_document="doc1.md",
            content_snippet=snippet1,
        )
        item2 = HybridRetrievalResult(
            entity="AuthDocPart2",
            type="spec",
            source="docs",
            file_or_document="doc2.md",
            content_snippet=snippet2,
        )

        is_red, reason = self.detector.check_redundancy(item2, [item1])
        self.assertTrue(is_red)
        self.assertIn("high_content_overlap", reason)

    def test_structural_subsumption_pruning(self):
        """Verify a method subsumed inside an already included class snippet is pruned."""
        class_item = HybridRetrievalResult(
            entity="AuthService",
            type="class",
            source="code",
            file_or_document="auth.py",
            content_snippet="class AuthService: def verify_password(self): return True",
        )
        method_item = HybridRetrievalResult(
            entity="verify_password",
            type="method",
            source="code",
            file_or_document="auth.py",
            content_snippet="def verify_password(self): return True",
        )

        is_red, reason = self.detector.check_redundancy(method_item, [class_item])
        self.assertTrue(is_red)
        self.assertIn("structural_subsumption", reason)


class TestGraphRelationshipsPreservation(unittest.TestCase):
    """Test suite for preserving critical relationship paths."""

    def test_preserves_relational_paths_in_optimized_context(self):
        """Verify graph relationships needed for understanding are rendered."""
        hop = RelationshipHop(
            source_id="sym-auth",
            source_name="AuthService",
            relation="SPECIFIES",
            target_id="doc-spec",
            target_name="Authentication Spec",
            confidence=0.95,
        )
        item = HybridRetrievalResult(
            entity="AuthService",
            type="class",
            source="code",
            file_or_document="auth.py",
            relationship_path=[hop],
            score=0.90,
            content_snippet="class AuthService: pass",
        )

        engine = TokenOptimizationEngine()
        result = engine.optimize(
            user_query="explain auth",
            ranked_context_items=[item],
            max_token_budget=500,
        )

        self.assertIn("Relational Context & Traversal Paths", result.optimized_context)
        self.assertIn("SPECIFIES", result.optimized_context)
        self.assertIn("Authentication Spec", result.optimized_context)


class TestBudgetStoppingAndMetrics(unittest.TestCase):
    """Test suite for strict token budget enforcement and audit metrics."""

    def test_strict_token_budget_stopping(self):
        """Verify optimizer stops when token budget is reached and never exceeds budget."""
        items = [
            HybridRetrievalResult(
                entity=f"Symbol_{i}",
                type="class",
                source="code",
                file_or_document=f"src/file_{i}.py",
                score=0.95 - (i * 0.05),
                content_snippet=f"Implementation details and method signatures for symbol number {i} " * 5,
            )
            for i in range(15)
        ]

        budget = 120
        engine = TokenOptimizationEngine()
        result = engine.optimize(
            user_query="inspect symbols",
            ranked_context_items=items,
            max_token_budget=budget,
        )

        # Budget ceiling check
        self.assertLessEqual(result.estimated_input_tokens, budget)
        self.assertGreater(result.tokens_saved, 0)
        self.assertGreater(result.percentage_reduction, 0.0)
        self.assertGreater(len(result.items_removed), 0)
        self.assertGreater(len(result.items_retained), 0)

        # Check reason on removed items
        for rm in result.items_removed:
            self.assertIn("reason", rm)
            self.assertIn("original_tokens", rm)

    def test_output_structure_and_serialization(self):
        """Verify TokenOptimizationResult exposes all required keys and roundtrips to dictionary."""
        item = HybridRetrievalResult(
            entity="AuthService",
            type="class",
            source="code",
            file_or_document="services/auth.py",
            score=0.85,
            content_snippet="class AuthService: pass",
        )

        result = optimize_tokens(
            user_query="where is auth",
            ranked_context_items=[item],
            max_token_budget=500,
        )

        # Verify required keys in output
        self.assertIsInstance(result.optimized_context, str)
        self.assertIsInstance(result.estimated_input_tokens, int)
        self.assertIsInstance(result.original_estimated_tokens, int)
        self.assertIsInstance(result.tokens_saved, int)
        self.assertIsInstance(result.percentage_reduction, float)
        self.assertIsInstance(result.items_removed, list)
        self.assertIsInstance(result.items_retained, list)

        # Dictionary serialization
        d = result.to_dict()
        self.assertIn("optimized_context", d)
        self.assertIn("estimated_input_tokens", d)
        self.assertIn("original_estimated_tokens", d)
        self.assertIn("tokens_saved", d)
        self.assertIn("percentage_reduction", d)
        self.assertIn("items_removed", d)
        self.assertIn("items_retained", d)

        rebuilt = TokenOptimizationResult.from_dict(d)
        self.assertEqual(rebuilt.estimated_input_tokens, result.estimated_input_tokens)
        self.assertEqual(rebuilt.tokens_saved, result.tokens_saved)


class TestTokenOptimizationOrchestratorIntegration(unittest.TestCase):
    """Integration test suite for Token Optimization Engine within the Orchestrator."""

    def test_orchestrator_attaches_token_optimization_metadata(self):
        """Verify ACO automatically populates retrieval_metadata['token_optimization'] when max_tokens is passed."""
        from backend.models import CodeSymbol
        from backend.orchestrator import AdaptiveContextOrchestrator

        syms = [
            CodeSymbol(
                id=f"sym_{i}",
                name=f"Service_{i}",
                file=f"services/svc_{i}.py",
                type="class",
                documentation=f"Detailed documentation for service {i} " * 6,
            )
            for i in range(8)
        ]

        from backend.hybrid_retrieval import HybridRetrievalService
        from backend.semantic_matcher import MockEmbeddingProvider

        hybrid_service = HybridRetrievalService(embedding_provider=MockEmbeddingProvider())
        orchestrator = AdaptiveContextOrchestrator(retrieval_service=hybrid_service)
        result = orchestrator.orchestrate(
            query="where is Service_0?",
            user_id="test_user",
            repository_id="test_repo",
            max_tokens=150,
            symbols=syms,
        )

        meta = result.retrieval_metadata
        self.assertIn("token_optimization", meta)
        opt_data = meta["token_optimization"]
        self.assertIsNotNone(opt_data)
        self.assertIn("tokens_saved", opt_data)
        self.assertIn("percentage_reduction", opt_data)
        self.assertIn("items_retained", opt_data)
        self.assertIn("items_removed", opt_data)

    def test_token_optimized_context_packer(self):
        """Verify TokenOptimizedContextPacker uses the optimization engine directly."""
        from backend.models import IntentAnalysisResult, QueryIntent
        from backend.orchestrator import TokenOptimizedContextPacker

        items = [
            HybridRetrievalResult(
                entity=f"Entity_{i}",
                type="class",
                source="code",
                file_or_document=f"src/file_{i}.py",
                score=0.9 - (i * 0.05),
                content_snippet=f"Snippet for entity {i}",
            )
            for i in range(5)
        ]
        intent = IntentAnalysisResult(
            primary_intent=QueryIntent.EXPLANATION,
            confidence=0.9,
            extracted_entities=["Entity_0"],
        )

        packer = TokenOptimizedContextPacker()
        text, tokens = packer.pack(items, intent, token_budget=100)
        self.assertIsInstance(text, str)
        self.assertGreater(tokens, 0)
        self.assertLessEqual(tokens, 100)

    def test_orchestrator_supports_llm_mode(self):
        """Verify orchestrator runs in LLM compression mode and records mode in metadata."""
        from backend.models import CodeSymbol, OptimizationMode
        from backend.orchestrator import AdaptiveContextOrchestrator

        sym = CodeSymbol(
            id="s1",
            name="AuthService",
            file="services/auth.py",
            type="class",
            documentation="Handles user authentication and JWT validation.",
        )
        from backend.hybrid_retrieval import HybridRetrievalService
        from backend.semantic_matcher import MockEmbeddingProvider

        hybrid_service = HybridRetrievalService(embedding_provider=MockEmbeddingProvider())
        orchestrator = AdaptiveContextOrchestrator(retrieval_service=hybrid_service)
        result = orchestrator.orchestrate(
            query="how does AuthService work?",
            user_id="user1",
            repository_id="repo1",
            max_tokens=400,
            symbols=[sym],
            optimization_mode=OptimizationMode.LLM,
        )

        opt_meta = result.retrieval_metadata.get("token_optimization", {})
        self.assertEqual(opt_meta.get("mode"), "llm")
        self.assertIn("AuthService", result.context_text)


class TestProvenanceInvariants(unittest.TestCase):
    """Test suite ensuring compression NEVER removes provenance and preserves invariants."""

    def setUp(self):
        from backend.models import RelationshipHop
        self.item = HybridRetrievalResult(
            entity="AuthService",
            type="class",
            source="code",
            file_or_document="services/auth.py",
            node_id="sym-auth-001",
            confidence=0.95,
            score=0.90,
            content_snippet="Handles user authentication with RSA256 signature verification. Decision: RS256 chosen over HS256 for asymmetric key rotation.",
            relationship_path=[
                RelationshipHop("sym-auth-001", "AuthService", "CALLS", "sym-jwt-002", "JWTManager")
            ],
            metadata={
                "engineering_decisions": ["RS256 chosen over HS256 for asymmetric key rotation"],
                "recent_changes": ["Migrated to refresh token rotation on 2026-10-05"],
            },
        )

    def test_extract_provenance_anchor(self):
        """Verify provenance anchor extracts sources, symbols, relationships, changes, and decisions."""
        from backend.token_optimizer import extract_provenance_anchor
        anchor = extract_provenance_anchor(self.item)

        self.assertEqual(anchor.entity, "AuthService")
        self.assertEqual(anchor.file_or_document, "services/auth.py")
        self.assertEqual(anchor.node_id, "sym-auth-001")
        self.assertTrue(any("JWTManager" in r for r in anchor.relationships))
        self.assertTrue(any("RS256" in d for d in anchor.engineering_decisions))
        self.assertTrue(any("2026-10-05" in c for c in anchor.recent_changes))

    def test_provenance_guardrail_detects_and_injects_ledger(self):
        """Verify provenance guardrail detects dropped source/symbol and restores traceability ledger."""
        from backend.token_optimizer import ProvenanceGuardrail

        # Simulate poor LLM compression that omitted source file and symbol
        bad_compressed_text = "Users authenticate via JWT token signatures."

        guarded_text, audit = ProvenanceGuardrail.verify_and_guard(
            compressed_text=bad_compressed_text,
            candidates=[self.item],
        )

        self.assertTrue(audit["ledger_injected"])
        self.assertIn("Provenance & Source Traceability Ledger", guarded_text)
        self.assertIn("services/auth.py", guarded_text)
        self.assertIn("AuthService", guarded_text)
        self.assertTrue(audit["all_sources_preserved"])
        self.assertTrue(audit["all_symbols_preserved"])

    def test_never_allows_compression_to_remove_provenance(self):
        """Verify LLM compression mode strictly preserves provenance traceability."""
        from backend.models import OptimizationMode
        from backend.token_optimizer import TokenOptimizationEngine

        engine = TokenOptimizationEngine()
        result = engine.optimize(
            user_query="explain auth",
            ranked_context_items=[self.item],
            max_token_budget=500,
            mode=OptimizationMode.LLM,
        )

        # Invariant checks:
        self.assertIn("services/auth.py", result.optimized_context)
        self.assertIn("AuthService", result.optimized_context)
        self.assertIn("JWTManager", result.optimized_context)
        self.assertTrue(result.provenance_audit.get("all_sources_preserved"))
        self.assertTrue(result.provenance_audit.get("all_symbols_preserved"))


class TestFourStagePipelineAndModes(unittest.TestCase):
    """Test suite verifying: raw context -> deduplication -> structured compression -> token budget enforcement."""

    def setUp(self):
        self.items = [
            HybridRetrievalResult(
                entity="AuthService",
                type="class",
                source="code",
                file_or_document="services/auth.py",
                node_id="sym-auth-1",
                score=0.92,
                content_snippet="class AuthService: def login(): pass",
            ),
            # Redundant duplicate of item 1
            HybridRetrievalResult(
                entity="AuthService",
                type="class",
                source="code",
                file_or_document="services/auth.py",
                node_id="sym-auth-1",
                score=0.90,
                content_snippet="class AuthService: def login(): pass",
            ),
            HybridRetrievalResult(
                entity="JWTManager",
                type="class",
                source="code",
                file_or_document="security/jwt.py",
                node_id="sym-jwt-2",
                score=0.85,
                content_snippet="class JWTManager: def verify(): pass",
            ),
        ]

    def test_stage_two_deduplication_removes_duplicate(self):
        """Verify Stage 2 deduplication removes duplicate items across all modes."""
        from backend.models import OptimizationMode
        from backend.token_optimizer import TokenOptimizationEngine

        engine = TokenOptimizationEngine()
        for mode in [OptimizationMode.NONE, OptimizationMode.DETERMINISTIC, OptimizationMode.LLM]:
            res = engine.optimize("check auth", self.items, max_token_budget=800, mode=mode)
            # The exact duplicate should be in items_removed
            removed_entities = [r["entity"] for r in res.items_removed if "duplicate_node_id" in r["reason"]]
            self.assertIn("AuthService", removed_entities)

    def test_mode_none_raw_uncompressed_under_budget(self):
        """Verify OptimizationMode.NONE keeps full raw representation and enforces budget ceiling."""
        from backend.models import OptimizationMode
        from backend.token_optimizer import TokenOptimizationEngine

        engine = TokenOptimizationEngine()
        res = engine.optimize("check auth", self.items, max_token_budget=100, mode=OptimizationMode.NONE)

        self.assertEqual(res.mode, "none")
        self.assertLessEqual(res.estimated_input_tokens, 100)
        self.assertGreater(len(res.items_retained), 0)

    def test_mode_deterministic_compaction(self):
        """Verify OptimizationMode.DETERMINISTIC applies multi-tier compaction and density ranking."""
        from backend.models import OptimizationMode
        from backend.token_optimizer import TokenOptimizationEngine

        engine = TokenOptimizationEngine()
        res = engine.optimize("check auth", self.items, max_token_budget=100, mode=OptimizationMode.DETERMINISTIC)

        self.assertEqual(res.mode, "deterministic")
        self.assertLessEqual(res.estimated_input_tokens, 100)
        self.assertGreater(res.tokens_saved, 0)

    def test_mode_llm_structured_compression(self):
        """Verify OptimizationMode.LLM executes structured compression and guardrails."""
        from backend.models import OptimizationMode
        from backend.token_optimizer import TokenOptimizationEngine

        engine = TokenOptimizationEngine()
        res = engine.optimize("check auth", self.items, max_token_budget=300, mode=OptimizationMode.LLM)

        self.assertEqual(res.mode, "llm")
        self.assertLessEqual(res.estimated_input_tokens, 300)
        self.assertIn("services/auth.py", res.optimized_context)
        self.assertIn("AuthService", res.optimized_context)
        self.assertTrue(res.provenance_audit.get("all_sources_preserved"))


class TestCompareOptimizationModes(unittest.TestCase):
    """Test suite comparing all three modes: none, deterministic, and llm."""

    def test_compare_modes_returns_three_modes(self):
        """Verify compare_optimization_modes returns valid results and comparison table."""
        from backend.models import HybridRetrievalResult
        from backend.token_optimizer import compare_optimization_modes

        items = [
            HybridRetrievalResult(
                entity=f"Symbol_{i}",
                type="class",
                source="code",
                file_or_document=f"src/file_{i}.py",
                score=0.9 - (i * 0.1),
                content_snippet=f"Detailed implementation and methods for symbol {i} " * 6,
            )
            for i in range(5)
        ]

        comparison = compare_optimization_modes(
            user_query="inspect symbols",
            ranked_context_items=items,
            max_token_budget=150,
        )

        self.assertEqual(comparison.query, "inspect symbols")
        self.assertEqual(comparison.max_token_budget, 150)
        self.assertEqual(comparison.none_result.mode, "none")
        self.assertEqual(comparison.deterministic_result.mode, "deterministic")
        self.assertEqual(comparison.llm_result.mode, "llm")

        # Verify comparison table has 3 rows
        self.assertEqual(len(comparison.comparison_table), 3)
        modes = [row["mode"] for row in comparison.comparison_table]
        self.assertIn("none", modes)
        self.assertIn("deterministic", modes)
        self.assertIn("llm", modes)

        # All modes must enforce the budget ceiling
        for row in comparison.comparison_table:
            self.assertLessEqual(row["estimated_input_tokens"], 150)

    def test_comparison_serialization_roundtrip(self):
        """Verify OptimizationComparison serializes to dictionary and reconstructs losslessly."""
        from backend.models import HybridRetrievalResult, OptimizationComparison
        from backend.token_optimizer import compare_optimization_modes

        items = [
            HybridRetrievalResult(
                entity="AuthService",
                type="class",
                source="code",
                file_or_document="services/auth.py",
                score=0.9,
                content_snippet="Authentication service implementation.",
            )
        ]

        comp = compare_optimization_modes("auth query", items, max_token_budget=300)
        d = comp.to_dict()

        self.assertIn("none_result", d)
        self.assertIn("deterministic_result", d)
        self.assertIn("llm_result", d)
        self.assertIn("comparison_table", d)

        rebuilt = OptimizationComparison.from_dict(d)
        self.assertEqual(rebuilt.query, comp.query)
        self.assertEqual(rebuilt.max_token_budget, comp.max_token_budget)
        self.assertEqual(len(rebuilt.comparison_table), 3)


if __name__ == "__main__":
    unittest.main()
