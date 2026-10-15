"""Comprehensive Unit and Integration Tests for Phase 18 — Observability.

Verifies:
1. Structured JSON logging with request/session contextvar propagation and secret redaction.
2. Low-cardinality query fingerprinting preventing metric label explosion and prompt leaks.
3. Prometheus / OpenMetrics registry counters, gauges, and percentile histograms.
4. Section 7 token governance metrics and context budget utilization ratios.
5. Granular ACO stage latency breakdown across pipeline components.
6. Public /metrics scraping endpoint and authenticated /api/v1/telemetry diagnostics endpoint.
7. Separation of process liveness from dependency readiness gauges.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.api import create_app, reset_protocol_engine
from backend.config import EnvironmentMode, SynapseConfig
from backend.security import Role, TokenService
from backend.session_protocol import AuthenticatedPrincipal
from backend.telemetry import (
    MetricsRegistry,
    StructuredJsonFormatter,
    default_telemetry_registry,
    get_current_request_id,
    get_current_session_id,
    hash_query_fingerprint,
    reset_request_context,
    set_request_context,
)


class TestObservabilitySuite(unittest.TestCase):
    """Test suite for Phase 18 Observability and Structured Telemetry."""

    def setUp(self):
        default_telemetry_registry.reset()

    def test_structured_json_logging_and_contextvars(self):
        """Verify StructuredJsonFormatter formats JSON, binds contextvars, and redacts secrets."""
        formatter = StructuredJsonFormatter(service_name="synapse-test")

        tokens = set_request_context(
            request_id="req_obs_12345",
            session_id="sess_obs_67890",
            tenant_id="tenant_alpha",
        )
        try:
            self.assertEqual(get_current_request_id(), "req_obs_12345")
            self.assertEqual(get_current_session_id(), "sess_obs_67890")

            # Create a log record containing sensitive credentials
            secret_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.doNotLeakThisSignature12345"
            record = logging.LogRecord(
                name="test_logger",
                level=logging.INFO,
                pathname="test.py",
                lineno=10,
                msg=f"User authenticated with token: {secret_jwt}",
                args=(),
                exc_info=None,
            )
            record.component = "auth"
            record.duration_ms = 45.2
            record.status = "success"

            formatted_json = formatter.format(record)
            parsed = json.loads(formatted_json)

            self.assertEqual(parsed["service"], "synapse-test")
            self.assertEqual(parsed["level"], "INFO")
            self.assertEqual(parsed["request_id"], "req_obs_12345")
            self.assertEqual(parsed["session_id"], "sess_obs_67890")
            self.assertEqual(parsed["tenant_id"], "tenant_alpha")
            self.assertEqual(parsed["component"], "auth")
            self.assertEqual(parsed["duration_ms"], 45.2)

            # Verify secret redaction
            self.assertNotIn(secret_jwt, parsed["message"])
            self.assertIn("[REDACTED_JWT_TOKEN]", parsed["message"])
        finally:
            reset_request_context(tokens)

    def test_query_fingerprinting_low_cardinality(self):
        """Verify query fingerprinting produces fixed, low-cardinality tokens without leaking text."""
        raw_query = "How do we store JWT authentication tokens safely in SQLite?"
        fp1 = hash_query_fingerprint(raw_query)
        fp2 = hash_query_fingerprint("  how do we store jwt authentication tokens safely in sqlite?  ")

        # Identical queries with varied casing/whitespace must yield identical fingerprints
        self.assertEqual(fp1, fp2)
        self.assertTrue(fp1.startswith("q_"))
        self.assertEqual(len(fp1), 14)

        # Raw query text must never appear in the fingerprint
        self.assertNotIn("jwt", fp1)
        self.assertNotIn("authentication", fp1)

    def test_metrics_registry_counters_and_gauges(self):
        """Verify thread-safe counters and gauges with Prometheus export."""
        reg = MetricsRegistry()

        # Counters
        reg.inc_counter('synapse_http_requests_total{method="GET",endpoint="/health",status="200"}', 5)
        reg.inc_counter('synapse_http_requests_total{method="GET",endpoint="/health",status="200"}', 2)

        # Gauges
        reg.set_gauge("synapse_active_sessions", 12)
        reg.set_dependency_health("neo4j", False)

        openmetrics = reg.export_openmetrics()
        self.assertIn('synapse_http_requests_total{method="GET",endpoint="/health",status="200"} 7.0', openmetrics)
        self.assertIn("synapse_active_sessions 12.0", openmetrics)
        self.assertIn('synapse_dependency_health{dependency="neo4j"} 0.0', openmetrics)

        json_stats = reg.export_json()
        self.assertEqual(json_stats["counters"]['synapse_http_requests_total{method="GET",endpoint="/health",status="200"}'], 7.0)
        self.assertEqual(json_stats["gauges"]["synapse_active_sessions"], 12.0)
        self.assertEqual(json_stats["gauges"]['synapse_dependency_health{dependency="neo4j"}'], 0.0)

    def test_token_and_budget_governance_metrics(self):
        """Verify Section 7 token usage and budget utilization ratio tracking."""
        reg = MetricsRegistry()

        # Knowledge context: 750 tokens, Query: 50 tokens, Instruction: 85 tokens, Budget: 1500 tokens
        reg.record_token_metrics(
            knowledge_context_tokens=750,
            query_tokens=50,
            instruction_tokens=85,
            budget=1500,
            repository_id="synapse-core",
        )

        openmetrics = reg.export_openmetrics()
        self.assertIn('synapse_tokens_total{repo="synapse-core",type="knowledge_context"} 750.0', openmetrics)
        self.assertIn('synapse_tokens_total{repo="synapse-core",type="query"} 50.0', openmetrics)
        self.assertIn('synapse_tokens_total{repo="synapse-core",type="instructions"} 85.0', openmetrics)
        self.assertIn('synapse_tokens_total{repo="synapse-core",type="total_input"} 885.0', openmetrics)

        json_data = reg.export_json()
        hist_key = 'synapse_context_budget_utilization_ratio{repo="synapse-core"}'
        self.assertIn(hist_key, json_data["histograms"])
        self.assertEqual(json_data["histograms"][hist_key]["p50"], 0.5)

    def test_aco_stage_latencies_breakdown(self):
        """Verify granular stage latencies tracking across internal pipeline phases."""
        reg = MetricsRegistry()

        reg.record_stage_latency("intent_classification", 3.2, "aco")
        reg.record_stage_latency("candidate_retrieval", 12.5, "aco")
        reg.record_stage_latency("semantic_ranking", 4.8, "aco")
        reg.record_stage_latency("knapsack_packing", 1.1, "aco")

        json_data = reg.export_json()
        hists = json_data["histograms"]

        self.assertIn('synapse_stage_latency_ms{component="aco",stage="intent_classification"}', hists)
        self.assertIn('synapse_stage_latency_ms{component="aco",stage="candidate_retrieval"}', hists)
        self.assertIn('synapse_stage_latency_ms{component="aco",stage="semantic_ranking"}', hists)
        self.assertIn('synapse_stage_latency_ms{component="aco",stage="knapsack_packing"}', hists)

        self.assertEqual(hists['synapse_stage_latency_ms{component="aco",stage="intent_classification"}']["p50"], 3.2)
        self.assertEqual(hists['synapse_stage_latency_ms{component="aco",stage="candidate_retrieval"}']["p50"], 12.5)

    def test_prometheus_endpoint_and_json_telemetry_api(self):
        """Verify /metrics returns standard OpenMetrics and /api/v1/telemetry returns JSON stats."""
        with patch.dict("os.environ", {"EMBEDDING_PROVIDER": "mock"}):
            reset_protocol_engine()
            try:
                config = SynapseConfig(
                    environment=EnvironmentMode.DEVELOPMENT.value,
                    secret_key="test-observability-secret-must-be-32-chars-long",
                    require_auth=True,
                )
                app = create_app(config)
                with TestClient(app) as client:
                    token_svc = TokenService(secret_key=config.secret_key)
                    principal = AuthenticatedPrincipal(
                        principal_id="obs-tester",
                        tenant_id="tenant-primary",
                        roles=[Role.ADMIN.value],
                        allowed_repositories=["*"],
                    )
                    token = token_svc.create_token(principal)
                    headers = {"Authorization": f"Bearer {token}"}

                    # 1. Hit health probe to generate HTTP metric
                    r_health = client.get("/health")
                    self.assertEqual(r_health.status_code, 200)

                    # 2. Hit Prometheus /metrics scraper endpoint
                    r_metrics = client.get("/metrics")
                    self.assertEqual(r_metrics.status_code, 200)
                    self.assertIn("text/plain", r_metrics.headers["content-type"])
                    self.assertIn("synapse_http_requests_total", r_metrics.text)
                    self.assertIn('endpoint="/health"', r_metrics.text)

                    # 3. Hit authenticated JSON telemetry diagnostics endpoint
                    r_telemetry = client.get("/api/v1/telemetry", headers=headers)
                    self.assertEqual(r_telemetry.status_code, 200)
                    self.assertIn("application/json", r_telemetry.headers["content-type"])
                    data = r_telemetry.json()
                    self.assertIn("counters", data)
                    self.assertIn("gauges", data)
                    self.assertIn("histograms", data)
                    self.assertIn("started_at", data)
            finally:
                reset_protocol_engine()

    def test_readiness_probe_dependency_health_isolation(self):
        """Verify /ready records dependency health gauges without conflating process liveness."""
        with patch.dict("os.environ", {"EMBEDDING_PROVIDER": "mock"}):
            reset_protocol_engine()
            try:
                config = SynapseConfig(
                    environment=EnvironmentMode.DEVELOPMENT.value,
                    secret_key="test-observability-secret-must-be-32-chars-long",
                )
                app = create_app(config)
                with TestClient(app) as client:
                    r_ready = client.get("/ready")
                    self.assertEqual(r_ready.status_code, 200)

                    # Verify dependency gauges were recorded
                    gauges = default_telemetry_registry.export_json()["gauges"]
                    self.assertEqual(gauges['synapse_dependency_health{dependency="sqlite"}'], 1.0)
                    self.assertEqual(gauges['synapse_dependency_health{dependency="orchestrator"}'], 1.0)
            finally:
                reset_protocol_engine()


if __name__ == "__main__":
    unittest.main()

