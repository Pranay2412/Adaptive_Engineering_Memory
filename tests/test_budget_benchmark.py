"""Unit tests for TEST CASE 2 Budget Benchmark Harness."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.models import CodeSymbol, DocumentEntity, Memory, OptimizationMode
from benchmarks.budget_benchmark import (
    QUERY_GROUND_TRUTH,
    AnswerQualityJudge,
    AnswerQualityScore,
    BudgetExperimentRunner,
    BudgetTrialMetric,
    compute_budget_aggregates,
    export_budget_csv,
    export_budget_json,
    export_budget_markdown,
)
from benchmarks.token_benchmark import MockBenchmarkLLM


class TestBudgetBenchmark(unittest.TestCase):
    """Test suite for budget benchmark metrics, evaluation, and serialization."""

    def test_trial_metric_calculation_and_budget_utilization(self) -> None:
        """Verify budget utilization calculation and bounded vs unbounded handling."""
        # 1. Bounded budget (500 tokens, 450 used -> 90.0% utilization)
        metric_bounded = BudgetTrialMetric(
            query="How does authentication work with JWT?",
            category="implementation",
            intent="explanation",
            budget=500,
            optimization_mode="deterministic",
            final_context_tokens=450,
            budget_utilization_pct=round((450 / 500) * 100.0, 2),
        )
        self.assertEqual(metric_bounded.budget_utilization_pct, 90.0)
        self.assertEqual(metric_bounded.budget, 500)

        # 2. Unbounded budget (budget is None -> utilization is None / N/A)
        metric_unbounded = BudgetTrialMetric(
            query="How does authentication work with JWT?",
            category="implementation",
            intent="explanation",
            budget=None,
            optimization_mode="deterministic",
            final_context_tokens=850,
            budget_utilization_pct=None,
        )
        self.assertIsNone(metric_unbounded.budget_utilization_pct)
        self.assertIsNone(metric_unbounded.budget)

    def test_mock_judge_deterministic_evaluation(self) -> None:
        """Verify AnswerQualityJudge accurately scores answers with the deterministic rubric."""
        judge = AnswerQualityJudge(llm_client=MockBenchmarkLLM())

        # Test with rich, factual answer containing ground-truth terms
        rich_answer = (
            "Authentication uses JWT signed with asymmetric RS256 algorithm. "
            "Private key rotation is supported. "
            "We added a clock skew leeway parameter to jwt.decode to prevent verification failures in staging."
        )
        score = judge.evaluate("How does authentication work with JWT?", rich_answer)
        self.assertGreaterEqual(score.correctness, 4.0)
        self.assertGreaterEqual(score.completeness, 3.0)
        self.assertGreaterEqual(score.overall_score, 3.5)

        # Test with evasive disclaimer answer
        disclaimer_answer = "The provided context does not contain any information regarding JWT authentication."
        score_disc = judge.evaluate("How does authentication work with JWT?", disclaimer_answer)
        self.assertEqual(score_disc.completeness, 1.5)
        self.assertEqual(score_disc.evidence_sufficiency, 1.0)
        self.assertLess(score_disc.overall_score, 3.0)

    def test_budget_experiment_runner_with_mock_llm(self) -> None:
        """Verify BudgetExperimentRunner executes across bounded and unbounded budgets."""
        mock_llm = MockBenchmarkLLM()
        runner = BudgetExperimentRunner(llm_client=mock_llm)

        symbols = [
            CodeSymbol(
                id="sym-auth",
                name="AuthService",
                file="backend/auth.py",
                type="class",
                documentation="Handles JWT token signing and verification with RS256.",
            )
        ]
        memories = [
            Memory(
                id="mem-1",
                title="Architecture Decision: Sign JWT tokens with asymmetric RS256",
                content="Chose RS256 over HS256 to eliminate shared symmetric secret vulnerabilities.",
                category="architecture_decision",
            )
        ]

        # Bounded trial (budget=500)
        trial_500 = runner.run_trial(
            query="How does authentication work with JWT?",
            category="implementation",
            budget=500,
            mode=OptimizationMode.DETERMINISTIC,
            symbols=symbols,
            memories=memories,
        )
        self.assertLessEqual(trial_500.final_context_tokens, 500)
        self.assertIsNotNone(trial_500.budget_utilization_pct)
        self.assertGreater(trial_500.quality_score.overall_score, 0.0)

        # Unbounded trial (budget=None)
        trial_unbounded = runner.run_trial(
            query="How does authentication work with JWT?",
            category="implementation",
            budget=None,
            mode=OptimizationMode.NONE,
            symbols=symbols,
            memories=memories,
        )
        self.assertIsNone(trial_unbounded.budget_utilization_pct)
        self.assertGreater(trial_unbounded.final_context_tokens, 0)

    def test_budget_aggregates_and_exporters(self) -> None:
        """Verify computation of statistical aggregates and JSON/CSV/Markdown file exports."""
        trials = [
            BudgetTrialMetric(
                query="Query 1",
                category="structural",
                intent="code_navigation",
                budget=500,
                optimization_mode="deterministic",
                final_context_tokens=400,
                actual_provider_prompt_tokens=440,
                output_completion_tokens=60,
                total_tokens=500,
                budget_utilization_pct=80.0,
                total_latency_ms=120.0,
                quality_score=AnswerQualityScore(correctness=4, completeness=4, relevance=4, evidence_sufficiency=4, overall_score=4.0),
            ),
            BudgetTrialMetric(
                query="Query 2",
                category="implementation",
                intent="explanation",
                budget=None,
                optimization_mode="deterministic",
                final_context_tokens=900,
                actual_provider_prompt_tokens=950,
                output_completion_tokens=150,
                total_tokens=1100,
                budget_utilization_pct=None,
                total_latency_ms=250.0,
                quality_score=AnswerQualityScore(correctness=5, completeness=5, relevance=5, evidence_sufficiency=5, overall_score=5.0),
            ),
        ]

        aggregates = compute_budget_aggregates(trials)
        self.assertEqual(aggregates["total_trials"], 2)
        self.assertEqual(len(aggregates["by_budget"]), 2)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_file = tmp / "results.json"
            csv_file = tmp / "results.csv"
            md_file = tmp / "report.md"

            report = {
                "timestamp": "2026-10-08T00:00:00Z",
                "metadata": {"model": "mock-llm"},
                "aggregates": aggregates,
                "trials": [t.to_dict() for t in trials],
            }

            export_budget_json(report, json_file)
            self.assertTrue(json_file.exists())
            loaded = json.loads(json_file.read_text(encoding="utf-8"))
            self.assertEqual(loaded["aggregates"]["total_trials"], 2)

            export_budget_csv(trials, csv_file)
            self.assertTrue(csv_file.exists())
            csv_text = csv_file.read_text(encoding="utf-8")
            self.assertIn("Query 1", csv_text)

            export_budget_markdown(report, md_file)
            self.assertTrue(md_file.exists())
            md_text = md_file.read_text(encoding="utf-8")
            self.assertIn("TEST CASE 2: ACO Token Budget Evaluation Report", md_text)


if __name__ == "__main__":
    unittest.main()
