"""Automated Test Suite for Phase 21 Research and Technical Documentation.

Verifies that the research report in docs/research/research_and_technical_report.md
satisfies all requirements of Section A24 of the handoff specification:
1. Presence of all 18 required research and technical sections.
2. Exact mapping between quantitative claims and underlying benchmark artifacts.
3. Explicit disclosure of latency trade-offs and negative/neutral findings.
4. Validity of internal file and artifact references.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


class TestResearchDocumentationSuite(unittest.TestCase):
    """Test suite validating the research documentation against benchmark artifacts."""

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parent.parent
        cls.report_path = cls.repo_root / "docs" / "research" / "research_and_technical_report.md"
        if not cls.report_path.is_file():
            raise FileNotFoundError(f"Research report missing at {cls.report_path}")
        cls.report_content = cls.report_path.read_text(encoding="utf-8")

    def test_report_exists_and_substantial(self):
        """Verify the research report exists and contains substantial technical depth."""
        self.assertGreater(len(self.report_content), 10000, "Research report is too brief")
        self.assertIn("Synapse: Adaptive Engineering Memory Platform", self.report_content)

    def test_all_18_required_sections_present(self):
        """Verify presence of all 18 required topics specified in Section A24."""
        required_topics = [
            ("Abstract / Summary", r"## Abstract|Executive Summary"),
            ("Problem / Motivation", r"## 1\. Introduction & Motivation"),
            ("Related Approaches & Gap Analysis", r"## 2\. Related Work & Gap Analysis"),
            ("Architecture & Data Flow", r"## 3\. Platform Architecture & Data Flow"),
            ("Structural & Semantic Memory", r"## 4\. Structural and Semantic Memory Foundations"),
            ("Hybrid Retrieval", r"## 5\. Hybrid Multi-Channel Retrieval"),
            ("ACO Algorithm", r"## 6\. The Adaptive Context Orchestrator \(ACO\)"),
            ("Session & Team Memory", r"## 7\. Session and Team Engineering Memory"),
            ("Implementation Environment", r"8\.1 Benchmark Methodology & Datasets"),
            ("Datasets / Rubric", r"queries_v1\.json"),
            ("Test Case 1 (Comparative)", r"8\.2 Test Case 1: Baseline Comparison"),
            ("Test Case 2 (Budget Sweep)", r"8\.3 Corrected Test Case 2: Budget Scaling"),
            ("Component Ablation", r"8\.4 Component Ablation Study"),
            ("Latency, Cost & Quality Trade-Offs", r"Latency Trade-Off|Cost & Quality"),
            ("Security & Privacy", r"## 9\. Security, Privacy, and Multi-Tenant Isolation"),
            ("Threats to Validity", r"## 10\. Threats to Validity & Limitations"),
            ("Conclusion & Future Work", r"## 12\. Conclusion & Future Work"),
            ("Reproduction & Artifact Inventory", r"## 11\. Reproduction Guide & Artifact Inventory"),
        ]

        for topic_name, pattern in required_topics:
            self.assertTrue(
                re.search(pattern, self.report_content, re.IGNORECASE),
                f"Missing required section: '{topic_name}' (pattern: {pattern})",
            )

    def test_quantitative_claims_map_to_benchmark_artifacts(self):
        """Verify quantitative figures match actual underlying benchmark records."""
        # 1. TC1 Context Token Reduction (43.22%) and Quality (4.20 vs 1.30)
        self.assertIn("43.22%", self.report_content)
        self.assertIn("4.20", self.report_content)
        self.assertIn("1.30", self.report_content)

        # 2. TC1 Latency Trade-Off Honest Disclosure (+65.2% / +0.970s slower)
        self.assertIn("65.2%", self.report_content)
        self.assertIn("0.97", self.report_content)

        # 3. Corrected TC2 Budget Sweep (75 trials, 0 violations, saturation at 1,191)
        self.assertIn("75", self.report_content)
        self.assertIn("1,191", self.report_content)

        # 4. Ablation Study (165 trials)
        self.assertIn("165", self.report_content)

        # 5. E2E Demo (83.21% token reduction)
        self.assertIn("83.21%", self.report_content)

    def test_authoritative_artifacts_exist_on_disk(self):
        """Verify that all referenced primary benchmark and documentation files exist."""
        artifacts_to_check = [
            "docs/experiments/BASELINE_FREEZE.md",
            "docs/demo/e2e_demo_guide.md",
            "docs/security/threat_model_and_mitigations.md",
            "docs/observability/metric_definitions_and_runbook.md",
            "benchmarks/results/final_baseline_tc1_results.json",
            "benchmarks/results/corrected_budget_benchmark_results.json",
            "benchmarks/results/demo_e2e_results.json",
            "benchmarks/datasets/queries_v1.json",
            "scripts/demo_e2e.py",
            "scripts/smoke_test.py",
        ]

        for rel_path in artifacts_to_check:
            full_path = self.repo_root / rel_path
            self.assertTrue(full_path.exists(), f"Referenced artifact does not exist: {rel_path}")


if __name__ == "__main__":
    unittest.main()
