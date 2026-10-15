# Synapse Observability & Telemetry Runbook (Phase 18)

## 1. Overview & Architecture

Synapse provides an enterprise observability subsystem designed according to Phase 18 specifications. The telemetry architecture ensures complete visibility across the 7-stage Adaptive Context Orchestrator (ACO) pipeline, the multi-tier caching hierarchy, repository ingestion, the REST API, and the Model Context Protocol (MCP) server.

All telemetry adheres to strict privacy and data-safety principles:
- **Zero Raw Prompt / Secret Logging**: Raw query strings, prompt templates, and proprietary code bodies are never stored as metric labels or logged to plaintext streams.
- **Automated Secret Redaction**: The `SecretRedactor` engine filters log records before serialization, masking JWT tokens, API keys (OpenAI, Anthropic, GitHub, AWS), and passwords.
- **Low-Cardinality Fingerprinting**: Queries are fingerprinted into fixed-length SHA-256 tokens (`q_<hash>`), preventing Prometheus label explosion while preserving correlation.
- **Context-Local Propagation**: Distributed trace and correlation contexts (`request_id`, `session_id`, `tenant_id`) propagate via thread-isolated `contextvars`.

---

## 2. Metric Taxonomy & OpenMetrics Definitions

The metrics registry exposes Prometheus / OpenMetrics format via `GET /metrics` and structured JSON diagnostics via `GET /api/v1/telemetry`.

### 2.1 HTTP & API Gateway Metrics
| Metric Name | Type | Description & Labels |
|---|---|---|
| `synapse_http_requests_total` | Counter | Total HTTP requests received. Labels: `method`, `endpoint`, `status`. Sanitized path variables prevent label explosion (e.g., `/sessions/{id}`). |
| `synapse_http_request_duration_seconds` | Histogram / Summary | Request latency distribution across endpoints. Quantiles: p50, p90, p99, sum, count. |
| `synapse_active_sessions` | Gauge | Current count of active, non-expired sessions maintained in memory. |

### 2.2 ACO Pipeline Stage Latencies
Each query processed by the Adaptive Context Orchestrator is timed at microsecond resolution across its 5 internal pipeline phases:
| Metric Name | Type | Description & Labels |
|---|---|---|
| `synapse_stage_latency_ms{stage="intent_classification",component="aco"}` | Summary | Time spent in rule-based query intent classification. |
| `synapse_stage_latency_ms{stage="candidate_retrieval",component="aco"}` | Summary | Latency for hybrid retrieval (lexical, semantic dense vector, graph traversal). |
| `synapse_stage_latency_ms{stage="semantic_ranking",component="aco"}` | Summary | Time spent computing intent-affinity ranking and reciprocal rank fusion. |
| `synapse_stage_latency_ms{stage="token_optimization",component="aco"}` | Summary | Token measurement and context tier assignment latency. |
| `synapse_stage_latency_ms{stage="knapsack_packing",component="aco"}` | Summary | Deterministic 0/1 knapsack budget packing execution time. |

These timings are also returned in the API response metadata under `retrieval_metadata.stage_latencies_ms` for instant per-query root cause diagnosis.

### 2.3 Section 7 Token Governance & Budget Utilization
In strict adherence to Section 7 and Appendix B benchmark token definitions:
| Metric Name | Type | Description & Labels |
|---|---|---|
| `synapse_tokens_total{type="knowledge_context",repo="..."}` | Counter | Cumulative tokens in retrieved engineering memory context. |
| `synapse_tokens_total{type="query",repo="..."}` | Counter | Cumulative tokens in incoming user queries. |
| `synapse_tokens_total{type="instructions",repo="..."}` | Counter | Cumulative tokens in developer/system instructions. |
| `synapse_tokens_total{type="total_input",repo="..."}` | Counter | Cumulative total prompt input tokens delivered to LLMs. |
| `synapse_context_budget_utilization_ratio{repo="..."}` | Summary | Ratio of retrieved context tokens to declared context budget (`context / budget`). Quantiles: p50, p90, p99. |

### 2.4 Multi-Tier Cache Operations
| Metric Name | Type | Description & Labels |
|---|---|---|
| `synapse_cache_operations_total` | Counter | Cache interactions across tiers. Labels: `cache` (`l1_lru`, `l2_sqlite`, `embedding`, `graph_ast`), `result` (`hit`, `miss`, `invalidate`). |

### 2.5 Ingestion & Background Jobs
| Metric Name | Type | Description & Labels |
|---|---|---|
| `synapse_ingestion_jobs_total` | Counter | Repository indexing jobs executed. Labels: `repo`, `status` (`success`, `failed`). |
| `synapse_ingestion_duration_seconds` | Summary | Duration of AST parsing, graph extraction, and vector index generation. |

### 2.6 Model Context Protocol (MCP) Metrics
| Metric Name | Type | Description & Labels |
|---|---|---|
| `synapse_mcp_requests_total` | Counter | Tool invocations received over MCP stdio/HTTP. Labels: `tool`, `status`. |
| `synapse_mcp_request_duration_seconds` | Summary | Latency of individual MCP tool operations (e.g. `query_memory`, `store_decision`). |

### 2.7 Subsystem & Dependency Health Gauges
Dependency health is strictly decoupled from process liveness:
| Metric Name | Type | Values | Description |
|---|---|---|---|
| `synapse_dependency_health{dependency="sqlite"}` | Gauge | `1.0` / `0.0` | SQLite database connectivity and schema integrity. |
| `synapse_dependency_health{dependency="neo4j"}` | Gauge | `1.0` / `0.0` | Neo4j property graph database reachability. |
| `synapse_dependency_health{dependency="embeddings"}` | Gauge | `1.0` / `0.0` | Embedding model inference service readiness. |
| `synapse_dependency_health{dependency="llm"}` | Gauge | `1.0` / `0.0` | Upstream LLM provider API readiness. |
| `synapse_dependency_health{dependency="orchestrator"}` | Gauge | `1.0` / `0.0` | Adaptive Context Orchestrator initialization state. |

---

## 3. Telemetry Ingestion Endpoints

### 3.1 Prometheus Scraping (`GET /metrics`)
- **Path**: `/metrics`
- **Authentication**: None (designed for internal cluster scraping by Prometheus / VictoriaMetrics / Grafana Agent).
- **Content-Type**: `text/plain; version=0.0.4; charset=utf-8`
- **Example Response**:
```prometheus
# HELP synapse_http_requests_total Total HTTP requests received
# TYPE synapse_http_requests_total counter
synapse_http_requests_total{endpoint="/health",method="GET",status="200"} 42.0

# HELP synapse_dependency_health Health status of subsystems (1=healthy, 0=unhealthy)
# TYPE synapse_dependency_health gauge
synapse_dependency_health{dependency="sqlite"} 1.0
synapse_dependency_health{dependency="orchestrator"} 1.0

# HELP synapse_latency_summary Latency summary and percentiles
# TYPE synapse_latency_summary summary
synapse_stage_latency_ms{component="aco",stage="candidate_retrieval",quantile="0.5"} 12.4
synapse_stage_latency_ms{component="aco",stage="candidate_retrieval",quantile="0.9"} 28.1
synapse_stage_latency_ms{component="aco",stage="candidate_retrieval",quantile="0.99"} 45.2
synapse_stage_latency_ms{component="aco",stage="candidate_retrieval"}_sum 1450.8
synapse_stage_latency_ms{component="aco",stage="candidate_retrieval"}_count 85
```

### 3.2 Operator Diagnostics API (`GET /api/v1/telemetry`)
- **Path**: `/api/v1/telemetry`
- **Authentication**: Required (`Authorization: Bearer <admin_token>`)
- **Content-Type**: `application/json`
- **Output**: Full JSON representation containing raw counters, active gauges, and calculated histogram percentiles (p50, p90, p99, mean, count, sum).

---

## 4. Root-Cause Analysis (RCA) Troubleshooting Runbook

### Case 1: High Query Latency (>500ms)
1. **Inspect Stage Latencies**:
   Query `GET /api/v1/telemetry` or inspect Prometheus metric `synapse_stage_latency_ms`:
   - If `candidate_retrieval` is high: Inspect whether Neo4j multi-hop expansion or embedding computation is blocking. Check `synapse_cache_operations_total{cache="embedding"}` for high miss rates.
   - If `semantic_ranking` is high: Check whether the candidate count before ranking exceeded optimal limits (>50 items).
   - If `token_optimization` or `knapsack_packing` is high: Verify token estimation mode is not repeatedly invoking heavy tokenizers.
2. **Inspect Per-Query Metadata**:
   Review the `retrieval_metadata.stage_latencies_ms` block returned in the query response payload to pinpoint the exact bottleneck.

### Case 2: Degraded Cache Hit Rate
1. Check `synapse_cache_operations_total{result="miss"}` vs `{result="hit"}`.
2. If `l1_lru` hit rate drops below 40%, check if high session churn or rapid file edits triggered repeated invalidations via `synapse_cache_operations_total{result="invalidate"}`.
3. Review whether `CACHE_ENABLED=true` is set in configuration.

### Case 3: Dependency Health Degradation
1. **Liveness vs Readiness Isolation**:
   - `GET /health` returns process liveness. If 200 OK, the Python runtime and HTTP server are functional.
   - `GET /ready` returns dependency readiness. If 503 Service Unavailable, inspect the `checks` payload:
     - `database: error: ...` -> SQLite file lock, disk space issue, or corrupt schema migrations.
     - `orchestrator: not_configured` -> ACO engine failed to initialize during startup lifespan.
2. Check `synapse_dependency_health` gauges in Prometheus alerting to fire automated alerts before traffic is routed.
