"""Comprehensive Unit and Integration Tests for Phase 15 Performance and Cost Optimization.

Verifies:
1. Multi-tier LRU cache functionality (eviction, TTL, hits/misses, thread-safety)
2. QueryEmbeddingCache (embedding caching, saved API calls, cost tracking)
3. QueryResultCache and repository-scoped invalidation
4. Query term memoization, intent classification caching, and token estimation caching
5. Hybrid retrieval caching with cache bypass options
6. Zero quality regression and evidence preservation between cold and warm retrieval
7. Automatic repository cache invalidation on incremental updates
"""

from __future__ import annotations

import concurrent.futures
import time
import unittest
from pathlib import Path

from backend.cache import (
    LRUCache,
    QueryEmbeddingCache,
    QueryResultCache,
    SynapseCacheManager,
    default_cache_manager,
)
from backend.hybrid_retrieval import HybridRetrievalService
from backend.incremental_update import (
    IncrementalUpdateService,
    RepositoryCheckpoint,
)
from backend.models import (
    CodeSymbol,
    DocumentEntity,
    GraphEdge,
    OptimizationMode,
    QueryIntent,
)
from backend.orchestrator import (
    AdaptiveContextOrchestrator,
    RuleBasedIntentClassifier,
)
from backend.retrieval import MemoryRetrievalService
from backend.semantic_matcher import MockEmbeddingProvider
from backend.token_optimizer import ContextTier, TokenEstimator


class FakeNeo4jDriver:
    """Mock Neo4j driver for test isolation."""

    def session(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def run(self, *args, **kwargs):
        return []


class TestLRUCacheCore(unittest.TestCase):
    """Test suite for the foundational LRUCache data structure."""

    def test_lru_eviction_when_capacity_exceeded(self):
        cache: LRUCache[str, int] = LRUCache(maxsize=3)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)

        self.assertEqual(len(cache), 3)
        self.assertEqual(cache.get("a"), 1)  # Accessed "a", so "b" is now least recently used

        cache.put("d", 4)  # Should evict "b"
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("a"), 1)
        self.assertEqual(cache.get("c"), 3)
        self.assertEqual(cache.get("d"), 4)
        self.assertEqual(cache.evictions, 1)

    def test_ttl_expiration_and_purging(self):
        cache: LRUCache[str, str] = LRUCache(maxsize=10, ttl_seconds=0.05)
        cache.put("k1", "val1")
        self.assertEqual(cache.get("k1"), "val1")

        time.sleep(0.08)
        # Should be expired on get
        self.assertIsNone(cache.get("k1"))
        self.assertEqual(cache.expirations, 1)

        # Test bulk purge_expired
        cache.put("k2", "val2")
        time.sleep(0.08)
        purged = cache.purge_expired()
        self.assertEqual(purged, 1)
        self.assertEqual(len(cache), 0)

    def test_hit_miss_statistics(self):
        cache: LRUCache[str, int] = LRUCache(maxsize=5)
        cache.put("k1", 100)

        _ = cache.get("k1")  # Hit
        _ = cache.get("k1")  # Hit
        _ = cache.get("missing")  # Miss

        st = cache.stats()
        self.assertEqual(st["hits"], 2)
        self.assertEqual(st["misses"], 1)
        self.assertEqual(st["total_requests"], 3)
        self.assertAlmostEqual(st["hit_rate"], 2 / 3, places=3)

    def test_predicate_invalidation(self):
        cache: LRUCache[str, str] = LRUCache(maxsize=10)
        cache.put("repo_a::q1", "res1")
        cache.put("repo_a::q2", "res2")
        cache.put("repo_b::q1", "res3")

        removed = cache.invalidate_matching(lambda k, entry: k.startswith("repo_a::"))
        self.assertEqual(removed, 2)
        self.assertIsNone(cache.get("repo_a::q1"))
        self.assertIsNone(cache.get("repo_a::q2"))
        self.assertEqual(cache.get("repo_b::q1"), "res3")

    def test_thread_safety_under_concurrent_load(self):
        cache: LRUCache[str, int] = LRUCache(maxsize=50)

        def worker(thread_id: int):
            for i in range(100):
                k = f"key_{i % 30}"
                cache.put(k, thread_id * 1000 + i)
                _ = cache.get(k)
                _ = cache.contains(k)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(worker, tid) for tid in range(8)]
            for f in concurrent.futures.as_completed(futures):
                f.result()

        self.assertLessEqual(len(cache), 50)
        st = cache.stats()
        self.assertGreater(st["hits"], 0)


class TestQueryEmbeddingCache(unittest.TestCase):
    """Test suite for QueryEmbeddingCache."""

    def setUp(self):
        self.emb_cache = QueryEmbeddingCache(maxsize=100)

    def test_embedding_caching_and_cost_metrics(self):
        model = "text-embedding-3-small"
        query = "How does authentication work?"
        vec = [0.1, 0.2, 0.3, 0.4]

        self.assertIsNone(self.emb_cache.get(model, query))
        self.emb_cache.put(model, query, vec)

        # Retrieve cached vector
        cached_vec = self.emb_cache.get(model, query)
        self.assertEqual(cached_vec, vec)

        # Test case-insensitivity and whitespace normalization
        cached_vec2 = self.emb_cache.get(model, "  HOW does Authentication work?  ")
        self.assertEqual(cached_vec2, vec)

        stats = self.emb_cache.stats()
        self.assertEqual(stats["saved_api_calls"], 2)
        self.assertGreater(stats["estimated_saved_cost_usd"], 0.0)


class TestQueryResultCacheAndInvalidation(unittest.TestCase):
    """Test suite for QueryResultCache and repository invalidation."""

    def setUp(self):
        self.res_cache = QueryResultCache(maxsize=100)

    def test_repository_scoped_invalidation(self):
        repo_a = "repo-alpha"
        repo_b = "repo-beta"
        cfg = "cfg123"

        self.res_cache.put(repo_a, "query 1", cfg, {"result": "data_a1"})
        self.res_cache.put(repo_a, "query 2", cfg, {"result": "data_a2"})
        self.res_cache.put(repo_b, "query 1", cfg, {"result": "data_b1"})

        # Invalidate only repo_a
        purged = self.res_cache.invalidate_repository(repo_a)
        self.assertEqual(purged, 2)

        # Verify repo_a is cleared while repo_b remains intact
        self.assertIsNone(self.res_cache.get(repo_a, "query 1", cfg))
        self.assertIsNone(self.res_cache.get(repo_a, "query 2", cfg))
        self.assertIsNotNone(self.res_cache.get(repo_b, "query 1", cfg))


class TestPerformanceSubsystemsIntegration(unittest.TestCase):
    """Integration tests verifying caching behavior across Synapse components."""

    def setUp(self):
        default_cache_manager.clear_all()
        self.symbols = [
            CodeSymbol(
                id="sym_auth",
                name="AuthService",
                file="src/auth.py",
                type="class",
                documentation="Handles authentication and token verification.",
            ),
            CodeSymbol(
                id="sym_jwt",
                name="JWTManager",
                file="src/jwt.py",
                type="class",
                documentation="Encodes and decodes JSON Web Tokens.",
            ),
        ]
        self.entities = [
            DocumentEntity(
                id="doc_auth",
                name="Authentication Architecture",
                type="ArchitectureSpec",
                description="Architecture of authentication workflow.",
                document_path="docs/auth.md",
            )
        ]
        self.edges = [
            GraphEdge(source_id="doc_auth", target_id="sym_auth", relation="SPECIFIES")
        ]
        self.embedding_provider = MockEmbeddingProvider()

    def test_intent_classification_caching(self):
        classifier = RuleBasedIntentClassifier()
        query = "Where is the AuthService class defined?"

        # First run (cache miss)
        res1 = classifier.classify(query)
        self.assertEqual(res1.primary_intent, QueryIntent.CODE_NAVIGATION)
        self.assertEqual(default_cache_manager.intent_cache.misses, 1)

        # Second run (cache hit)
        res2 = classifier.classify(query)
        self.assertEqual(res2.primary_intent, QueryIntent.CODE_NAVIGATION)
        self.assertEqual(default_cache_manager.intent_cache.hits, 1)
        self.assertEqual(res1.extracted_entities, res2.extracted_entities)

    def test_query_term_extraction_memoization(self):
        service = MemoryRetrievalService(driver=FakeNeo4jDriver())
        query = "Explain how JWTManager handles RS256 token verification"

        terms1 = service.extract_query_terms(query)
        self.assertIn("jwtmanager", terms1)
        self.assertIn("rs256", terms1)
        self.assertEqual(default_cache_manager.query_terms_cache.misses, 1)

        terms2 = service.extract_query_terms(query)
        self.assertEqual(terms1, terms2)
        self.assertEqual(default_cache_manager.query_terms_cache.hits, 1)

    def test_token_estimation_caching(self):
        service = HybridRetrievalService(
            retrieval_service=MemoryRetrievalService(driver=FakeNeo4jDriver()),
            embedding_provider=self.embedding_provider,
        )
        resp = service.retrieve(
            user_id="user1",
            repository_id="repo1",
            query="AuthService",
            symbols=self.symbols,
            entities=self.entities,
            use_cache=False,
        )
        item = resp.results[0]

        # First estimation
        est1 = TokenEstimator.estimate_item(item, tier=ContextTier.FULL)
        self.assertGreater(est1, 0)
        self.assertEqual(default_cache_manager.token_estimation_cache.misses, 1)

        # Second estimation (cache hit)
        est2 = TokenEstimator.estimate_item(item, tier=ContextTier.FULL)
        self.assertEqual(est1, est2)
        self.assertEqual(default_cache_manager.token_estimation_cache.hits, 1)

    def test_hybrid_retrieval_caching_and_fidelity(self):
        service = HybridRetrievalService(
            retrieval_service=MemoryRetrievalService(driver=FakeNeo4jDriver()),
            embedding_provider=self.embedding_provider,
        )
        user_id = "user-123"
        repo_id = "repo-xyz"
        query = "How does authentication work?"

        # Cold retrieval
        resp_cold = service.retrieve(
            user_id=user_id,
            repository_id=repo_id,
            query=query,
            symbols=self.symbols,
            entities=self.entities,
            edges=self.edges,
            use_cache=True,
        )
        self.assertFalse(resp_cold.retrieval_stats.get("from_cache"))
        self.assertGreater(len(resp_cold.results), 0)

        # Warm retrieval
        resp_warm = service.retrieve(
            user_id=user_id,
            repository_id=repo_id,
            query=query,
            symbols=self.symbols,
            entities=self.entities,
            edges=self.edges,
            use_cache=True,
        )
        self.assertTrue(resp_warm.retrieval_stats.get("from_cache"))
        self.assertEqual(len(resp_cold.results), len(resp_warm.results))

        # Bit-for-bit fidelity: identical entities and scores
        for c, w in zip(resp_cold.results, resp_warm.results):
            self.assertEqual(c.entity, w.entity)
            self.assertEqual(c.score, w.score)
            self.assertEqual(c.file_or_document, w.file_or_document)

        # Bypass cache verification
        resp_bypass = service.retrieve(
            user_id=user_id,
            repository_id=repo_id,
            query=query,
            symbols=self.symbols,
            entities=self.entities,
            edges=self.edges,
            use_cache=False,
        )
        self.assertFalse(resp_bypass.retrieval_stats.get("from_cache"))

    def test_incremental_update_purges_repository_cache(self):
        user_id = "dev"
        repo_id = "test-repo-cache"
        service = HybridRetrievalService(
            retrieval_service=MemoryRetrievalService(driver=FakeNeo4jDriver()),
            embedding_provider=self.embedding_provider,
        )

        # Populate cache
        service.retrieve(
            user_id=user_id,
            repository_id=repo_id,
            query="test query",
            symbols=self.symbols,
            entities=self.entities,
            use_cache=True,
        )
        self.assertGreater(len(default_cache_manager.retrieval_cache.cache), 0)

        # Invalidate via SynapseCacheManager
        inv = default_cache_manager.invalidate_repository(repo_id)
        self.assertGreater(inv["retrieval_results_invalidated"], 0)

        # Check that test query is now cleared from cache
        self.assertIsNone(
            default_cache_manager.retrieval_cache.get(repo_id, "test query", "any_cfg")
        )

    def test_incremental_update_service_triggers_invalidation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_path = Path(tmp_dir)
            (repo_path / "app.py").write_text("def hello(): pass\n", encoding="utf-8")
            repo_id = "temp-repo-inv"
            default_cache_manager.retrieval_cache.put(repo_id, "hello", "cfg", {"res": 1})
            self.assertIsNotNone(default_cache_manager.retrieval_cache.get(repo_id, "hello", "cfg"))

            update_service = IncrementalUpdateService()
            update_service.run_update(repo_path, user_id="u1", repository_id=repo_id)

            # Must have been cleared automatically
            self.assertIsNone(default_cache_manager.retrieval_cache.get(repo_id, "hello", "cfg"))


if __name__ == "__main__":
    unittest.main()
