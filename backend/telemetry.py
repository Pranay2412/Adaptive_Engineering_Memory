"""Enterprise Observability, Structured Logging, and Metrics for Synapse (Phase 18).

Provides:
1. Context-local request & session correlation via contextvars.
2. Privacy-preserving structured JSON log formatting with automatic secret redaction.
3. Thread-safe Prometheus / OpenMetrics registry capturing request counts, latency histograms,
   ACO stage latencies, Section 7 token usage, cache metrics, and dependency health.
4. Safe query fingerprinting to prevent metric cardinality explosion and prompt leakage.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
import threading
import time
from typing import Any, Callable

from .session_protocol import SecretRedactor

logger = logging.getLogger(__name__)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 1. Context-Local Correlation State
# ---------------------------------------------------------------------------

_request_id_ctx: ContextVar[str] = ContextVar("synapse_request_id", default="req_system")
_session_id_ctx: ContextVar[str | None] = ContextVar("synapse_session_id", default=None)
_tenant_id_ctx: ContextVar[str | None] = ContextVar("synapse_tenant_id", default=None)


def set_request_context(
    request_id: str,
    session_id: str | None = None,
    tenant_id: str | None = None,
) -> tuple[Any, Any, Any]:
    """Bind correlation identifiers to the current task execution context."""
    tok1 = _request_id_ctx.set(request_id)
    tok2 = _session_id_ctx.set(session_id)
    tok3 = _tenant_id_ctx.set(tenant_id)
    return (tok1, tok2, tok3)


def reset_request_context(tokens: tuple[Any, Any, Any]) -> None:
    """Restore previous task execution correlation context."""
    _request_id_ctx.reset(tokens[0])
    _session_id_ctx.reset(tokens[1])
    _tenant_id_ctx.reset(tokens[2])


def get_current_request_id() -> str:
    return _request_id_ctx.get()


def get_current_session_id() -> str | None:
    return _session_id_ctx.get()


def get_current_tenant_id() -> str | None:
    return _tenant_id_ctx.get()


# ---------------------------------------------------------------------------
# 2. Structured JSON Logging Formatter
# ---------------------------------------------------------------------------

class StructuredJsonFormatter(logging.Formatter):
    """Formats log records as structured, secret-redacted JSON."""

    def __init__(self, service_name: str = "synapse") -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        # 1. Base log record fields
        log_data: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "message": SecretRedactor.redact(record.getMessage()),
            "request_id": getattr(record, "request_id", None) or get_current_request_id(),
        }

        # 2. Optional correlation identifiers
        sess_id = getattr(record, "session_id", None) or get_current_session_id()
        if sess_id:
            log_data["session_id"] = sess_id

        tenant_id = getattr(record, "tenant_id", None) or get_current_tenant_id()
        if tenant_id:
            log_data["tenant_id"] = tenant_id

        # 3. Component & Execution Metrics
        if hasattr(record, "component"):
            log_data["component"] = record.component
        if hasattr(record, "duration_ms"):
            log_data["duration_ms"] = round(record.duration_ms, 2)
        if hasattr(record, "status"):
            log_data["status"] = record.status

        # 4. Exception details with masked traces
        if record.exc_info:
            log_data["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else "Exception",
                "message": SecretRedactor.redact(str(record.exc_info[1])),
            }

        # 5. Sanitize extra metadata
        if hasattr(record, "metadata") and isinstance(record.metadata, dict):
            log_data["metadata"] = SecretRedactor.redact_dict(record.metadata)

        return json.dumps(log_data)


# ---------------------------------------------------------------------------
# 3. Privacy-Preserving Query Fingerprinting
# ---------------------------------------------------------------------------

def hash_query_fingerprint(query: str) -> str:
    """Generate a low-cardinality SHA-256 fingerprint for a query without storing raw text."""
    clean = re.sub(r"\s+", " ", query.strip().lower())
    return f"q_{hashlib.sha256(clean.encode('utf-8')).hexdigest()[:12]}"


# ---------------------------------------------------------------------------
# 4. Prometheus / OpenMetrics Registry
# ---------------------------------------------------------------------------

@dataclass
class HistogramStats:
    count: int = 0
    total_sum: float = 0.0
    values: list[float] = field(default_factory=list)

    def observe(self, val: float) -> None:
        self.count += 1
        self.total_sum += val
        # Keep fixed reservoir of recent samples for percentiles
        if len(self.values) < 2000:
            self.values.append(val)
        else:
            self.values[self.count % 2000] = val

    def percentile(self, p: float) -> float:
        if not self.values:
            return 0.0
        sorted_v = sorted(self.values)
        k = (len(sorted_v) - 1) * p
        f = int(k)
        c = f + 1
        if c < len(sorted_v):
            d0 = sorted_v[f] * (c - k)
            d1 = sorted_v[c] * (k - f)
            return round(d0 + d1, 3)
        return round(sorted_v[f], 3)


class MetricsRegistry:
    """Thread-safe Prometheus/OpenMetrics registry and aggregation engine."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.counters: dict[str, float] = {}
        self.gauges: dict[str, float] = {}
        self.histograms: dict[str, HistogramStats] = {}
        self.started_at = utc_now_iso()

        # Initialize core system gauges
        self.gauges["synapse_active_sessions"] = 0.0
        self.gauges['synapse_dependency_health{dependency="sqlite"}'] = 1.0
        self.gauges['synapse_dependency_health{dependency="neo4j"}'] = 1.0
        self.gauges['synapse_dependency_health{dependency="embeddings"}'] = 1.0
        self.gauges['synapse_dependency_health{dependency="llm"}'] = 1.0

    def inc_counter(self, name: str, amount: float = 1.0) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0.0) + amount

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self.gauges[name] = float(value)

    def observe_histogram(self, name: str, value: float) -> None:
        with self._lock:
            if name not in self.histograms:
                self.histograms[name] = HistogramStats()
            self.histograms[name].observe(value)

    # --- High-Level Convenience Recorders ---

    def record_http_request(self, method: str, endpoint: str, status_code: int, duration_sec: float) -> None:
        """Record HTTP request count, status code, and duration histogram."""
        clean_ep = endpoint.split("?")[0]
        # Sanitize parameter values in path for low cardinality
        clean_ep = re.sub(r"/sessions/[^/]+", "/sessions/{id}", clean_ep)
        clean_ep = re.sub(r"/checkpoints/[^/]+", "/checkpoints/{id}", clean_ep)

        counter_name = f'synapse_http_requests_total{{method="{method}",endpoint="{clean_ep}",status="{status_code}"}}'
        hist_name = f'synapse_http_request_duration_seconds{{method="{method}",endpoint="{clean_ep}"}}'

        self.inc_counter(counter_name, 1.0)
        self.observe_histogram(hist_name, duration_sec)

    def record_stage_latency(self, stage: str, duration_ms: float, component: str = "aco") -> None:
        """Record latency for an internal orchestration pipeline stage."""
        hist_name = f'synapse_stage_latency_ms{{component="{component}",stage="{stage}"}}'
        self.observe_histogram(hist_name, duration_ms)

    def record_token_metrics(
        self,
        knowledge_context_tokens: int,
        query_tokens: int,
        instruction_tokens: int,
        budget: int,
        repository_id: str = "synapse-core",
    ) -> None:
        """Record Section 7 token usage and budget utilization."""
        clean_repo = re.sub(r"[^a-zA-Z0-9_\-]", "_", repository_id)

        self.inc_counter(f'synapse_tokens_total{{repo="{clean_repo}",type="knowledge_context"}}', float(knowledge_context_tokens))
        self.inc_counter(f'synapse_tokens_total{{repo="{clean_repo}",type="query"}}', float(query_tokens))
        self.inc_counter(f'synapse_tokens_total{{repo="{clean_repo}",type="instructions"}}', float(instruction_tokens))
        self.inc_counter(f'synapse_tokens_total{{repo="{clean_repo}",type="total_input"}}', float(knowledge_context_tokens + query_tokens + instruction_tokens))

        if budget > 0:
            utilization = round(knowledge_context_tokens / budget, 4)
            self.observe_histogram(f'synapse_context_budget_utilization_ratio{{repo="{clean_repo}"}}', utilization)

    def record_cache_operation(self, cache_name: str, result: str) -> None:
        """Record multi-tier cache operation: hit, miss, invalidation."""
        self.inc_counter(f'synapse_cache_operations_total{{cache="{cache_name}",result="{result}"}}', 1.0)

    def record_ingestion_job(self, repository_id: str, status: str, duration_sec: float) -> None:
        """Record repository ingestion job outcome and duration."""
        clean_repo = re.sub(r"[^a-zA-Z0-9_\-]", "_", repository_id)
        self.inc_counter(f'synapse_ingestion_jobs_total{{repo="{clean_repo}",status="{status}"}}', 1.0)
        self.observe_histogram(f'synapse_ingestion_duration_seconds{{repo="{clean_repo}"}}', duration_sec)

    def set_dependency_health(self, dependency: str, healthy: bool) -> None:
        """Update health gauge for an external or persistent dependency."""
        self.set_gauge(f'synapse_dependency_health{{dependency="{dependency}"}}', 1.0 if healthy else 0.0)

    # --- Export Formats ---

    def export_openmetrics(self) -> str:
        """Render metrics in Prometheus/OpenMetrics text format."""
        lines: list[str] = [
            "# HELP synapse_http_requests_total Total HTTP requests received",
            "# TYPE synapse_http_requests_total counter",
        ]
        with self._lock:
            # Counters
            for name, val in sorted(self.counters.items()):
                lines.append(f"{name} {val}")

            # Gauges
            lines.append("\n# HELP synapse_dependency_health Health status of subsystems (1=healthy, 0=unhealthy)")
            lines.append("# TYPE synapse_dependency_health gauge")
            for name, val in sorted(self.gauges.items()):
                lines.append(f"{name} {val}")

            # Histograms (count, sum, p50, p90, p99)
            lines.append("\n# HELP synapse_latency_summary Latency summary and percentiles")
            lines.append("# TYPE synapse_latency_summary summary")
            for name, hist in sorted(self.histograms.items()):
                base = name.rstrip("}")
                sep = "," if "{" in name else "{"
                lines.append(f"{base}{sep}quantile=\"0.5\"}} {hist.percentile(0.5)}")
                lines.append(f"{base}{sep}quantile=\"0.9\"}} {hist.percentile(0.9)}")
                lines.append(f"{base}{sep}quantile=\"0.99\"}} {hist.percentile(0.99)}")
                lines.append(f"{name}_sum {round(hist.total_sum, 4)}")
                lines.append(f"{name}_count {hist.count}")

        return "\n".join(lines) + "\n"

    def export_json(self) -> dict[str, Any]:
        """Render aggregated telemetry summary as a structured JSON document."""
        with self._lock:
            hist_summaries = {}
            for name, hist in self.histograms.items():
                hist_summaries[name] = {
                    "count": hist.count,
                    "sum": round(hist.total_sum, 3),
                    "mean": round(hist.total_sum / hist.count, 3) if hist.count > 0 else 0.0,
                    "p50": hist.percentile(0.5),
                    "p90": hist.percentile(0.9),
                    "p99": hist.percentile(0.99),
                }

            return {
                "started_at": self.started_at,
                "timestamp": utc_now_iso(),
                "counters": dict(self.counters),
                "gauges": dict(self.gauges),
                "histograms": hist_summaries,
            }

    def reset(self) -> None:
        """Clear all metrics for test isolation."""
        with self._lock:
            self.counters.clear()
            self.gauges.clear()
            self.histograms.clear()
            self.gauges["synapse_active_sessions"] = 0.0
            self.gauges['synapse_dependency_health{dependency="sqlite"}'] = 1.0
            self.gauges['synapse_dependency_health{dependency="neo4j"}'] = 1.0
            self.gauges['synapse_dependency_health{dependency="embeddings"}'] = 1.0
            self.gauges['synapse_dependency_health{dependency="llm"}'] = 1.0


# Global default telemetry registry singleton
default_telemetry_registry = MetricsRegistry()
