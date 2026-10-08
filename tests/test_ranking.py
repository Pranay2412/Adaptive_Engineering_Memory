"""Unit tests for the Configurable Context Ranking System for ACO."""

import datetime
import unittest
from backend.models import (
    CandidateRankingScore,
    CodeSymbol,
    DocumentEntity,
    GraphEdge,
    HybridRetrievalResult,
    IntentAnalysisResult,
    QueryIntent,
    RankingWeights,
    RelationshipHop,
)
from backend.orchestrator import AdaptiveContextOrchestrator
from backend.ranking import (
    ConfigurableContextRanker,
    adapt_weights_for_intent,
    compute_confidence,
    compute_freshness,
    compute_graph_relevance,
    compute_lexical_relevance,
    compute_repository_match,
    compute_semantic_relevance,
    compute_source_reliability,
    compute_token_cost_efficiency,
)
from backend.hybrid_retrieval import HybridRetrievalService
from backend.semantic_matcher import MockEmbeddingProvider


class TestRankingSignalComputations(unittest.TestCase):
    """Test suite for individual signal calculations across all 8 ranking factors."""

    def test_compute_semantic_relevance(self):
        """Verify semantic relevance reads from breakdown, metadata, or falls back properly."""
        r1 = HybridRetrievalResult(
            entity="AuthService",
            type="class",
            source="code",
            scores_breakdown={"semantic_score": 0.88},
        )
        self.assertAlmostEqual(compute_semantic_relevance(r1), 0.88, places=4)

        r2 = HybridRetrievalResult(
            entity="UserRepo",
            type="class",
            source="code",
            metadata={"semantic_similarity": 0.72},
        )
        self.assertAlmostEqual(compute_semantic_relevance(r2), 0.72, places=4)

        r3 = HybridRetrievalResult(
            entity="Empty",
            type="unknown",
            source="unknown",
        )
        self.assertEqual(compute_semantic_relevance(r3), 0.0)

    def test_compute_lexical_relevance(self):
        """Verify lexical relevance evaluates exact matches, substrings, and token overlap."""
        # Exact match
        r_exact = HybridRetrievalResult(entity="AuthService", type="class", source="code")
        self.assertEqual(compute_lexical_relevance(r_exact, "AuthService"), 1.0)

        # Substring match
        r_sub = HybridRetrievalResult(entity="AuthService", type="class", source="code")
        self.assertEqual(compute_lexical_relevance(r_sub, "where is AuthService defined"), 0.90)

        # Token overlap
        r_token = HybridRetrievalResult(entity="TokenManager", type="class", source="code")
        score = compute_lexical_relevance(r_token, "validate token session")
        self.assertGreater(score, 0.0)

        # Pre-calculated breakdown score preservation
        r_prev = HybridRetrievalResult(
            entity="Arbitrary",
            type="class",
            source="code",
            scores_breakdown={"lexical_score": 0.95},
        )
        self.assertEqual(compute_lexical_relevance(r_prev, "nomatch"), 0.95)

    def test_compute_graph_relevance(self):
        """Verify graph relevance considers hops, relationship types, and confidence."""
        # 1-hop SPECIFIES path
        hop1 = RelationshipHop("doc", "Spec", "SPECIFIES", "sym", "AuthService", confidence=1.0)
        r_spec = HybridRetrievalResult(
            entity="AuthService",
            type="class",
            source="code",
            relationship_path=[hop1],
        )
        score_1hop = compute_graph_relevance(r_spec)
        self.assertGreater(score_1hop, 0.85)

        # 2-hop path (decay applied)
        hop2 = RelationshipHop("sym", "AuthService", "CALLS", "sym2", "JWTManager", confidence=1.0)
        r_2hop = HybridRetrievalResult(
            entity="JWTManager",
            type="class",
            source="code",
            relationship_path=[hop1, hop2],
        )
        score_2hop = compute_graph_relevance(r_2hop)
        self.assertLess(score_2hop, score_1hop)

        # High-value relation vs low-value relation
        hop_calls = RelationshipHop("s1", "A", "CALLS", "s2", "B", confidence=1.0)
        hop_import = RelationshipHop("s1", "A", "IMPORTS", "s2", "B", confidence=1.0)
        r_calls = HybridRetrievalResult(entity="B", type="class", source="code", relationship_path=[hop_calls])
        r_import = HybridRetrievalResult(entity="B", type="class", source="code", relationship_path=[hop_import])
        self.assertGreater(compute_graph_relevance(r_calls), compute_graph_relevance(r_import))

    def test_compute_freshness(self):
        """Verify freshness decays over age and respects metadata timestamps."""
        now = datetime.datetime.now(datetime.timezone.utc)
        r_recent = HybridRetrievalResult(
            entity="RecentDoc",
            type="doc",
            source="documentation",
            metadata={"created_at": (now - datetime.timedelta(days=2)).isoformat()},
        )
        self.assertEqual(compute_freshness(r_recent, reference_time=now), 1.0)

        r_month_old = HybridRetrievalResult(
            entity="MonthDoc",
            type="doc",
            source="documentation",
            metadata={"created_at": (now - datetime.timedelta(days=20)).isoformat()},
        )
        self.assertEqual(compute_freshness(r_month_old, reference_time=now), 0.85)

        r_old = HybridRetrievalResult(
            entity="OldDoc",
            type="doc",
            source="documentation",
            metadata={"created_at": (now - datetime.timedelta(days=200)).isoformat()},
        )
        self.assertLess(compute_freshness(r_old, reference_time=now), 0.50)

        # Neutral baseline when timestamp missing
        r_none = HybridRetrievalResult(entity="Plain", type="class", source="code")
        self.assertEqual(compute_freshness(r_none), 0.50)

    def test_compute_confidence(self):
        """Verify confidence uses explicit values or intrinsic source defaults."""
        r_explicit = HybridRetrievalResult(entity="A", type="class", source="code", confidence=0.88)
        self.assertEqual(compute_confidence(r_explicit), 0.88)

        r_code = HybridRetrievalResult(entity="A", type="CodeSymbol (class)", source="code")
        self.assertEqual(compute_confidence(r_code), 1.0)

        r_doc = HybridRetrievalResult(entity="A", type="DocumentEntity (spec)", source="documentation")
        self.assertEqual(compute_confidence(r_doc), 0.95)

    def test_compute_repository_match(self):
        """Verify repository match rewards matching repo and penalizes foreign repos."""
        r_target = HybridRetrievalResult(entity="A", type="class", source="code", repository="repo-core")
        self.assertEqual(compute_repository_match(r_target, "repo-core"), 1.0)

        r_sub = HybridRetrievalResult(entity="A", type="class", source="code", repository="repo-core/auth")
        self.assertEqual(compute_repository_match(r_sub, "repo-core"), 0.75)

        r_foreign = HybridRetrievalResult(entity="A", type="class", source="code", repository="other-org/lib")
        self.assertEqual(compute_repository_match(r_foreign, "repo-core"), 0.10)

        r_unscoped = HybridRetrievalResult(entity="A", type="class", source="code", repository="")
        self.assertEqual(compute_repository_match(r_unscoped, "repo-core"), 0.50)

    def test_compute_source_reliability(self):
        """Verify source reliability distinguishes code AST, specs, docstrings, and heuristics."""
        r_code = HybridRetrievalResult(entity="A", type="CodeSymbol", source="code")
        r_rfc = HybridRetrievalResult(entity="A", type="DocumentEntity (RFC)", source="documentation")
        r_doc = HybridRetrievalResult(entity="A", type="DocumentEntity", source="documentation")
        r_heur = HybridRetrievalResult(entity="A", type="HeuristicEntity", source="heuristic")

        self.assertEqual(compute_source_reliability(r_code), 1.0)
        self.assertEqual(compute_source_reliability(r_rfc), 0.95)
        self.assertEqual(compute_source_reliability(r_doc), 0.85)
        self.assertEqual(compute_source_reliability(r_heur), 0.65)

    def test_compute_token_cost_efficiency(self):
        """Verify smaller, concise items receive higher token efficiency score."""
        r_small = HybridRetrievalResult(
            entity="Small",
            type="class",
            source="code",
            content_snippet="short snippet",  # ~4 tokens
        )
        r_large = HybridRetrievalResult(
            entity="Large",
            type="class",
            source="code",
            content_snippet="word " * 500,     # ~625 tokens
        )

        eff_small = compute_token_cost_efficiency(r_small, reference_tokens=300)
        eff_large = compute_token_cost_efficiency(r_large, reference_tokens=300)

        self.assertGreater(eff_small, eff_large)
        self.assertGreater(eff_small, 0.85)
        self.assertLess(eff_large, 0.35)


class TestConfigurableWeightsAndRanking(unittest.TestCase):
    """Test suite for configurable ranking weights and ranking behavior."""

    def setUp(self):
        self.item_a = HybridRetrievalResult(
            entity="LexicalChampion",
            type="class",
            source="code",
            repository="repo-main",
            scores_breakdown={"lexical_score": 1.0, "semantic_score": 0.2},
            content_snippet="def find(): pass",
        )
        self.item_b = HybridRetrievalResult(
            entity="SemanticChampion",
            type="class",
            source="code",
            repository="repo-main",
            scores_breakdown={"lexical_score": 0.2, "semantic_score": 1.0},
            content_snippet="def find(): pass",
        )
        self.intent = IntentAnalysisResult(
            primary_intent=QueryIntent.EXPLANATION,
            confidence=0.8,
        )

    def test_default_weights_normalization(self):
        """Verify weights normalize non-negative inputs to sum to 1.0."""
        weights = RankingWeights(
            semantic_relevance=2.0,
            lexical_relevance=2.0,
            graph_relevance=0.0,
            freshness=0.0,
            confidence=0.0,
            repository_match=0.0,
            source_reliability=0.0,
            token_cost=0.0,
        )
        norm = weights.normalized()
        self.assertAlmostEqual(norm.semantic_relevance, 0.5, places=4)
        self.assertAlmostEqual(norm.lexical_relevance, 0.5, places=4)
        total = sum(norm.to_dict().values())
        self.assertAlmostEqual(total, 1.0, places=4)

    def test_custom_weights_reorder_candidates(self):
        """Verify custom weight configuration changes ranking order deterministically."""
        # Config 1: Heavily prioritize lexical relevance
        lex_weights = RankingWeights(
            lexical_relevance=1.0,
            semantic_relevance=0.0,
            graph_relevance=0.0,
            freshness=0.0,
            confidence=0.0,
            repository_match=0.0,
            source_reliability=0.0,
            token_cost=0.0,
        )
        ranker_lex = ConfigurableContextRanker(weights=lex_weights, intent_adaptive=False)
        ranked_lex = ranker_lex.rank([self.item_a, self.item_b], self.intent, "query")
        self.assertEqual(ranked_lex[0].entity, "LexicalChampion")

        # Config 2: Heavily prioritize semantic relevance
        sem_weights = RankingWeights(
            lexical_relevance=0.0,
            semantic_relevance=1.0,
            graph_relevance=0.0,
            freshness=0.0,
            confidence=0.0,
            repository_match=0.0,
            source_reliability=0.0,
            token_cost=0.0,
        )
        ranker_sem = ConfigurableContextRanker(weights=sem_weights, intent_adaptive=False)
        ranked_sem = ranker_sem.rank([self.item_a, self.item_b], self.intent, "query")
        self.assertEqual(ranked_sem[0].entity, "SemanticChampion")

    def test_exposure_of_individual_scores_in_breakdown_and_metadata(self):
        """Verify all 8 individual scores and total score are exposed on each ranked candidate."""
        ranker = ConfigurableContextRanker()
        ranked = ranker.rank([self.item_a], self.intent, "query", repository_id="repo-main")
        item = ranked[0]

        # Check breakdown
        bd = item.scores_breakdown
        expected_keys = [
            "semantic_relevance",
            "lexical_relevance",
            "graph_relevance",
            "freshness",
            "confidence",
            "repository_match",
            "source_reliability",
            "token_cost",
            "total_score",
        ]
        for k in expected_keys:
            self.assertIn(k, bd, f"Missing score breakdown key: {k}")
            self.assertIsInstance(bd[k], float)
            self.assertGreaterEqual(bd[k], 0.0)

        # Check metadata
        meta = item.metadata
        self.assertIn("ranking_score", meta)
        self.assertIn("ranking_explanation", meta)
        score_obj = meta["ranking_score"]
        self.assertIn("total_score", score_obj)
        self.assertIn("weights", score_obj)
        self.assertEqual(score_obj["total_score"], bd["total_score"])

    def test_token_cost_penalty_breaks_ties(self):
        """Verify token cost penalty penalizes bloated content snippets when other signals tie."""
        item_concise = HybridRetrievalResult(
            entity="ConciseSymbol",
            type="class",
            source="code",
            scores_breakdown={"lexical_score": 0.8, "semantic_score": 0.8},
            content_snippet="small snippet",
        )
        item_bloated = HybridRetrievalResult(
            entity="BloatedSymbol",
            type="class",
            source="code",
            scores_breakdown={"lexical_score": 0.8, "semantic_score": 0.8},
            content_snippet="word " * 1000,
        )

        weights = RankingWeights(
            lexical_relevance=0.4,
            semantic_relevance=0.4,
            token_cost=0.2,
            graph_relevance=0.0,
            freshness=0.0,
            confidence=0.0,
            repository_match=0.0,
            source_reliability=0.0,
        )
        ranker = ConfigurableContextRanker(weights=weights, intent_adaptive=False)
        ranked = ranker.rank([item_bloated, item_concise], self.intent, "query")

        self.assertEqual(ranked[0].entity, "ConciseSymbol")
        self.assertGreater(
            ranked[0].scores_breakdown["token_cost"],
            ranked[1].scores_breakdown["token_cost"],
        )

    def test_intent_adaptive_weight_modulation(self):
        """Verify adapt_weights_for_intent modulates weights according to engineering intent."""
        base = RankingWeights()

        w_nav = adapt_weights_for_intent(base, QueryIntent.CODE_NAVIGATION)
        self.assertGreater(w_nav.lexical_relevance, base.lexical_relevance)

        w_refact = adapt_weights_for_intent(base, QueryIntent.REFACTORING)
        self.assertGreater(w_refact.graph_relevance, base.graph_relevance)

        w_session = adapt_weights_for_intent(base, QueryIntent.SESSION_CONTINUATION)
        self.assertGreater(w_session.freshness, base.freshness)

    def test_ranking_is_deterministic(self):
        """Verify repeated ranking calls yield identical ordering and scores."""
        ranker = ConfigurableContextRanker()
        run1 = ranker.rank([self.item_a, self.item_b], self.intent, "query")
        run2 = ranker.rank([self.item_a, self.item_b], self.intent, "query")

        self.assertEqual(len(run1), len(run2))
        for r1, r2 in zip(run1, run2):
            self.assertEqual(r1.entity, r2.entity)
            self.assertEqual(r1.score, r2.score)
            self.assertEqual(r1.scores_breakdown, r2.scores_breakdown)

    def test_serialization_roundtrip_ranking_models(self):
        """Verify CandidateRankingScore and RankingWeights roundtrip serialization."""
        weights = RankingWeights(semantic_relevance=0.3, lexical_relevance=0.7)
        w_dict = weights.to_dict()
        w_rebuilt = RankingWeights.from_dict(w_dict)
        self.assertAlmostEqual(w_rebuilt.semantic_relevance, 0.3, places=4)
        self.assertAlmostEqual(w_rebuilt.lexical_relevance, 0.7, places=4)

        score = CandidateRankingScore(
            total_score=0.85,
            semantic_relevance=0.9,
            lexical_relevance=0.8,
            graph_relevance=0.7,
            freshness=0.6,
            confidence=0.95,
            repository_match=1.0,
            source_reliability=1.0,
            token_cost=0.75,
            weights=w_dict,
            explanation="Explanation",
        )
        s_dict = score.to_dict()
        s_rebuilt = CandidateRankingScore.from_dict(s_dict)
        self.assertEqual(s_rebuilt.total_score, 0.85)
        self.assertEqual(s_rebuilt.semantic_relevance, 0.9)
        self.assertEqual(s_rebuilt.explanation, "Explanation")


class TestACOIntegrationWithConfigurableRanker(unittest.TestCase):
    """Integration tests verifying AdaptiveContextOrchestrator utilizes the configurable ranker."""

    def test_aco_end_to_end_includes_ranking_metadata_and_scores(self):
        """Verify ACO orchestration attaches all individual ranking scores and weights."""
        concept_vectors = {
            "authentication": [1.0, 0.9, 0.8, 0.0],
            "auth": [0.95, 0.9, 0.8, 0.0],
            "billing": [0.0, 0.0, 0.0, 1.0],
        }
        provider = MockEmbeddingProvider(concept_vectors=concept_vectors, dimension=4)
        hybrid_service = HybridRetrievalService(embedding_provider=provider)

        custom_weights = RankingWeights(
            semantic_relevance=0.30,
            lexical_relevance=0.30,
            graph_relevance=0.15,
            freshness=0.05,
            confidence=0.05,
            repository_match=0.05,
            source_reliability=0.05,
            token_cost=0.05,
        )
        ranker = ConfigurableContextRanker(weights=custom_weights)
        orchestrator = AdaptiveContextOrchestrator(
            retrieval_service=hybrid_service,
            ranker=ranker,
        )

        symbols = [
            CodeSymbol(
                id="s1",
                name="AuthService",
                file="services/auth.py",
                type="class",
                documentation="Handles authentication.",
                repository_id="repo-main",
            )
        ]

        result = orchestrator.orchestrate(
            query="where is AuthService?",
            user_id="u1",
            repository_id="repo-main",
            symbols=symbols,
        )

        self.assertGreater(len(result.retrieved_results), 0)
        top_item = result.retrieved_results[0]

        # Verify exposed scores
        bd = top_item.scores_breakdown
        self.assertIn("semantic_relevance", bd)
        self.assertIn("lexical_relevance", bd)
        self.assertIn("graph_relevance", bd)
        self.assertIn("freshness", bd)
        self.assertIn("confidence", bd)
        self.assertIn("repository_match", bd)
        self.assertIn("source_reliability", bd)
        self.assertIn("token_cost", bd)
        self.assertIn("total_score", bd)

        # Verify retrieval metadata exposes active ranking weights
        meta = result.retrieval_metadata
        self.assertIn("ranking_weights", meta)
        self.assertIn("semantic_relevance", meta["ranking_weights"])


if __name__ == "__main__":
    unittest.main()
