"""Standalone client demonstration for the Synapse Session & Context API.

Demonstrates that an external IDE or client utility can interact with the
Synapse service purely over HTTP without importing any internal backend packages.
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import Any


class SynapseHttpClient:
    """Lightweight HTTP client demonstrating external API interaction."""

    def __init__(self, base_url: str = "http://localhost:8000", principal_id: str = "user-alice") -> None:
        self.base_url = base_url.rstrip("/")
        self.principal_id = principal_id

    def _request(self, method: str, endpoint: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "X-Principal-ID": self.principal_id,
            "X-Allowed-Repositories": "*",
        }
        body = json.dumps(data).encode("utf-8") if data is not None else None

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            try:
                err_json = json.loads(err_body)
                raise RuntimeError(f"HTTP {e.code}: {err_json}") from None
            except Exception:
                raise RuntimeError(f"HTTP {e.code}: {err_body}") from None

    def check_health(self) -> dict[str, Any]:
        """Check service health."""
        return self._request("GET", "/api/v1/health")

    def create_session(self, repo_id: str, title: str) -> str:
        """Create a new development session and return its ID."""
        payload = {
            "repository": {"repository_id": repo_id},
            "title": title,
            "client": {"client_type": "cli", "assistant_name": "example-client"},
        }
        res = self._request("POST", "/api/v1/sessions", payload)
        return res["session_id"]

    def query_context(self, session_id: str, repo_id: str, query: str, max_tokens: int = 1500) -> dict[str, Any]:
        """Query budgeted context for an engineering query."""
        payload = {
            "repository_id": repo_id,
            "query": query,
            "max_tokens": max_tokens,
        }
        return self._request("POST", f"/api/v1/sessions/{session_id}/context", payload)

    def store_decision(self, session_id: str, repo_id: str, title: str, rationale: str) -> str:
        """Store an architectural decision."""
        payload = {
            "repository_id": repo_id,
            "title": title,
            "rationale": rationale,
            "scope": "repository",
        }
        res = self._request("POST", f"/api/v1/sessions/{session_id}/decisions", payload)
        return res["decision_id"]

    def close_session(self, session_id: str, summary: str) -> dict[str, Any]:
        """Close session."""
        return self._request("POST", f"/api/v1/sessions/{session_id}/close", {"summary": summary})


if __name__ == "__main__":
    print("Synapse Independent HTTP Client Demo")
    print("To test against a live server, launch: uvicorn backend.api:app --port 8000")
    client = SynapseHttpClient()
    print("Client initialized for", client.base_url)
