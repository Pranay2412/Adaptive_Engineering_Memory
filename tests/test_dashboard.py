"""Comprehensive Unit and Integration Tests for Phase 16 — Dashboard.

Verifies:
1. Static HTML dashboard serving at / and /dashboard
2. Real API telemetry endpoints (/api/v1/dashboard/overview, /repositories, /jobs, /sessions, /memories)
3. Interactive query console primary workflow (/api/v1/dashboard/query)
4. Token economy governance and accurate budget utilization definitions (Section 7)
5. Multi-tier cache integration and hit indicators in dashboard query responses
6. Authentication, RBAC, and payload validation enforcement
"""

from __future__ import annotations

import unittest
from fastapi.testclient import TestClient

from backend.api import create_app
from backend.cache import default_cache_manager
from backend.config import EnvironmentMode, SynapseConfig
from backend.security import Role, TokenService


class TestDashboardSuite(unittest.TestCase):
    """Test suite for Synapse Engineering Memory Dashboard."""

    def setUp(self):
        self.config = SynapseConfig(
            environment=EnvironmentMode.DEVELOPMENT.value,
            secret_key="test-dashboard-secret-must-be-at-least-32-chars-long",
            request_timeout_seconds=60.0,
        )
        self.app = create_app(self.config)
        self.client = TestClient(self.app)
        self.token_service = TokenService(secret_key=self.config.secret_key)
        default_cache_manager.clear_all()

    def _auth_header(self, roles: list[str] | None = None) -> dict[str, str]:
        from backend.session_protocol import AuthenticatedPrincipal
        principal = AuthenticatedPrincipal(
            principal_id="dash-admin",
            tenant_id="tenant-primary",
            roles=roles or [Role.ADMIN.value],
            allowed_repositories=["*"],
        )
        token = self.token_service.create_token(principal)
        return {"Authorization": f"Bearer {token}"}

    def test_serve_dashboard_html_root_and_dashboard(self):
        """Verify dashboard HTML is served cleanly at root / and /dashboard."""
        for path in ("/", "/dashboard"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200)
            self.assertIn("text/html", resp.headers["content-type"])
            self.assertIn("Synapse", resp.text)
            self.assertIn("Adaptive Engineering Memory Platform", resp.text)
            self.assertIn("Interactive Context Orchestration Console", resp.text)

    def test_dashboard_overview_telemetry(self):
        """Verify overview endpoint returns real platform, memory, and cache telemetry."""
        headers = self._auth_header()
        resp = self.client.get("/api/v1/dashboard/overview", headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        # System telemetry
        self.assertIn("system", data)
        self.assertEqual(data["system"]["status"], "ready")
        self.assertIn("protocol_version", data["system"])

        # Memory store counts
        self.assertIn("memory_store", data)
        self.assertGreaterEqual(data["memory_store"]["total_symbols"], 0)
        self.assertGreaterEqual(data["memory_store"]["total_document_concepts"], 0)
        self.assertGreaterEqual(data["memory_store"]["total_team_memories"], 0)

        # Cache telemetry
        self.assertIn("cache_telemetry", data)
        self.assertIn("retrieval_cache", data["cache_telemetry"])
        self.assertIn("embedding_cache", data["cache_telemetry"])

    def test_dashboard_repositories_and_jobs(self):
        """Verify repository listing and historical checkpoint jobs."""
        headers = self._auth_header()

        # Repositories
        resp_repos = self.client.get("/api/v1/dashboard/repositories", headers=headers)
        self.assertEqual(resp_repos.status_code, 200)
        repos = resp_repos.json()
        self.assertIsInstance(repos, list)
        self.assertGreater(len(repos), 0)
        repo = repos[0]
        self.assertEqual(repo["repository_id"], "synapse-core")
        self.assertEqual(repo["status"], "indexed")
        self.assertIn("symbol_count", repo)

        # Jobs
        resp_jobs = self.client.get("/api/v1/dashboard/jobs", headers=headers)
        self.assertEqual(resp_jobs.status_code, 200)
        jobs = resp_jobs.json()
        self.assertIsInstance(jobs, list)
        self.assertGreater(len(jobs), 0)
        self.assertIn("checkpoint_id", jobs[0])
        self.assertIn("duration_ms", jobs[0])

    def test_dashboard_sessions_and_memories(self):
        """Verify active session records and stored ADR memories."""
        headers = self._auth_header()

        # Sessions
        resp_sessions = self.client.get("/api/v1/dashboard/sessions", headers=headers)
        self.assertEqual(resp_sessions.status_code, 200)
        self.assertIsInstance(resp_sessions.json(), list)

        # Memories
        resp_mem = self.client.get("/api/v1/dashboard/memories", headers=headers)
        self.assertEqual(resp_mem.status_code, 200)
        self.assertIsInstance(resp_mem.json(), list)

    def test_interactive_query_primary_workflow(self):
        """Verify the primary interactive query workflow against real API."""
        headers = self._auth_header()
        query_text = "How does authentication work with JWT?"
        payload = {
            "query": query_text,
            "repository_id": "synapse-core",
            "context_budget": 1500,
            "optimization_mode": "deterministic",
        }

        # First run: Cold compute
        resp1 = self.client.post("/api/v1/dashboard/query", json=payload, headers=headers)
        self.assertEqual(resp1.status_code, 200)
        data1 = resp1.json()

        # 1. Query & Intent verification
        self.assertEqual(data1["query"], query_text)
        self.assertIn("intent", data1)
        self.assertIn("primary_intent", data1["intent"])
        self.assertGreater(data1["intent"]["confidence"], 0.0)

        # 2. Token economy governance (Section 7)
        tokens = data1["tokens"]
        self.assertIn("knowledge_context_tokens", tokens)
        self.assertIn("query_tokens", tokens)
        self.assertIn("instruction_tokens", tokens)
        self.assertIn("total_input_tokens", tokens)
        self.assertEqual(tokens["configured_budget"], 1500)
        self.assertTrue(tokens["is_exact_count"])
        self.assertGreaterEqual(tokens["budget_utilization_pct"], 0.0)
        self.assertLessEqual(tokens["budget_utilization_pct"], 100.0)

        # 3. Evidence items and provenance
        evidence = data1["evidence_items"]
        self.assertIsInstance(evidence, list)
        self.assertGreater(len(evidence), 0)
        item0 = evidence[0]
        self.assertIn("entity", item0)
        self.assertIn("type", item0)
        self.assertIn("score", item0)
        self.assertIn("matched_channels", item0)

        # 4. Assembled prompt context preview
        self.assertIn("assembled_context_preview", data1)
        self.assertIn("### [", data1["assembled_context_preview"])

        # 5. Timing & Cache check
        timing1 = data1["timing"]
        self.assertIn("latency_ms", timing1)

        # Second run: Warm cache check
        resp2 = self.client.post("/api/v1/dashboard/query", json=payload, headers=headers)
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()
        self.assertTrue(data2["timing"]["from_cache"])
        self.assertEqual(len(data1["evidence_items"]), len(data2["evidence_items"]))

    def test_query_validation_error_handling(self):
        """Verify malformed query payloads are rejected with clear validation errors."""
        headers = self._auth_header()

        # Empty query
        resp_empty = self.client.post(
            "/api/v1/dashboard/query",
            json={"query": "", "context_budget": 1500},
            headers=headers,
        )
        self.assertEqual(resp_empty.status_code, 422)

        # Budget below minimum allowed
        resp_budget = self.client.post(
            "/api/v1/dashboard/query",
            json={"query": "test", "context_budget": 10},
            headers=headers,
        )
        self.assertEqual(resp_budget.status_code, 422)

    def test_unauthorized_dashboard_access(self):
        """Verify production mode rejects requests lacking authentication or permissions."""
        prod_config = SynapseConfig(
            environment=EnvironmentMode.PRODUCTION.value,
            secret_key="test-prod-secret-must-be-at-least-32-chars-long",
            require_auth=True,
            cors_allowed_origins=["https://dashboard.internal"],
        )
        prod_app = create_app(prod_config)
        client = TestClient(prod_app)

        # Missing token in production mode
        resp = client.post(
            "/api/v1/dashboard/query",
            json={"query": "test query", "context_budget": 1500},
        )
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
