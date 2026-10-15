"""Main Client Implementation for Synapse Python SDK (Phase 11).

Provides SynapseClient:
- Typed session, context, and memory operations
- Strict idempotency-aware safe retry policies (never blindly retries non-idempotent writes)
- Bearer token, API key, and development identity management
- Connection lifecycle and context manager support
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import httpx

from .config import ClientConfig
from .exceptions import (
    SynapseApiError,
    SynapseConnectionError,
    SynapseError,
    SynapseTimeoutError,
    exception_from_response,
)
from .models import (
    ContextResult,
    DecisionRecord,
    DistillationResult,
    FactRecord,
    HealthStatus,
    ReadinessStatus,
    ScopedQueryResult,
    SessionInfo,
)

logger = logging.getLogger(__name__)


class SynapseClient:
    """Client for interacting with the Synapse Adaptive Engineering Memory Platform.

    Usage:
        client = SynapseClient(base_url="http://localhost:8000", api_key="sk-...")
        # or as context manager:
        with SynapseClient(base_url="http://localhost:8000") as client:
            health = client.check_health()
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: str | None = None,
        bearer_token: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        config: ClientConfig | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if config is not None:
            self.config = config
        else:
            self.config = ClientConfig(
                base_url=base_url,
                api_key=api_key,
                bearer_token=bearer_token,
                timeout=timeout,
                max_retries=max_retries,
                backoff_factor=backoff_factor,
            )

        self._client = httpx.Client(
            base_url=self.config.base_url,
            timeout=self.config.timeout,
            transport=transport,
            headers={
                "User-Agent": "Synapse-Python-SDK/1.0.0",
                "Accept": "application/json",
            },
        )
        self._is_closed = False

    def __enter__(self) -> SynapseClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close client and release underlying connection pools."""
        if not self._is_closed:
            self._client.close()
            self._is_closed = True

    # -----------------------------------------------------------------------
    # Internal Request Dispatcher with Safe Retry Policy
    # -----------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        json_data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        custom_headers: dict[str, str] | None = None,
        is_idempotent: bool = False,
    ) -> dict[str, Any]:
        """Dispatch HTTP request with auth headers and strict idempotency-aware retries.

        CRITICAL RETRY RULE:
        - Safe, idempotent requests (GET, or requests with an idempotency_key) may retry on transient errors.
        - Non-idempotent mutating POST requests WITHOUT an idempotency_key MUST NEVER be automatically retried.
        """
        if self._is_closed:
            raise SynapseError("Cannot make request: SynapseClient is closed.")

        url = path
        headers = {**self.config.get_auth_headers(), **self.config.custom_headers, **(custom_headers or {})}

        # Determine idempotency: if json payload contains an idempotency_key, operation is idempotent
        effective_idempotent = is_idempotent
        if json_data and isinstance(json_data, dict):
            env = json_data.get("envelope", {})
            if isinstance(env, dict) and env.get("idempotency_key"):
                effective_idempotent = True

        max_attempts = (self.config.max_retries + 1) if effective_idempotent else 1
        last_exception: Exception | None = None

        for attempt in range(max_attempts):
            try:
                response = self._client.request(
                    method=method,
                    url=url,
                    json=json_data,
                    params=params,
                    headers=headers,
                )

                # Success path (2xx)
                if 200 <= response.status_code < 300:
                    try:
                        return response.json()
                    except Exception:
                        return {"status": "ok", "raw_content": response.text}

                # Parse structured error
                try:
                    err_data = response.json()
                except Exception:
                    err_data = {"error": {"message": response.text}}

                api_err = exception_from_response(
                    status_code=response.status_code,
                    data=err_data,
                    raw_text=response.text,
                )

                # Check if error is transient and request is idempotent
                is_transient = response.status_code in (502, 503, 504) or api_err.retryable
                if effective_idempotent and is_transient and (attempt < max_attempts - 1):
                    sleep_time = self.config.backoff_factor * (2 ** attempt)
                    logger.warning(
                        "Transient error %d on idempotent %s %s (attempt %d/%d). Retrying in %.2fs...",
                        response.status_code,
                        method,
                        path,
                        attempt + 1,
                        max_attempts,
                        sleep_time,
                    )
                    time.sleep(sleep_time)
                    continue

                # Not retryable or non-idempotent: raise immediately
                raise api_err

            except httpx.TimeoutException as exc:
                last_exception = SynapseTimeoutError(f"Request {method} {url} timed out: {exc}")
                if effective_idempotent and (attempt < max_attempts - 1):
                    sleep_time = self.config.backoff_factor * (2 ** attempt)
                    logger.warning(
                        "Timeout on idempotent %s %s (attempt %d/%d). Retrying in %.2fs...",
                        method,
                        path,
                        attempt + 1,
                        max_attempts,
                        sleep_time,
                    )
                    time.sleep(sleep_time)
                    continue
                raise last_exception from exc

            except httpx.NetworkError as exc:
                last_exception = SynapseConnectionError(f"Network error connecting to {url}: {exc}")
                if effective_idempotent and (attempt < max_attempts - 1):
                    sleep_time = self.config.backoff_factor * (2 ** attempt)
                    logger.warning(
                        "Connection error on idempotent %s %s (attempt %d/%d). Retrying in %.2fs...",
                        method,
                        path,
                        attempt + 1,
                        max_attempts,
                        sleep_time,
                    )
                    time.sleep(sleep_time)
                    continue
                raise last_exception from exc

        if last_exception:
            raise last_exception
        raise SynapseError(f"Failed to execute {method} {url} after {max_attempts} attempts.")

    # -----------------------------------------------------------------------
    # System Probes & Diagnostics
    # -----------------------------------------------------------------------

    def check_health(self) -> HealthStatus:
        """Check service liveness and capability status."""
        data = self._request("GET", "/api/v1/health", is_idempotent=True)
        return HealthStatus.from_dict(data)

    def check_readiness(self) -> ReadinessStatus:
        """Check service readiness and database operational connectivity."""
        data = self._request("GET", "/api/v1/ready", is_idempotent=True)
        return ReadinessStatus.from_dict(data)

    def get_service_status(self) -> dict[str, Any]:
        """Retrieve detailed service metrics, session counts, and database status."""
        return self._request("GET", "/api/v1/status", is_idempotent=True)

    def get_config(self) -> dict[str, Any]:
        """Retrieve runtime configuration with credentials safely redacted."""
        return self._request("GET", "/api/v1/config", is_idempotent=True)

    # -----------------------------------------------------------------------
    # Session Operations
    # -----------------------------------------------------------------------

    def create_session(
        self,
        repository_id: str,
        title: str = "",
        client_type: str = "cli",
        assistant_name: str = "python-sdk",
        assistant_version: str = "1.0.0",
        ttl_seconds: int = 86400,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> SessionInfo:
        """Create a new engineering session bound to repository and client identity."""
        if not idempotency_key and self.config.auto_idempotency_keys:
            idempotency_key = f"sdk_create_{uuid.uuid4().hex}"

        payload: dict[str, Any] = {
            "repository": {"repository_id": repository_id},
            "client": {
                "client_type": client_type,
                "assistant_name": assistant_name,
                "assistant_version": assistant_version,
            },
            "title": title,
            "ttl_seconds": ttl_seconds,
            "metadata": metadata or {},
            "envelope": {"idempotency_key": idempotency_key} if idempotency_key else {},
        }
        data = self._request("POST", "/api/v1/sessions", json_data=payload, is_idempotent=bool(idempotency_key))
        return SessionInfo.from_dict(data)

    def resume_session(
        self,
        session_id: str,
        client_type: str = "cli",
        assistant_name: str = "python-sdk",
    ) -> SessionInfo:
        """Resume or inspect an active session."""
        payload = {
            "client": {
                "client_type": client_type,
                "assistant_name": assistant_name,
            }
        }
        data = self._request("POST", f"/api/v1/sessions/{session_id}/resume", json_data=payload, is_idempotent=True)
        return SessionInfo.from_dict(data)

    def get_session(self, session_id: str) -> SessionInfo:
        """Inspect active session state by ID."""
        data = self._request("GET", f"/api/v1/sessions/{session_id}", is_idempotent=True)
        return SessionInfo.from_dict(data)

    def close_session(
        self,
        session_id: str,
        reason: str = "completed",
        summary: str = "",
        idempotency_key: str | None = None,
    ) -> SessionInfo:
        """Close an active session, finalizing summary and preventing further modifications."""
        if not idempotency_key and self.config.auto_idempotency_keys:
            idempotency_key = f"sdk_close_{uuid.uuid4().hex}"

        payload: dict[str, Any] = {
            "reason": reason,
            "summary": summary,
            "envelope": {"idempotency_key": idempotency_key} if idempotency_key else {},
        }
        data = self._request("POST", f"/api/v1/sessions/{session_id}/close", json_data=payload, is_idempotent=bool(idempotency_key))
        return SessionInfo.from_dict(data)

    # -----------------------------------------------------------------------
    # Context Retrieval via Adaptive Context Orchestrator (ACO)
    # -----------------------------------------------------------------------

    def query_context(
        self,
        session_id: str,
        repository_id: str,
        query: str,
        max_tokens: int = 1500,
        optimization_mode: str = "deterministic",
        active_file: str | None = None,
        cursor_position: int | None = None,
        include_scratchpad: bool = False,
    ) -> ContextResult:
        """Retrieve token-budgeted, multi-channel context package for an engineering task."""
        payload = {
            "repository_id": repository_id,
            "query": query,
            "max_tokens": max_tokens,
            "optimization_mode": optimization_mode,
            "active_file": active_file,
            "cursor_position": cursor_position,
            "include_scratchpad": include_scratchpad,
        }
        data = self._request("POST", f"/api/v1/sessions/{session_id}/context", json_data=payload, is_idempotent=True)
        return ContextResult.from_dict(data, query=query)

    # -----------------------------------------------------------------------
    # Durable Memory Writes (Decisions, Facts, Distillation)
    # -----------------------------------------------------------------------

    def store_decision(
        self,
        session_id: str,
        repository_id: str,
        title: str,
        rationale: str,
        scope: str = "repository",
        category: str = "architecture",
        status: str = "accepted",
        impact_areas: list[str] | None = None,
        affected_symbols: list[str] | None = None,
        team_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> DecisionRecord:
        """Store an architectural decision record (ADR) under repository or team scope."""
        if not idempotency_key and self.config.auto_idempotency_keys:
            idempotency_key = f"sdk_dec_{uuid.uuid4().hex}"

        payload: dict[str, Any] = {
            "repository_id": repository_id,
            "title": title,
            "rationale": rationale,
            "scope": scope,
            "category": category,
            "status": status,
            "impact_areas": impact_areas or [],
            "affected_symbols": affected_symbols or [],
            "team_id": team_id,
            "envelope": {"idempotency_key": idempotency_key} if idempotency_key else {},
        }
        data = self._request(
            "POST",
            f"/api/v1/sessions/{session_id}/decisions",
            json_data=payload,
            is_idempotent=bool(idempotency_key),
        )
        return DecisionRecord.from_dict(data, title=title, rationale=rationale)

    def store_fact(
        self,
        session_id: str,
        repository_id: str,
        title: str,
        content: str,
        category: str = "implementation_summary",
        scope: str = "repository",
        actionable_takeaways: list[str] | None = None,
        impact_areas: list[str] | None = None,
        team_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> FactRecord:
        """Store a technical recipe, recurring solution, or root cause fact."""
        if not idempotency_key and self.config.auto_idempotency_keys:
            idempotency_key = f"sdk_fact_{uuid.uuid4().hex}"

        payload: dict[str, Any] = {
            "repository_id": repository_id,
            "title": title,
            "content": content,
            "category": category,
            "scope": scope,
            "actionable_takeaways": actionable_takeaways or [],
            "impact_areas": impact_areas or [],
            "team_id": team_id,
            "envelope": {"idempotency_key": idempotency_key} if idempotency_key else {},
        }
        data = self._request(
            "POST",
            f"/api/v1/sessions/{session_id}/facts",
            json_data=payload,
            is_idempotent=bool(idempotency_key),
        )
        return FactRecord.from_dict(data, title=title, content=content)

    def distill_session(
        self,
        session_id: str,
        repository_id: str,
        prompt: str,
        response_summary: str,
        affected_files: list[str] | None = None,
        affected_symbols: list[str] | None = None,
        engineering_decisions: list[str] | None = None,
        visibility: str = "repository",
        team_id: str | None = None,
        store_raw_transcript: bool = False,
        raw_messages: list[dict[str, Any]] | None = None,
    ) -> DistillationResult:
        """Distill complete interaction into reusable engineering memory cards."""
        payload: dict[str, Any] = {
            "repository_id": repository_id,
            "prompt": prompt,
            "response_summary": response_summary,
            "affected_files": affected_files or [],
            "affected_symbols": affected_symbols or [],
            "engineering_decisions": engineering_decisions or [],
            "visibility": visibility,
            "team_id": team_id,
            "store_raw_transcript": store_raw_transcript,
            "raw_messages": raw_messages,
        }
        data = self._request("POST", f"/api/v1/sessions/{session_id}/distill", json_data=payload, is_idempotent=False)
        return DistillationResult.from_dict(data)

    # -----------------------------------------------------------------------
    # Scoped Cross-Repository / Cross-Team Retrieval
    # -----------------------------------------------------------------------

    def query_memories(
        self,
        repository_id: str,
        query: str,
        allowed_scopes: list[str] | None = None,
        category_filter: list[str] | None = None,
        limit: int = 10,
    ) -> ScopedQueryResult:
        """Query stored engineering memories within permitted scopes and categories."""
        payload = {
            "repository_id": repository_id,
            "query": query,
            "allowed_scopes": allowed_scopes or ["repository", "team"],
            "category_filter": category_filter,
            "limit": limit,
        }
        data = self._request("POST", "/api/v1/memories/query", json_data=payload, is_idempotent=True)
        return ScopedQueryResult.from_dict(data)

    # -----------------------------------------------------------------------
    # Security, Identity & Audit (Phase 9)
    # -----------------------------------------------------------------------

    def get_my_principal(self) -> dict[str, Any]:
        """Retrieve verified identity, active roles, and granted permissions."""
        return self._request("GET", "/api/v1/auth/me", is_idempotent=True)

    def list_audit_events(
        self,
        tenant_id: str | None = None,
        principal_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List security audit trail records (requires admin or maintainer role)."""
        params: dict[str, Any] = {"limit": limit}
        if tenant_id:
            params["tenant_id"] = tenant_id
        if principal_id:
            params["principal_id"] = principal_id
        if event_type:
            params["event_type"] = event_type
        return self._request("GET", "/api/v1/security/audit", params=params, is_idempotent=True)

    def create_api_key(
        self,
        name: str,
        roles: list[str] | None = None,
        allowed_repositories: list[str] | None = None,
        allowed_teams: list[str] | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """Generate high-entropy API key (requires admin or maintainer role)."""
        payload = {
            "name": name,
            "roles": roles or ["developer"],
            "allowed_repositories": allowed_repositories or ["*"],
            "allowed_teams": allowed_teams or [],
            "expires_at": expires_at,
        }
        return self._request("POST", "/api/v1/auth/api-keys", json_data=payload, is_idempotent=False)

    def revoke_api_key(self, key_id: str) -> bool:
        """Revoke active API key by ID (requires admin or maintainer role)."""
        res = self._request("DELETE", f"/api/v1/auth/api-keys/{key_id}", is_idempotent=True)
        return bool(res.get("revoked", False))
