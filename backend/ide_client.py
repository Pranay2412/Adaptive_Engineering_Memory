"""Synapse Real-Client / IDE Integration Adapter (Phase 6.4).

Provides an independent client adapter designed for IDE extensions
(VS Code, Cursor, Antigravity) that interacts with Synapse over standard HTTP
or MCP protocols without depending on internal implementation details.

Capture Model:
- Capture is TOOL-INVOKED and DEVELOPER-APPROVED.
- Memories and decisions are captured when explicitly commanded by the user
  or coding assistant (via synapse_store_decision or synapse_distill_session).
- Indiscriminate background keystroke capture is intentionally avoided to prevent
  polluting engineering memory with ephemeral syntax trial-and-error.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .session_protocol import (
    ErrorCode,
    ProtocolException,
)

logger = logging.getLogger(__name__)


class SynapseIDEClient:
    """Client adapter for IDE extensions communicating with Synapse REST/MCP service."""

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        principal_id: str = "ide-developer",
        tenant_id: str = "local-tenant",
        allowed_repositories: list[str] | None = None,
        timeout_seconds: float = 10.0,
        app_client: Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.principal_id = principal_id
        self.tenant_id = tenant_id
        self.allowed_repositories = allowed_repositories or ["*"]
        self.timeout_seconds = timeout_seconds
        # Optional in-process TestClient for deterministic unit/contract testing
        self._app_client = app_client

    def _request(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        custom_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Dispatch HTTP request to Synapse API with standard identity headers."""
        headers = {
            "Content-Type": "application/json",
            "X-Principal-ID": self.principal_id,
            "X-Tenant-ID": self.tenant_id,
            "X-Allowed-Repositories": ",".join(self.allowed_repositories),
        }
        if custom_headers:
            headers.update(custom_headers)

        # In-process TestClient dispatch
        if self._app_client is not None:
            endpoint = path
            if method.upper() == "GET":
                resp = self._app_client.get(endpoint, headers=headers)
            elif method.upper() == "POST":
                resp = self._app_client.post(endpoint, json=data, headers=headers)
            elif method.upper() == "PUT":
                resp = self._app_client.put(endpoint, json=data, headers=headers)
            elif method.upper() == "DELETE":
                resp = self._app_client.delete(endpoint, headers=headers)
            else:
                raise ValueError(f"Unsupported method: {method}")

            resp_json = resp.json()
            if resp.status_code >= 400:
                err_detail = resp_json.get("error", {})
                code = err_detail.get("code", "UNKNOWN_ERROR")
                msg = err_detail.get("message", f"HTTP {resp.status_code}")
                raise ProtocolException(code, msg, details=err_detail)
            return resp_json

        # Live network dispatch via urllib
        url = f"{self.base_url}{path}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = Request(url, data=body, headers=headers, method=method)

        try:
            with urlopen(req, timeout=self.timeout_seconds) as resp:
                resp_text = resp.read().decode("utf-8")
                return json.loads(resp_text)
        except HTTPError as e:
            err_text = e.read().decode("utf-8")
            try:
                err_json = json.loads(err_text)
                err_detail = err_json.get("error", {})
                code = err_detail.get("code", f"HTTP_{e.code}")
                msg = err_detail.get("message", err_text)
                raise ProtocolException(code, msg, details=err_detail) from None
            except json.JSONDecodeError:
                raise ProtocolException(f"HTTP_{e.code}", err_text) from None
        except URLError as e:
            raise ProtocolException(ErrorCode.INTERNAL_ERROR.value, f"Connection failed: {e.reason}") from None

    # --- Session Operations ---

    def create_session(
        self,
        repository_id: str,
        title: str,
        client_type: str = "vscode",
        client_version: str = "1.85.0",
        assistant_name: str = "synapse-extension",
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Create a development session and return its ID."""
        payload = {
            "repository": {"repository_id": repository_id},
            "client": {
                "client_type": client_type,
                "client_version": client_version,
                "assistant_name": assistant_name,
            },
            "title": title,
            "metadata": metadata or {},
            "envelope": {"idempotency_key": idempotency_key} if idempotency_key else {},
        }
        res = self._request("POST", "/api/v1/sessions", payload)
        return str(res["session_id"])

    def resume_session(self, session_id: str) -> dict[str, Any]:
        """Resume an active session and get its current state."""
        return self._request("POST", f"/api/v1/sessions/{session_id}/resume", {})

    def close_session(self, session_id: str, summary: str = "") -> dict[str, Any]:
        """Close a development session."""
        return self._request("POST", f"/api/v1/sessions/{session_id}/close", {"summary": summary})

    # --- Context Operations ---

    def query_context(
        self,
        session_id: str,
        repository_id: str,
        query: str,
        max_tokens: int = 1500,
        optimization_mode: str = "deterministic",
        active_file: str | None = None,
    ) -> dict[str, Any]:
        """Query budgeted context for an engineering prompt via ACO."""
        payload = {
            "repository_id": repository_id,
            "query": query,
            "max_tokens": max_tokens,
            "optimization_mode": optimization_mode,
            "active_file": active_file,
        }
        return self._request("POST", f"/api/v1/sessions/{session_id}/context", payload)

    # --- Memory Operations ---

    def record_decision(
        self,
        session_id: str,
        repository_id: str,
        title: str,
        rationale: str,
        scope: str = "repository",
        category: str = "architecture",
        impact_areas: list[str] | None = None,
        affected_symbols: list[str] | None = None,
        team_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Persist an approved architectural decision (ADR)."""
        payload = {
            "repository_id": repository_id,
            "title": title,
            "rationale": rationale,
            "scope": scope,
            "category": category,
            "status": "accepted",
            "impact_areas": impact_areas or [],
            "affected_symbols": affected_symbols or [],
            "team_id": team_id,
            "envelope": {"idempotency_key": idempotency_key} if idempotency_key else {},
        }
        res = self._request("POST", f"/api/v1/sessions/{session_id}/decisions", payload)
        return str(res["decision_id"])

    def record_fact(
        self,
        session_id: str,
        repository_id: str,
        title: str,
        content: str,
        scope: str = "repository",
        category: str = "implementation_summary",
        actionable_takeaways: list[str] | None = None,
    ) -> str:
        """Store an engineering fact, recipe, or recurring solution."""
        payload = {
            "repository_id": repository_id,
            "title": title,
            "content": content,
            "scope": scope,
            "category": category,
            "actionable_takeaways": actionable_takeaways or [],
        }
        res = self._request("POST", f"/api/v1/sessions/{session_id}/facts", payload)
        return str(res["memory_id"])

    def distill_session(
        self,
        session_id: str,
        repository_id: str,
        prompt: str,
        response_summary: str,
        decisions: list[str] | None = None,
        affected_files: list[str] | None = None,
    ) -> dict[str, Any]:
        """Distill an AI session interaction into reusable memory cards."""
        payload = {
            "repository_id": repository_id,
            "prompt": prompt,
            "response_summary": response_summary,
            "engineering_decisions": decisions or [],
            "affected_files": affected_files or [],
        }
        return self._request("POST", f"/api/v1/sessions/{session_id}/distill", payload)

    def query_memories(
        self,
        repository_id: str,
        query: str,
        scopes: list[str] = ["repository", "team"],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Query memories within repository and team scopes."""
        payload = {
            "repository_id": repository_id,
            "query": query,
            "allowed_scopes": scopes,
            "limit": limit,
        }
        res = self._request("POST", "/api/v1/memories/query", payload)
        return res.get("memories", [])
