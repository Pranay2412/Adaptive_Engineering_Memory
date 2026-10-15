"""Comprehensive Performance and Cost Optimization Benchmark for Synapse.

Phase 15: Empirical latency profiling, multi-tier caching evaluation, and cost analysis:
1. Stage-by-stage latency breakdown:
   - Query term extraction & normalization
   - Intent classification (rule-based)
   - Dense vector embedding generation
   - Semantic similarity search (symbols & entities)
   - In-memory graph traversal (1-hop, 2-hop)
   - Multi-channel fusion and Reciprocal Rank Fusion (RRF) ranking
   - Token estimation and ACO context packing
2. Cold vs. Warm latency comparison across all 5 canonical queries:
   - Measures min, max, mean, median, p95, and standard deviation
   - Quantifies speedup factor and latency reduction %
3. Cache invalidation verification:
   - Validates that repository mutation purges stale query caches
   - Validates seamless cold-to-warm transition
4. Retrieval fidelity and evidence preservation:
   - Confirms zero degradation of retrieved evidence or ranking order
5. Cost and API call savings:
   - Quantifies saved embedding model calls and token calculation overhead
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Ensure repo root is on sys.path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

load_dotenv()

from backend.cache import default_cache_manager
from backend.hybrid_retrieval import HybridRetrievalService, SemanticKnowledgeIndex
from backend.models import (
    CodeSymbol,
    DocumentEntity,
    GraphEdge,
    Memory,
    OptimizationMode,
    QueryIntent,
)
from backend.orchestrator import (
    AdaptiveContextOrchestrator,
    RuleBasedIntentClassifier,
)
from backend.retrieval import MemoryRetrievalService
from backend.semantic_matcher import MockEmbeddingProvider
from backend.token_optimizer import ContextTier, TokenEstimator, render_candidate_item
from benchmarks.eval_framework import QueryDataset, get_git_commit_hash
from benchmarks.run_benchmark import load_repository_context

logger = logging.getLogger("performance_benchmark")


class FakeDriver:
    """Mock Neo4j driver context manager for offline benchmarking."""
    def session(self):
        return self
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def run(self, *args, **kwargs):
        return []


def calculate_percentile(data: list[float], percentile: float) -> float:
    """Calculate the p-th percentile from sorted list of numbers."""
    if not data:
        return 0.0
    s = sorted(data)
    idx = (len(s) - 1) * (percentile / 100.0)
    floor_idx = math.floor(idx)
    ceil_idx = math.ceil(idx)
    if floor_idx == ceil_idx:
        return s[int(idx)]
    return s[floor_idx] * (ceil_idx - idx) + s[ceil_idx] * (idx - floor_idx)


def profile_retrieval_stages(
    query: str,
    repository_id: str,
    user_id: str,
    symbols: list[CodeSymbol],
    entities: list[DocumentEntity],
    edges: list[GraphEdge],
    embedding_provider: MockEmbeddingProvider,
) -> dict[str, float]:
    """Isolate and measure the exact duration of each retrieval stage in milliseconds."""
    timings: dict[str, float] = {}

    # 1. Query term extraction
    retrieval_service = MemoryRetrievalService(driver=FakeDriver())
    t0 = time.perf_counter_ns()
    terms = retrieval_service.extract_query_terms(query)
    timings["term_extraction_ms"] = round((time.perf_counter_ns() - t0) / 1_000_000, 3)

    # 2. Intent classification
    classifier = RuleBasedIntentClassifier()
    t0 = time.perf_counter_ns()
    intent_res = classifier.classify(query)
    timings["intent_classification_ms"] = round((time.perf_counter_ns() - t0) / 1_000_000, 3)

    # 3. Dense vector embedding generation
    t0 = time.perf_counter_ns()
    query_vec = embedding_provider.embed_query(query, use_cache=False)
    timings["embedding_generation_ms"] = round((time.perf_counter_ns() - t0) / 1_000_000, 3)

    # 4. In-memory semantic search
    index = SemanticKnowledgeIndex(symbols=symbols, entities=entities, provider=embedding_provider)
    t0 = time.perf_counter_ns()
    sym_matches = index.query_symbols(query_vec, top_n=10)
    ent_matches = index.query_entities(query_vec, top_n=10)
    timings["semantic_search_ms"] = round((time.perf_counter_ns() - t0) / 1_000_000, 3)

    # 5. In-memory graph traversal
    hybrid_service = HybridRetrievalService(
        retrieval_service=retrieval_service,
        embedding_provider=embedding_provider,
    )
    syms_by_id = {s.id: s for s in symbols}
    ents_by_id = {e.id: e for e in entities}
    seed_ids = {s.id for s, _ in sym_matches[:4]} | {e.id for e, _ in ent_matches[:4]}
    t0 = time.perf_counter_ns()
    traversed = hybrid_service._traverse_in_memory_graph(
        seed_ids=seed_ids,
        symbols_by_id=syms_by_id,
        entities_by_id=ents_by_id,
        edges=edges,
        max_hops=2,
    )
    timings["graph_traversal_ms"] = round((time.perf_counter_ns() - t0) / 1_000_000, 3)

    # 6. Multi-channel fusion and ranking
    t0 = time.perf_counter_ns()
    resp = hybrid_service.retrieve(
        user_id=user_id,
        repository_id=repository_id,
        query=query,
        symbols=symbols,
        entities=entities,
        edges=edges,
        max_hops=2,
        limit=10,
        use_cache=False,
    )
    timings["fusion_ranking_ms"] = round((time.perf_counter_ns() - t0) / 1_000_000, 3)

    # 7. Token estimation across candidate tiers
    t0 = time.perf_counter_ns()
    for item in resp.results:
        TokenEstimator.estimate_item(item, tier=ContextTier.FULL)
        TokenEstimator.estimate_item(item, tier=ContextTier.COMPACT)
        TokenEstimator.estimate_item(item, tier=ContextTier.MINIMAL)
    timings["token_estimation_ms"] = round((time.perf_counter_ns() - t0) / 1_000_000, 3)

    # 8. ACO context packing
    orchestrator = AdaptiveContextOrchestrator(
        retrieval_service=hybrid_service,
        classifier=classifier,
    )
    t0 = time.perf_counter_ns()
    orch_ctx = orchestrator.orchestrate(
        query=query,
        user_id=user_id,
        repository_id=repository_id,
        max_tokens=1500,
        optimization_mode=OptimizationMode.DETERMINISTIC,
        symbols=symbols,
        entities=entities,
        edges=edges,
    )
    timings["aco_packing_ms"] = round((time.perf_counter_ns() - t0) / 1_000_000, 3)

    return timings


class PerformanceBenchmarkRunner:
    """Executes systematic cold vs warm performance benchmarks across canonical queries."""

    def __init__(
        self,
        dataset_path: Path | None = None,
        iterations: int = 5,
        output_dir: Path | None = None,
    ) -> None:
        self.iterations = iterations
        self.dataset_path = dataset_path or (repo_root / "benchmarks" / "datasets" / "queries_v1.json")
        self.output_dir = output_dir or (repo_root / "benchmarks" / "results" / "performance")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.dataset = QueryDataset(self.dataset_path)
        self.symbols, self.entities, self.memories = load_repository_context(repo_root)

        # Build test graph edges
        self.edges = [
            GraphEdge(source_id=self.entities[0].id, target_id=self.symbols[0].id, relation="SPECIFIES")
            if self.entities and self.symbols else GraphEdge(source_id="doc1", target_id="sym1", relation="SPECIFIES"),
            GraphEdge(source_id=self.symbols[0].id, target_id=self.symbols[1].id, relation="CALLS")
            if len(self.symbols) > 1 else GraphEdge(source_id="sym1", target_id="sym2", relation="CALLS"),
        ]

        self.embedding_provider = MockEmbeddingProvider()

    def run_benchmark(self) -> dict[str, Any]:
        """Run complete performance profiling, cold/warm benchmarking, and invalidation verification."""
        print("=" * 80)
        print("SYNAPSE PHASE 15 PERFORMANCE & COST OPTIMIZATION BENCHMARK")
        print("=" * 80)
        print(f"Dataset: {self.dataset.name} (v{self.dataset.version})")
        print(f"Iterations: {self.iterations} cold runs + {self.iterations} warm runs per query")
        print(f"Target directory: {self.output_dir}")
        print("-" * 80)

        user_id = "bench-user"
        repo_id = "synapse-core"
        commit_hash = get_git_commit_hash(repo_root)

        # -------------------------------------------------------------
        # 1. Stage-by-Stage Latency Profiling
        # -------------------------------------------------------------
        print("\n[Stage 1/4] Profiling isolated retrieval sub-operations...")
        stage_breakdowns: dict[str, dict[str, float]] = {}
        for q in self.dataset.queries:
            breakdown = profile_retrieval_stages(
                query=q.query,
                repository_id=repo_id,
                user_id=user_id,
                symbols=self.symbols,
                entities=self.entities,
                edges=self.edges,
                embedding_provider=self.embedding_provider,
            )
            stage_breakdowns[q.query_id] = breakdown
            print(f"  [{q.query_id}] {q.category.upper()}: {breakdown}")

        # Compute average stage latencies across queries
        avg_stage_latencies: dict[str, float] = {}
        for stage in next(iter(stage_breakdowns.values())).keys():
            vals = [sb[stage] for sb in stage_breakdowns.values()]
            avg_stage_latencies[stage] = round(statistics.mean(vals), 3)

        # -------------------------------------------------------------
        # 2. Cold vs. Warm Latency Benchmark
        # -------------------------------------------------------------
        print("\n[Stage 2/4] Measuring cold vs. warm end-to-end retrieval latency...")
        query_results: list[dict[str, Any]] = []

        orchestrator = AdaptiveContextOrchestrator(
            retrieval_service=HybridRetrievalService(
                retrieval_service=MemoryRetrievalService(driver=FakeDriver()),
                embedding_provider=self.embedding_provider,
            ),
            classifier=RuleBasedIntentClassifier(),
        )

        for q in self.dataset.queries:
            cold_timings: list[float] = []
            warm_timings: list[float] = []
            cold_evidence_ids: list[str] = []
            warm_evidence_ids: list[str] = []

            # 2a. Cold executions (purging all caches before each run)
            for _ in range(self.iterations):
                default_cache_manager.clear_all()
                t0 = time.perf_counter_ns()
                ctx = orchestrator.orchestrate(
                    query=q.query,
                    user_id=user_id,
                    repository_id=repo_id,
                    max_tokens=1500,
                    optimization_mode=OptimizationMode.DETERMINISTIC,
                    symbols=self.symbols,
                    entities=self.entities,
                    edges=self.edges,
                )
                elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000
                cold_timings.append(elapsed_ms)
                if not cold_evidence_ids:
                    cold_evidence_ids = [item.entity for item in ctx.retrieved_results]

            # 2b. Warm executions (leaving caches populated)
            for _ in range(self.iterations):
                t0 = time.perf_counter_ns()
                ctx = orchestrator.orchestrate(
                    query=q.query,
                    user_id=user_id,
                    repository_id=repo_id,
                    max_tokens=1500,
                    optimization_mode=OptimizationMode.DETERMINISTIC,
                    symbols=self.symbols,
                    entities=self.entities,
                    edges=self.edges,
                )
                elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000
                warm_timings.append(elapsed_ms)
                if not warm_evidence_ids:
                    warm_evidence_ids = [item.entity for item in ctx.retrieved_results]

            # Calculate statistical aggregates
            cold_median = round(statistics.median(cold_timings), 2)
            cold_mean = round(statistics.mean(cold_timings), 2)
            cold_p95 = round(calculate_percentile(cold_timings, 95), 2)
            cold_stdev = round(statistics.stdev(cold_timings) if len(cold_timings) > 1 else 0.0, 2)

            warm_median = round(statistics.median(warm_timings), 2)
            warm_mean = round(statistics.mean(warm_timings), 2)
            warm_p95 = round(calculate_percentile(warm_timings, 95), 2)
            warm_stdev = round(statistics.stdev(warm_timings) if len(warm_timings) > 1 else 0.0, 2)

            speedup = round(cold_median / warm_median, 2) if warm_median > 0 else 1.0
            reduction_pct = round(((cold_median - warm_median) / cold_median) * 100, 2) if cold_median > 0 else 0.0

            # Fidelity verification: evidence must match exactly
            evidence_identical = cold_evidence_ids == warm_evidence_ids

            record = {
                "query_id": q.query_id,
                "category": q.category,
                "query": q.query,
                "cold_median_ms": cold_median,
                "cold_mean_ms": cold_mean,
                "cold_p95_ms": cold_p95,
                "cold_stdev_ms": cold_stdev,
                "cold_min_ms": round(min(cold_timings), 2),
                "cold_max_ms": round(max(cold_timings), 2),
                "warm_median_ms": warm_median,
                "warm_mean_ms": warm_mean,
                "warm_p95_ms": warm_p95,
                "warm_stdev_ms": warm_stdev,
                "warm_min_ms": round(min(warm_timings), 2),
                "warm_max_ms": round(max(warm_timings), 2),
                "speedup_ratio": speedup,
                "latency_reduction_pct": reduction_pct,
                "evidence_identical": evidence_identical,
                "evidence_count": len(cold_evidence_ids),
            }
            query_results.append(record)

            print(
                f"  [{q.query_id}] Cold Median: {cold_median:5.2f}ms | "
                f"Warm Median: {warm_median:5.2f}ms | "
                f"Speedup: {speedup:4.2f}x (-{reduction_pct:.1f}%) | "
                f"Fidelity: {'PASS' if evidence_identical else 'FAIL'}"
            )

        # -------------------------------------------------------------
        # 3. Cache Invalidation Verification
        # -------------------------------------------------------------
        print("\n[Stage 3/4] Verifying cache invalidation on repository update...")
        test_query = self.dataset.queries[0].query
        # 3a. Warm query (in cache)
        t0 = time.perf_counter_ns()
        orchestrator.orchestrate(
            query=test_query,
            user_id=user_id,
            repository_id=repo_id,
            max_tokens=1500,
            symbols=self.symbols,
            entities=self.entities,
            edges=self.edges,
        )
        warm_pre_inv = (time.perf_counter_ns() - t0) / 1_000_000

        # 3b. Trigger repository invalidation
        inv_stats = default_cache_manager.invalidate_repository(repo_id)

        # 3c. Query immediately after invalidation (must miss cache and recompute)
        t0 = time.perf_counter_ns()
        orchestrator.orchestrate(
            query=test_query,
            user_id=user_id,
            repository_id=repo_id,
            max_tokens=1500,
            symbols=self.symbols,
            entities=self.entities,
            edges=self.edges,
        )
        cold_post_inv = (time.perf_counter_ns() - t0) / 1_000_000

        # 3d. Next query should be warm again
        t0 = time.perf_counter_ns()
        orchestrator.orchestrate(
            query=test_query,
            user_id=user_id,
            repository_id=repo_id,
            max_tokens=1500,
            symbols=self.symbols,
            entities=self.entities,
            edges=self.edges,
        )
        warm_post_inv = (time.perf_counter_ns() - t0) / 1_000_000

        invalidation_report = {
            "repository_id": repo_id,
            "purged_records": inv_stats.get("retrieval_results_invalidated", 0),
            "warm_pre_invalidation_ms": round(warm_pre_inv, 2),
            "cold_post_invalidation_ms": round(cold_post_inv, 2),
            "warm_resumed_ms": round(warm_post_inv, 2),
            "invalidation_successful": cold_post_inv > warm_pre_inv and warm_post_inv < cold_post_inv,
        }
        print(f"  Invalidated cache records: {invalidation_report['purged_records']}")
        print(f"  Warm Pre-Invalidation:     {invalidation_report['warm_pre_invalidation_ms']}ms")
        print(f"  Cold Post-Invalidation:    {invalidation_report['cold_post_invalidation_ms']}ms (re-executed fresh)")
        print(f"  Warm Resumed:              {invalidation_report['warm_resumed_ms']}ms (re-cached)")
        print(f"  Invalidation Verified:     {'PASS' if invalidation_report['invalidation_successful'] else 'FAIL'}")

        # -------------------------------------------------------------
        # 4. Aggregates, Cost Savings, and Global Stats
        # -------------------------------------------------------------
        print("\n[Stage 4/4] Aggregating performance results and cost telemetry...")
        overall_cold_median = round(statistics.median([r["cold_median_ms"] for r in query_results]), 2)
        overall_warm_median = round(statistics.median([r["warm_median_ms"] for r in query_results]), 2)
        overall_speedup = round(overall_cold_median / overall_warm_median, 2) if overall_warm_median > 0 else 1.0
        overall_reduction_pct = round(((overall_cold_median - overall_warm_median) / overall_cold_median) * 100, 2)

        cache_stats = default_cache_manager.get_global_stats()

        summary = {
            "benchmark_name": "synapse_phase_15_performance_optimization",
            "git_commit": commit_hash,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "dataset_version": self.dataset.version,
            "iterations_per_arm": self.iterations,
            "overall_metrics": {
                "overall_cold_median_ms": overall_cold_median,
                "overall_warm_median_ms": overall_warm_median,
                "overall_speedup_ratio": overall_speedup,
                "overall_latency_reduction_pct": overall_reduction_pct,
                "all_evidence_preserved": all(r["evidence_identical"] for r in query_results),
            },
            "per_query_results": query_results,
            "average_stage_latencies_ms": avg_stage_latencies,
            "stage_breakdown_by_query": stage_breakdowns,
            "invalidation_verification": invalidation_report,
            "cache_telemetry": cache_stats,
        }

        # -------------------------------------------------------------
        # 5. Export Artifacts
        # -------------------------------------------------------------
        json_path = self.output_dir / "performance_results.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        print(f"\n--> Exported JSON report: {json_path}")

        csv_path = self.output_dir / "performance_results.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            fieldnames = [
                "query_id",
                "category",
                "cold_median_ms",
                "cold_p95_ms",
                "warm_median_ms",
                "warm_p95_ms",
                "speedup_ratio",
                "latency_reduction_pct",
                "evidence_identical",
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in query_results:
                writer.writerow({k: r[k] for k in fieldnames})
        print(f"--> Exported CSV summary: {csv_path}")

        stage_json_path = self.output_dir / "stage_latency_breakdown.json"
        with open(stage_json_path, "w", encoding="utf-8") as f:
            json.dump({
                "average_stage_latencies_ms": avg_stage_latencies,
                "by_query": stage_breakdowns,
            }, f, indent=2)
        print(f"--> Exported Stage Breakdown: {stage_json_path}")

        print("\n" + "=" * 80)
        print(f"BENCHMARK COMPLETE: Overall Speedup: {overall_speedup}x (-{overall_reduction_pct}%)")
        print("=" * 80)
        return summary


def main():
    parser = argparse.ArgumentParser(description="Run Synapse Phase 15 Performance Benchmark")
    parser.add_argument("--iterations", type=int, default=5, help="Number of cold and warm iterations (default: 5)")
    args = parser.parse_args()

    runner = PerformanceBenchmarkRunner(iterations=args.iterations)
    runner.run_benchmark()


if __name__ == "__main__":
    main()
