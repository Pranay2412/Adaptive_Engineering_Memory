"""Unit tests for the Graphify-vs-Synapse Token Benchmark Harness."""

import unittest
from pathlib import Path

import networkx as nx

from backend.hybrid_retrieval import HybridRetrievalService
from backend.models import CodeSymbol, DocumentEntity, Memory
from backend.orchestrator import AdaptiveContextOrchestrator
from backend.semantic_matcher import MockEmbeddingProvider
from benchmarks.token_benchmark import (
    ComparisonReport,
    GraphifyWorkflowRunner,
    SynapseWorkflowRunner,
    TokenBenchmarkHarness,
    WorkflowTokenMetrics,
)


class TestTokenBenchmark(unittest.TestCase):
    """Test suite for the Graphify vs Synapse token comparison benchmark harness."""

    def setUp(self):
        # 1. Build a mock in-memory NetworkX graph for Graphify
        self.G = nx.Graph()
        self.G.add_node(
            "AuthService",
            label="AuthService",
            source_file="backend/models.py",
            source_location="L18",
            community=1,
        )
        self.G.add_node(
            "JWTManager",
            label="JWTManager",
            source_file="backend/models.py",
            source_location="L89",
            community=1,
        )
        self.G.add_node(
            "TokenValidator",
            label="TokenValidator",
            source_file="backend/models.py",
            source_location="L150",
            community=1,
        )
        self.G.add_edge("AuthService", "JWTManager", relation="CALLS", confidence=1.0)
        self.G.add_edge("JWTManager", "TokenValidator", relation="CALLS", confidence=1.0)

        # 2. Build mock symbols and entities for Synapse
        self.symbols = [
            CodeSymbol(
                id="s-auth",
                name="AuthService",
                file="services/auth_service.py",
                type="class",
                documentation="Issues RS256 JWT tokens and authenticates user identities.",
            ),
            CodeSymbol(
                id="s-jwt",
                name="JWTManager",
                file="security/jwt_manager.py",
                type="class",
                documentation="Cryptographic key rotation and token signing engine.",
            ),
        ]
        self.entities = [
            DocumentEntity(
                id="d-spec",
                name="Authentication Architecture",
                type="spec",
                description="All tokens must expire within 24 hours and rotate refresh keys.",
            )
        ]
        self.memories = [
            Memory(
                id="m-adr",
                title="Architecture Decision: RS256 Tokens",
                content="Chose asymmetric RS256 over HS256 to avoid sharing secrets.",
                category="architecture_decision",
                scope="repository",
                author_id="dev-alice",
            )
        ]

        # 3. Runners
        self.graphify_runner = GraphifyWorkflowRunner()
        self.graphify_runner.set_in_memory_graph(self.G)

        mock_hybrid = HybridRetrievalService(embedding_provider=MockEmbeddingProvider())
        orchestrator = AdaptiveContextOrchestrator(retrieval_service=mock_hybrid)
        self.synapse_runner = SynapseWorkflowRunner(orchestrator=orchestrator)

    def test_graphify_topology_mode_tokens(self):
        """Verify Graphify topology-only mode produces pure node/edge metadata without code."""
        metrics = self.graphify_runner.run(
            query="how does AuthService work?",
            context_budget=1000,
            mode="topology_only",
        )
        self.assertIn("graphify_topology", metrics.workflow_name)
        self.assertGreater(metrics.retrieval_tokens, 0)
        self.assertGreater(metrics.context_tokens, 0)
        self.assertGreater(metrics.input_tokens, metrics.context_tokens)
        self.assertEqual(metrics.total_tokens, metrics.input_tokens + metrics.output_tokens)
        self.assertFalse(metrics.has_code_content)
        self.assertFalse(metrics.has_documentation)
        self.assertIn("NODE AuthService", metrics.context_text)
        self.assertIn("EDGE AuthService", metrics.context_text)

    def test_graphify_code_resolved_mode(self):
        """Verify Graphify code-resolved mode extracts source content and has higher token usage."""
        metrics = self.graphify_runner.run(
            query="how does AuthService work?",
            context_budget=1000,
            mode="code_resolved",
            repo_root=Path("C:/FYP"),
        )
        self.assertIn("graphify_code_resolved", metrics.workflow_name)
        self.assertGreater(metrics.context_tokens, 0)
        self.assertTrue(metrics.has_code_content)
        self.assertIn("Symbol: AuthService", metrics.context_text)

    def test_synapse_runner_metrics(self):
        """Verify Synapse runner produces multi-modal context with code, docs, and decisions."""
        metrics = self.synapse_runner.run(
            query="how does AuthService work?",
            context_budget=1000,
            symbols=self.symbols,
            entities=self.entities,
            memories=self.memories,
        )
        self.assertEqual(metrics.workflow_name, "synapse_complete")
        self.assertGreater(metrics.context_tokens, 0)
        self.assertTrue(metrics.has_code_content)
        self.assertTrue(metrics.has_documentation)
        self.assertTrue(metrics.has_team_decisions)
        self.assertIn("AuthService", metrics.context_text)
        self.assertIn("RS256", metrics.context_text)

    def test_harness_comparison_report(self):
        """Verify TokenBenchmarkHarness generates a valid scientific comparison report."""
        harness = TokenBenchmarkHarness(
            graphify_runner=self.graphify_runner,
            synapse_runner=self.synapse_runner,
        )
        report = harness.compare(
            query="how does AuthService work?",
            context_budget=1000,
            symbols=self.symbols,
            entities=self.entities,
            memories=self.memories,
            repo_root=Path("C:/FYP"),
        )

        self.assertIsInstance(report, ComparisonReport)
        self.assertEqual(report.query, "how does AuthService work?")
        self.assertGreater(report.graphify_metrics.context_tokens, 0)
        self.assertGreater(report.synapse_metrics.context_tokens, 0)

        # JSON dictionary representation
        data = report.to_dict()
        self.assertIn("graphify", data)
        self.assertIn("synapse", data)
        self.assertIn("context_budget", data)
        self.assertIn("percentage_context_token_reduction", data)


if __name__ == "__main__":
    unittest.main()
