"""Automated Integration Test for Phase 20 End-to-End Demo.

Verifies that the reproducible demo runs from clean setup to completion,
exercising all 8 functional and architectural stages with zero regressions.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.demo_e2e import run_e2e_demo


class TestEndToEndDemoSuite(unittest.TestCase):
    """Test suite executing the full 8-stage end-to-end demo harness."""

    def test_run_e2e_demo_complete_success(self):
        """Verify that the end-to-end demo completes with PASS status across all 8 stages."""
        repo_root = Path(__file__).resolve().parent.parent
        fixtures_dir = repo_root / "fixtures" / "demo_repo"
        self.assertTrue(fixtures_dir.is_dir(), f"Fixtures directory missing at {fixtures_dir}")

        with tempfile.TemporaryDirectory(prefix="test_demo_e2e_") as td:
            output_json = Path(td) / "demo_results.json"
            report = run_e2e_demo(
                fixtures_dir=fixtures_dir,
                output_json_path=output_json,
                verbose=False,
            )

            # 1. Overall Status Verification
            self.assertEqual(report.get("overall_status"), "PASSED")
            self.assertTrue(report.get("synthetic_data_labeled"))
            self.assertTrue(output_json.exists(), "Results JSON was not written")

            stages = report.get("stages", {})
            expected_stages = [
                "ingestion",
                "structural_query",
                "debugging_query",
                "persist_decision",
                "cross_client_continuity",
                "token_accounting",
                "baseline_comparison",
                "security_boundaries",
            ]
            for stage_name in expected_stages:
                self.assertIn(stage_name, stages, f"Missing stage: {stage_name}")
                self.assertEqual(
                    stages[stage_name].get("status"),
                    "PASS",
                    f"Stage {stage_name} did not pass: {stages[stage_name]}",
                )

            # 2. Ingestion Stage Assertions
            ingest_data = stages["ingestion"]
            self.assertGreaterEqual(ingest_data["files_count"], 5)
            self.assertGreaterEqual(ingest_data["symbols_count"], 15)
            self.assertGreaterEqual(ingest_data["entities_count"], 5)
            self.assertGreaterEqual(ingest_data["edges_count"], 20)

            # 3. Structural Query Assertions
            struct_data = stages["structural_query"]
            self.assertEqual(struct_data["intent"], "architecture")
            self.assertGreater(struct_data["confidence"], 0.6)
            self.assertGreater(struct_data["retrieved_count"], 0)

            # 4. Debugging Query Assertions
            debug_data = stages["debugging_query"]
            self.assertEqual(debug_data["intent"], "debugging")
            self.assertGreater(debug_data["confidence"], 0.6)
            self.assertGreater(debug_data["retrieved_count"], 0)

            # 5. ADR Persistence & Cross-Client Continuity Assertions
            persist_data = stages["persist_decision"]
            self.assertTrue(persist_data["decision_id"].startswith("dec_"))

            cont_data = stages["cross_client_continuity"]
            self.assertTrue(cont_data["adr_found"], "ADR was not retrieved across sessions")
            self.assertNotEqual(cont_data["session_client_1"], cont_data["session_client_2"])

            # 6. Section 7 Token Accounting Invariant Assertions
            tok_data = stages["token_accounting"]
            budget = tok_data["context_budget"]
            ctx_tokens = tok_data["knowledge_context_tokens"]
            total_input = tok_data["total_input_tokens"]

            # Knowledge-context tokens must respect configured budget
            self.assertLessEqual(ctx_tokens, budget, "Knowledge context tokens exceeded budget!")
            # Total input tokens must be greater than context tokens (includes instructions + query)
            self.assertGreater(total_input, ctx_tokens)
            self.assertGreater(tok_data["budget_utilization_pct"], 0.0)

            # 7. Baseline vs. Synapse Comparison Assertions
            comp_data = stages["baseline_comparison"]
            self.assertGreater(comp_data["baseline_context_tokens"], comp_data["synapse_context_tokens"])
            self.assertGreater(comp_data["token_reduction_pct"], 50.0)

            # 8. Security Boundaries Assertions
            sec_data = stages["security_boundaries"]
            self.assertTrue(sec_data["unauthorized_repo_blocked"])
            self.assertTrue(sec_data["directory_traversal_blocked"])
            self.assertTrue(sec_data["prompt_injection_neutralized"])

            # 9. Verify on-disk JSON artifact format
            disk_data = json.loads(output_json.read_text(encoding="utf-8"))
            self.assertEqual(disk_data["overall_status"], "PASSED")
            self.assertEqual(disk_data["git_commit"], report["git_commit"])


if __name__ == "__main__":
    unittest.main()
