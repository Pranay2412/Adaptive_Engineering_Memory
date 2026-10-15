"""Unit and Integration Tests for the Systematic Ablation Study Suite.

Phase 14 Verification:
1. FixedStrategyIntentClassifier parameter override.
2. AblationRunnerFactory configuration validation across 11 arms.
3. Execution and retrieval metric capture across isolated modality arms.
4. Memory ablation evidence impact (absence vs presence of ADRs).
5. Cross-arm delta computation (retrieval F1, token reduction, memory gain).
6. Scientific report generation with structured comparison tables.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.models import CodeSymbol, DocumentEntity, Memory
from backend.orchestrator import AdaptiveContextOrchestrator
from backend.semantic_matcher import MockEmbeddingProvider
from benchmarks.ablation_study import (
    AblationAggregator,
    AblationRunner,
    FixedStrategyIntentClassifier,
    build_ablation_runners,
)
from benchmarks.eval_framework import (
    AnswerQualityMetrics,
    EvaluationConfig,
    EvaluationHarness,
    EvaluationJudge,
    QueryItem,
    RetrievalMetrics,
    TrialRecord,
)
from benchmarks.token_benchmark import MockBenchmarkLLM


# ---------------------------------------------------------------------------
# 1. Fixed Strategy Intent Classifier Test
# ---------------------------------------------------------------------------

def test_fixed_strategy_classifier_overrides():
    classifier = FixedStrategyIntentClassifier(
        lexical_weight=0.90,
        semantic_weight=0.10,
        graph_weight=0.00,
        max_hops=0,
        limit=8,
        name="test_override",
    )

    res = classifier.classify("How does authentication work with JWT?")
    strat = res.retrieval_strategy

    assert strat["lexical_weight"] == 0.90
    assert strat["semantic_weight"] == 0.10
    assert strat["graph_weight"] == 0.00
    assert strat["max_hops"] == 0
    assert strat["limit"] == 8


# ---------------------------------------------------------------------------
# 2. Ablation Runner Factory Test
# ---------------------------------------------------------------------------

def test_ablation_runner_factory_arms(tmp_path):
    symbols = [CodeSymbol(id="sym1", name="test_sym", type="class", file="backend/test.py")]
    entities = [DocumentEntity(id="doc1", name="test_doc", type="architecture_spec", source_document="docs/test.md")]
    memories = [Memory(id="mem1", title="ADR 1", content="Rationale")]

    runners = build_ablation_runners(tmp_path, symbols, entities, memories)

    expected_arms = {
        "retrieval_lexical_only",
        "retrieval_semantic_only",
        "retrieval_structural_only",
        "retrieval_hybrid_no_graph",
        "retrieval_hybrid_full",
        "aco_off_unbounded",
        "aco_none_truncated",
        "aco_deterministic",
        "aco_llm_compressed",
        "memory_off",
        "memory_on",
    }

    assert expected_arms.issubset(set(runners.keys()))
    assert runners["memory_off"].memories == []
    assert len(runners["memory_on"].memories) == 1
    assert runners["aco_off_unbounded"].budget is None
    assert runners["aco_deterministic"].budget == 1500


# ---------------------------------------------------------------------------
# 3. Execution & Retrieval Metric Capture Test
# ---------------------------------------------------------------------------

def _make_test_orchestrator() -> AdaptiveContextOrchestrator:
    from backend.hybrid_retrieval import HybridRetrievalService
    from backend.retrieval import MemoryRetrievalService
    from backend.semantic_matcher import MockEmbeddingProvider
    from benchmarks.ablation_study import OfflineNeo4jDriver
    ret = MemoryRetrievalService(driver=OfflineNeo4jDriver())
    hybrid = HybridRetrievalService(retrieval_service=ret, embedding_provider=MockEmbeddingProvider())
    return AdaptiveContextOrchestrator(retrieval_service=hybrid)


def test_ablation_runner_execution():
    symbols = [
        CodeSymbol(id="sym1", name="models", type="module", file="backend/models.py"),
        CodeSymbol(id="sym2", name="bridge", type="module", file="backend/bridge.py"),
    ]
    orchestrator = _make_test_orchestrator()
    runner = AblationRunner(
        name="test_runner",
        orchestrator=orchestrator,
        symbols=symbols,
        optimization_mode="deterministic",
        budget=1000,
    )

    query = QueryItem(
        query_id="TEST-Q",
        category="structural",
        query="What modules import models.py?",
        expected_evidence=["backend/models.py", "backend/bridge.py"],
    )

    context, answer, ret_metrics = runner.execute_query(
        query_item=query,
        budget=1000,
        llm_client=MockBenchmarkLLM(),
    )

    assert "models" in context.lower()
    assert ret_metrics.context_tokens_precise > 0
    assert ret_metrics.budget == 1000
    assert answer is not None


# ---------------------------------------------------------------------------
# 4. Memory Ablation Evidence Impact Test
# ---------------------------------------------------------------------------

def test_memory_ablation_evidence_impact():
    symbols = [CodeSymbol(id="sym1", name="TokenService", type="class", file="backend/security.py")]
    adr_memory = Memory(
        id="mem-auth-rs256",
        title="Architecture Decision: Sign JWT tokens with asymmetric RS256",
        content="Chose RS256 with private key rotation over HS256 to eliminate shared symmetric secret vulnerabilities.",
        category="architecture_decision",
        scope="repository",
    )

    orch = _make_test_orchestrator()
    runner_mem_off = AblationRunner(
        name="memory_off",
        orchestrator=orch,
        symbols=symbols,
        memories=[],
        budget=1500,
    )
    runner_mem_on = AblationRunner(
        name="memory_on",
        orchestrator=orch,
        symbols=symbols,
        memories=[adr_memory],
        budget=1500,
    )

    query = QueryItem(
        query_id="SYN-Q05",
        category="architecture",
        query="Why was RS256 chosen for authentication?",
        expected_evidence=["mem-auth-rs256", "asymmetric RS256"],
    )

    ctx_off, _, ret_off = runner_mem_off.execute_query(query, 1500, MockBenchmarkLLM())
    ctx_on, _, ret_on = runner_mem_on.execute_query(query, 1500, MockBenchmarkLLM())

    assert "RS256" not in ctx_off or "mem-auth-rs256" not in ctx_off
    assert "mem-auth-rs256" in ctx_on or "RS256" in ctx_on
    assert ret_on.context_tokens_precise >= ret_off.context_tokens_precise


# ---------------------------------------------------------------------------
# 5. Cross-Arm Delta Calculations Test
# ---------------------------------------------------------------------------

def test_ablation_deltas_computation():
    def make_rec(cfg: str, ctx_tok: int, qual: float, f1: float) -> TrialRecord:
        return TrialRecord(
            trial_id=f"t_{cfg}",
            run_id="run_delta",
            repetition_index=0,
            query_id="Q1",
            query_text="query",
            category="cat",
            configuration_name=cfg,
            git_commit="git123",
            timestamp="2026-10-15T00:00:00Z",
            retrieval=RetrievalMetrics(context_tokens_precise=ctx_tok, evidence_f1=f1),
            quality=AnswerQualityMetrics(overall_score=qual),
            status="success",
        )

    records = [
        make_rec("retrieval_hybrid_full", ctx_tok=800, qual=4.5, f1=0.75),
        make_rec("retrieval_lexical_only", ctx_tok=850, qual=3.8, f1=0.50),
        make_rec("aco_off_unbounded", ctx_tok=2000, qual=4.5, f1=0.75),
        make_rec("aco_deterministic", ctx_tok=800, qual=4.5, f1=0.75),
        make_rec("memory_off", ctx_tok=600, qual=3.0, f1=0.40),
        make_rec("memory_on", ctx_tok=800, qual=4.5, f1=0.75),
    ]

    agg = AblationAggregator(records)
    deltas = agg.compute_ablation_deltas()

    ret_deltas = deltas["retrieval_modality_deltas"]["retrieval_lexical_only"]
    assert ret_deltas["delta_f1_vs_full"] == -0.25
    assert ret_deltas["delta_quality_vs_full"] == -0.7

    aco_deltas = deltas["aco_compression_deltas"]["aco_deterministic"]
    assert aco_deltas["token_reduction_pct_vs_unbounded"] == 60.0
    assert aco_deltas["delta_quality_vs_unbounded"] == 0.0

    mem_deltas = deltas["memory_modality_deltas"]
    assert mem_deltas["quality_gain_from_memory"] == 1.5
    assert mem_deltas["f1_gain_from_memory"] == 0.35


# ---------------------------------------------------------------------------
# 6. Report Generation Test
# ---------------------------------------------------------------------------

def test_ablation_markdown_report_generation(tmp_path):
    rec = TrialRecord(
        trial_id="t1",
        run_id="run1",
        repetition_index=0,
        query_id="Q1",
        query_text="query",
        category="cat",
        configuration_name="retrieval_hybrid_full",
        git_commit="c",
        timestamp="2026-10-15T00:00:00Z",
        retrieval=RetrievalMetrics(context_tokens_precise=800),
        quality=AnswerQualityMetrics(overall_score=4.5),
        status="success",
    )

    agg = AblationAggregator([rec])
    md_file = tmp_path / "ablation_report.md"
    agg.export_ablation_markdown_report(md_file)

    assert md_file.exists()
    content = md_file.read_text(encoding="utf-8")
    assert "# Synapse Ablation Study Report (Phase 14)" in content
    assert "Group 1: Retrieval Modality Ablation" in content
    assert "Group 2: Context Compression & Packing Ablation (ACO)" in content
    assert "Group 3: Engineering Memory Layer Ablation" in content
    assert "Negative & Neutral Findings" in content
