"""Comprehensive Unit and Integration Tests for the Repeatable Evaluation Framework.

Phase 13 Contract & Acceptance Verification:
1. Query dataset validation, schema enforcement, and versioning.
2. Evidence precision, recall, and F1 calculation.
3. Scoring rubric evaluation (full, partial, evasive disclaimer).
4. Deterministic execution reproducibility across repeated trials.
5. Checkpoint recovery and resumability without duplicate work.
6. Graceful failure trapping with explicit error status and retry tracking (no silent omission).
7. Aggregator integrity checks (duplicate detection, missing rows handling).
8. Separation of retrieval metrics from answer quality metrics.
9. Verified multi-format exports (JSON, CSV, Markdown).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.eval_framework import (
    AnswerQualityMetrics,
    BaseEvaluationRunner,
    EvaluationAggregator,
    EvaluationConfig,
    EvaluationHarness,
    EvaluationJudge,
    GraphifyBaselineRunner,
    QueryDataset,
    QueryItem,
    RetrievalMetrics,
    SynapseAcoRunner,
    TrialRecord,
    compute_evidence_overlap,
)
from benchmarks.token_benchmark import MockBenchmarkLLM


# ---------------------------------------------------------------------------
# 1. Dataset Loading & Schema Validation
# ---------------------------------------------------------------------------

def test_query_dataset_loading_and_validation(tmp_path):
    dataset_file = Path("benchmarks/datasets/queries_v1.json")
    assert dataset_file.exists(), "queries_v1.json must exist in benchmarks/datasets"

    ds = QueryDataset(dataset_file)
    assert ds.version == "1.0.0"
    assert len(ds) == 5

    q1 = ds.get_by_id("SYN-Q01")
    assert q1 is not None
    assert q1.category == "structural"
    assert len(q1.expected_evidence) > 0
    assert "5" in q1.scoring_rubric
    assert "1" in q1.scoring_rubric

    # Verify invalid query rejection
    bad_item = QueryItem(query_id="", category="cat", query="test")
    with pytest.raises(ValueError, match="query_id cannot be empty"):
        bad_item.validate()


# ---------------------------------------------------------------------------
# 2. Evidence Precision, Recall, and F1 Overlap
# ---------------------------------------------------------------------------

def test_evidence_overlap_metrics():
    expected = [
        "backend/models.py",
        "backend/db_loader.py",
        "backend/bridge.py",
    ]
    retrieved = [
        "backend/models.py defines CodeSymbol",
        "backend/db_loader.py initializes store",
        "unrelated/file.py does something else",
    ]

    p, r, f1 = compute_evidence_overlap(expected, retrieved)
    assert r == pytest.approx(2.0 / 3.0, rel=1e-2)
    assert p > 0.0
    assert 0.0 < f1 <= 1.0

    # Edge cases
    assert compute_evidence_overlap([], ["some_file"]) == (1.0, 1.0, 1.0)
    assert compute_evidence_overlap(["some_file"], []) == (0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# 3. Scoring Rubric Evaluation
# ---------------------------------------------------------------------------

def test_evaluation_judge_scoring():
    judge = EvaluationJudge(llm_client=MockBenchmarkLLM())
    item = QueryItem(
        query_id="TEST-01",
        category="testing",
        query="Explain token budget optimization",
        expected_evidence=["knapsack optimization", "token budget", "provenance preservation"],
        reference_answer="Synapse uses utility-density knapsack to optimize tokens while preserving provenance.",
    )

    # 1. Perfect answer matching all evidence
    good_answer = "Synapse uses knapsack optimization within the token budget while ensuring provenance preservation."
    good_score = judge.evaluate(item, good_answer, context="test context")
    assert good_score.overall_score == 5.0
    assert good_score.correctness == 5.0

    # 2. Evasive response disclaimer
    evasive_answer = "The provided context does not contain information to answer this question."
    bad_score = judge.evaluate(item, evasive_answer, context="test context")
    assert bad_score.overall_score < 2.0
    assert bad_score.correctness == 1.0


# ---------------------------------------------------------------------------
# 4. Deterministic Execution Reproducibility
# ---------------------------------------------------------------------------

def test_deterministic_reproducibility(tmp_path):
    query = QueryItem(
        query_id="REP-01",
        category="structural",
        query="What modules import models.py?",
        expected_evidence=["backend/db_loader.py", "backend/bridge.py"],
    )

    class DummyRunner(BaseEvaluationRunner):
        def execute_query(self, query_item, budget, llm_client):
            ctx = "Node[backend/db_loader.py]: loader\nNode[backend/bridge.py]: bridge"
            ans = llm_client.invoke(ctx).get("content", "")
            return ctx, ans, RetrievalMetrics(context_tokens_precise=50, budget=budget)

    config = EvaluationConfig(
        run_id="det_run",
        configurations=["dummy"],
        repetitions=2,
        output_dir=str(tmp_path / "det_eval"),
    )

    harness = EvaluationHarness(
        config=config,
        dataset=[query],  # type: ignore
        runners={"dummy": DummyRunner("dummy")},
        llm_client=MockBenchmarkLLM(),
    )

    records = harness.run()
    assert len(records) == 2
    # Both repetitions must be byte-for-byte identical in metrics
    r0, r1 = records[0], records[1]
    assert r0.retrieval.context_tokens_precise == r1.retrieval.context_tokens_precise
    assert r0.quality.overall_score == r1.quality.overall_score
    assert r0.raw_answer == r1.raw_answer


# ---------------------------------------------------------------------------
# 5. Resumability & Checkpoint Recovery
# ---------------------------------------------------------------------------

def test_checkpoint_recovery_and_resumability(tmp_path):
    out_dir = tmp_path / "resume_test"
    out_dir.mkdir(parents=True)
    chk_file = out_dir / "checkpoint_resumable_run.jsonl"

    q1 = QueryItem(query_id="Q-1", category="c1", query="Query 1")
    q2 = QueryItem(query_id="Q-2", category="c2", query="Query 2")

    # Pre-populate checkpoint with Q-1 completed
    prior_record = TrialRecord(
        trial_id="prior_t1",
        run_id="resumable_run",
        repetition_index=0,
        query_id="Q-1",
        query_text="Query 1",
        category="c1",
        configuration_name="mock_runner",
        git_commit="abc1234",
        timestamp="2026-10-15T00:00:00Z",
        retrieval=RetrievalMetrics(context_tokens_precise=100),
        quality=AnswerQualityMetrics(overall_score=4.0),
        status="success",
    )
    with open(chk_file, "w", encoding="utf-8") as f:
        f.write(json.dumps(prior_record.to_dict()) + "\n")

    executed_queries: list[str] = []

    class MockTrackingRunner(BaseEvaluationRunner):
        def execute_query(self, query_item, budget, llm_client):
            executed_queries.append(query_item.query_id)
            return "context", "answer", RetrievalMetrics()

    config = EvaluationConfig(
        run_id="resumable_run",
        configurations=["mock_runner"],
        repetitions=1,
        output_dir=str(out_dir),
        resume=True,
    )

    harness = EvaluationHarness(
        config=config,
        dataset=[q1, q2],  # type: ignore
        runners={"mock_runner": MockTrackingRunner("mock_runner")},
        llm_client=MockBenchmarkLLM(),
    )

    final_records = harness.run()

    # Q-1 was resumed from checkpoint, only Q-2 should have actually executed!
    assert executed_queries == ["Q-2"]
    assert len(final_records) == 2
    assert final_records[0].query_id == "Q-1"
    assert final_records[1].query_id == "Q-2"


# ---------------------------------------------------------------------------
# 6. Graceful Failure Trapping & Explicit Error Logging
# ---------------------------------------------------------------------------

def test_failure_trapping_without_silent_omission(tmp_path):
    q = QueryItem(query_id="FAIL-Q", category="err", query="Trigger failure")

    class CrashingRunner(BaseEvaluationRunner):
        def execute_query(self, query_item, budget, llm_client):
            raise TimeoutError("Simulated LLM network timeout after 30s")

    config = EvaluationConfig(
        run_id="fail_run",
        configurations=["crashing_runner"],
        repetitions=1,
        output_dir=str(tmp_path / "fail_eval"),
    )

    harness = EvaluationHarness(
        config=config,
        dataset=[q],  # type: ignore
        runners={"crashing_runner": CrashingRunner("crashing_runner")},
        llm_client=MockBenchmarkLLM(),
    )

    records = harness.run()
    assert len(records) == 1
    rec = records[0]
    assert rec.status == "failure"
    assert "Simulated LLM network timeout" in (rec.error_message or "")
    assert rec.retries > 0  # Verified retries took place


# ---------------------------------------------------------------------------
# 7. Aggregator Integrity Checks (Duplicates, Missing Rows)
# ---------------------------------------------------------------------------

def test_aggregator_integrity_detection():
    # 1. Clean records
    rec1 = TrialRecord(
        trial_id="t1", run_id="r1", repetition_index=0, query_id="Q1",
        query_text="q", category="cat", configuration_name="cfg", git_commit="c",
        timestamp="2026-10-15T00:00:00Z", retrieval=RetrievalMetrics(context_tokens_precise=100),
        quality=AnswerQualityMetrics(overall_score=4.0, prompt_tokens=100, completion_tokens=50, total_tokens=150),
        status="success",
    )
    # Duplicate of rec1
    rec2 = TrialRecord(
        trial_id="t2", run_id="r1", repetition_index=0, query_id="Q1",
        query_text="q", category="cat", configuration_name="cfg", git_commit="c",
        timestamp="2026-10-15T00:00:01Z", retrieval=RetrievalMetrics(context_tokens_precise=100),
        quality=AnswerQualityMetrics(overall_score=4.0, prompt_tokens=100, completion_tokens=50, total_tokens=150),
        status="success",
    )
    # Missing record
    rec3 = TrialRecord(
        trial_id="t3", run_id="r1", repetition_index=1, query_id="Q2",
        query_text="q", category="cat", configuration_name="cfg", git_commit="c",
        timestamp="2026-10-15T00:00:02Z", retrieval=RetrievalMetrics(),
        quality=AnswerQualityMetrics(),
        status="missing",
        error_message="Runner not found",
    )

    agg = EvaluationAggregator([rec1, rec2, rec3])
    integrity = agg.validate_integrity()

    assert integrity["duplicates_found"] == 1
    assert integrity["missing_count"] == 1
    assert integrity["valid"] is False


# ---------------------------------------------------------------------------
# 8. Separation of Retrieval & Answer Quality Metrics
# ---------------------------------------------------------------------------

def test_separation_of_retrieval_and_answer_quality():
    rec = TrialRecord(
        trial_id="t1", run_id="r1", repetition_index=0, query_id="Q1",
        query_text="q", category="cat", configuration_name="synapse_det", git_commit="c",
        timestamp="2026-10-15T00:00:00Z",
        retrieval=RetrievalMetrics(
            context_tokens_precise=800,
            evidence_precision=0.75,
            evidence_recall=0.80,
            evidence_f1=0.774,
            retrieval_latency_ms=12.5,
        ),
        quality=AnswerQualityMetrics(
            correctness=5.0,
            completeness=4.0,
            relevance=5.0,
            evidence_sufficiency=4.5,
            overall_score=4.62,
            prompt_tokens=850,
            completion_tokens=60,
            total_tokens=910,
            llm_latency_ms=25.0,
        ),
        total_latency_ms=37.5,
        status="success",
    )

    agg = EvaluationAggregator([rec])
    summary = agg.compute_summary()
    cfg = summary["configurations"]["synapse_det"]

    # Assert clean partition
    assert "retrieval" in cfg
    assert "answer_quality" in cfg
    assert cfg["retrieval"]["mean_context_tokens"] == 800.0
    assert cfg["retrieval"]["mean_evidence_f1"] == 0.774
    assert cfg["answer_quality"]["mean_correctness"] == 5.0
    assert cfg["answer_quality"]["mean_overall_score"] == 4.62


# ---------------------------------------------------------------------------
# 9. Verified Multi-Format Exports (JSON, CSV, Markdown)
# ---------------------------------------------------------------------------

def test_export_formats_json_csv_markdown(tmp_path):
    rec = TrialRecord(
        trial_id="t1", run_id="r1", repetition_index=0, query_id="SYN-Q01",
        query_text="Test query", category="structural", configuration_name="synapse_det", git_commit="c1",
        timestamp="2026-10-15T00:00:00Z",
        retrieval=RetrievalMetrics(context_tokens_precise=500),
        quality=AnswerQualityMetrics(overall_score=4.5),
        status="success",
    )

    agg = EvaluationAggregator([rec])
    json_out = tmp_path / "report.json"
    csv_out = tmp_path / "report.csv"
    md_out = tmp_path / "report.md"

    agg.export_json(json_out)
    agg.export_csv(csv_out)
    agg.export_markdown_report(md_out)

    assert json_out.exists()
    assert csv_out.exists()
    assert md_out.exists()

    with open(json_out, "r", encoding="utf-8") as f:
        data = json.load(f)
        assert "summary" in data
        assert len(data["trials"]) == 1

    with open(csv_out, "r", encoding="utf-8") as f:
        lines = f.readlines()
        assert len(lines) == 2  # Header + 1 row
        assert "SYN-Q01" in lines[1]

    with open(md_out, "r", encoding="utf-8") as f:
        md_text = f.read()
        assert "# Synapse Evaluation Framework Benchmark Report" in md_text
        assert "synapse_det" in md_text
