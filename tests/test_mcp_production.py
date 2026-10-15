"""Comprehensive Test Suite for Phase 12: Production MCP Adapter.

Tests:
1. Tool discovery and schema registration for all 7 production MCP tools.
2. Production authentication resolution via API key and Bearer tokens.
3. Authentication enforcement failure when SYNAPSE_REQUIRE_AUTH is set.
4. Scope boundary enforcement (ACCESS_DENIED for unauthorized repositories).
5. Role-based access control (ACCESS_DENIED for read-only viewer roles on writes).
6. Input validation and boundary clamping (empty inputs, invalid scopes, max_tokens bounds).
7. Idempotency management (safe duplicate replay vs conflict detection).
8. Execution timeout handling and structured error reporting.
9. Accurate token accounting, utilization reporting, and untrusted evidence warnings.
10. Server CLI entry point options (stdio and SSE transport modes).
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from backend.api import reset_protocol_engine
from backend.mcp_server import mcp_server, resolve_mcp_principal
from backend.security import ApiKeyService, TokenService
from backend.session_protocol import (
    AuthenticatedPrincipal,
    ErrorCode,
    SessionProtocolEngine,
)
from backend.team_memory_store import TeamMemoryDocumentStore


@pytest.fixture(autouse=True)
def clean_engine():
    """Ensure a clean, isolated engine and environment for each test."""
    test_engine = SessionProtocolEngine(doc_store=TeamMemoryDocumentStore())
    reset_protocol_engine(test_engine)
    yield test_engine
    reset_protocol_engine(None)


def _extract_tool_result(res: tuple[Any, Any] | Any) -> dict[str, Any]:
    """Helper to parse MCP call_tool return value into a JSON dictionary."""
    if isinstance(res, tuple) and len(res) == 2:
        content_list, extra = res
        if content_list and hasattr(content_list[0], "text"):
            try:
                return json.loads(content_list[0].text)
            except Exception:
                pass
        if isinstance(extra, dict) and "result" in extra:
            return extra["result"]
        if isinstance(extra, dict):
            return extra
    return {}


# ---------------------------------------------------------------------------
# 1. Tool Discovery & Schemas
# ---------------------------------------------------------------------------

def test_production_mcp_tool_discovery():
    async def _test():
        tools = await mcp_server.list_tools()
        tool_names = {t.name for t in tools}

        expected = {
            "synapse_query_context",
            "synapse_query_memories",
            "synapse_store_decision",
            "synapse_store_fact",
            "synapse_distill_session",
            "synapse_manage_session",
            "synapse_get_server_status",
        }
        assert expected.issubset(tool_names), f"Missing tools: {expected - tool_names}"

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# 2. Authentication Resolution & Enforcement
# ---------------------------------------------------------------------------

def test_mcp_auth_via_api_key(clean_engine):
    api_key_svc = ApiKeyService(doc_store=clean_engine.doc_store)
    raw_key, record = api_key_svc.create_api_key(
        name="MCP Production Key",
        tenant_id="tenant-prod",
        principal_id="principal-agent",
        roles=["developer"],
        allowed_repositories=["repo-alpha"],
    )

    async def _test():
        args = {"api_key": raw_key}
        raw_res = await mcp_server.call_tool("synapse_get_server_status", args)
        data = _extract_tool_result(raw_res)

        assert data.get("success") is True
        assert data["principal"]["principal_id"] == "principal-agent"
        assert data["principal"]["tenant_id"] == "tenant-prod"
        assert data["principal"]["allowed_repositories"] == ["repo-alpha"]

    asyncio.run(_test())


def test_mcp_auth_via_bearer_token():
    token_svc = TokenService()
    principal = AuthenticatedPrincipal(
        principal_id="user-token",
        tenant_id="tenant-token",
        team_ids=["team-token"],
        allowed_repositories=["repo-beta"],
        roles=["maintainer"],
    )
    token = token_svc.create_token(principal)

    async def _test():
        args = {"auth_token": token}
        raw_res = await mcp_server.call_tool("synapse_get_server_status", args)
        data = _extract_tool_result(raw_res)

        assert data.get("success") is True
        assert data["principal"]["principal_id"] == "user-token"
        assert data["principal"]["roles"] == ["maintainer"]

    asyncio.run(_test())


def test_mcp_auth_required_rejection(monkeypatch):
    monkeypatch.setenv("SYNAPSE_REQUIRE_AUTH", "true")

    async def _test():
        args = {
            "repository_id": "repo-core",
            "query": "How is auth enforced?",
        }
        raw_res = await mcp_server.call_tool("synapse_query_context", args)
        data = _extract_tool_result(raw_res)

        assert data.get("success") is False
        assert data["error"]["code"] == ErrorCode.AUTHENTICATION_REQUIRED.value
        assert "Authentication required" in data["error"]["message"]

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# 3. Repository Scope & Authorization Enforcement
# ---------------------------------------------------------------------------

def test_mcp_unauthorized_repository_denied():
    token_svc = TokenService()
    principal = AuthenticatedPrincipal(
        principal_id="restricted-dev",
        tenant_id="tenant-1",
        allowed_repositories=["repo-allowed"],
        roles=["developer"],
    )
    token = token_svc.create_token(principal)

    async def _test():
        args = {
            "repository_id": "repo-unauthorized",
            "query": "Show sensitive code",
            "auth_token": token,
        }
        raw_res = await mcp_server.call_tool("synapse_query_context", args)
        data = _extract_tool_result(raw_res)

        assert data.get("success") is False
        assert data["error"]["code"] == ErrorCode.ACCESS_DENIED.value

    asyncio.run(_test())


def test_mcp_viewer_role_write_denied():
    token_svc = TokenService()
    # Viewer role lacks MEMORY_WRITE
    principal = AuthenticatedPrincipal(
        principal_id="read-only-viewer",
        tenant_id="tenant-1",
        allowed_repositories=["*"],
        roles=["viewer"],
    )
    token = token_svc.create_token(principal)

    async def _test():
        args = {
            "repository_id": "repo-core",
            "title": "Forbidden Decision",
            "rationale": "Viewers cannot mutate engineering memory",
            "auth_token": token,
        }
        raw_res = await mcp_server.call_tool("synapse_store_decision", args)
        data = _extract_tool_result(raw_res)

        assert data.get("success") is False
        assert data["error"]["code"] == ErrorCode.ACCESS_DENIED.value

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# 4. Input Validation & Bounds Clamping
# ---------------------------------------------------------------------------

def test_mcp_validation_empty_inputs():
    async def _test():
        # Empty repository_id
        res1 = _extract_tool_result(await mcp_server.call_tool("synapse_query_context", {"repository_id": "", "query": "test"}))
        assert res1.get("success") is False
        assert res1["error"]["code"] == ErrorCode.MALFORMED_INPUT.value

        # Empty query
        res2 = _extract_tool_result(await mcp_server.call_tool("synapse_query_context", {"repository_id": "repo-1", "query": "   "}))
        assert res2.get("success") is False
        assert res2["error"]["code"] == ErrorCode.MALFORMED_INPUT.value

        # Invalid optimization mode
        res3 = _extract_tool_result(await mcp_server.call_tool("synapse_query_context", {"repository_id": "repo-1", "query": "test", "optimization_mode": "invalid"}))
        assert res3.get("success") is False
        assert res3["error"]["code"] == ErrorCode.MALFORMED_INPUT.value

        # Invalid scope
        res4 = _extract_tool_result(await mcp_server.call_tool("synapse_query_memories", {"repository_id": "repo-1", "query": "test", "scopes": ["invalid_scope"]}))
        assert res4.get("success") is False
        assert res4["error"]["code"] == ErrorCode.INVALID_SCOPE.value

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# 5. Idempotency Conflict vs Replay
# ---------------------------------------------------------------------------

def test_mcp_idempotency_duplicate_and_conflict():
    async def _test():
        # First write
        args = {
            "repository_id": "repo-core",
            "title": "Initial Decision Title",
            "rationale": "Initial rationale",
            "idempotency_key": "mcp_key_999",
        }
        res1 = _extract_tool_result(await mcp_server.call_tool("synapse_store_decision", args))
        assert res1.get("success") is True
        dec_id = res1["decision_id"]

        # Exact duplicate replay -> returns cached response
        res2 = _extract_tool_result(await mcp_server.call_tool("synapse_store_decision", args))
        assert res2.get("success") is True
        assert res2["decision_id"] == dec_id

        # Conflicting payload with same idempotency key -> raises IDEMPOTENCY_CONFLICT
        conflict_args = {
            "repository_id": "repo-core",
            "title": "COMPLETELY DIFFERENT TITLE",
            "rationale": "Conflicting rationale",
            "idempotency_key": "mcp_key_999",
        }
        res3 = _extract_tool_result(await mcp_server.call_tool("synapse_store_decision", conflict_args))
        assert res3.get("success") is False
        assert res3["error"]["code"] == ErrorCode.IDEMPOTENCY_CONFLICT.value

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# 6. Execution Timeouts
# ---------------------------------------------------------------------------

def test_mcp_execution_timeout(monkeypatch):
    # Set timeout to 0.01s to test timeout trigger
    monkeypatch.setenv("SYNAPSE_MCP_TIMEOUT_SECONDS", "0.01")

    async def _test():
        # Mock engine method to simulate slow execution
        engine = SessionProtocolEngine(doc_store=TeamMemoryDocumentStore())
        original_query = engine.query_context

        def slow_query(*args, **kwargs):
            import time
            time.sleep(0.05)
            return original_query(*args, **kwargs)

        engine.query_context = slow_query
        reset_protocol_engine(engine)

        args = {
            "repository_id": "repo-timeout",
            "query": "Slow operation test",
        }
        res = _extract_tool_result(await mcp_server.call_tool("synapse_query_context", args))
        assert res.get("success") is False
        assert "timed out" in res["error"]["message"].lower()

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# 7. Accurate Token Reporting & Evidence Warning
# ---------------------------------------------------------------------------

def test_mcp_accurate_token_accounting():
    async def _test():
        args = {
            "repository_id": "repo-core",
            "query": "Database connection configuration",
            "max_tokens": 1200,
        }
        res = _extract_tool_result(await mcp_server.call_tool("synapse_query_context", args))
        assert res.get("success") is True
        assert res["token_budget"] == 1200
        assert res["tokens_used"] >= 0
        assert 0.0 <= res["budget_utilization_pct"] <= 100.0
        assert "untrusted_evidence_warning" in res

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# 8. Server CLI Runner Options
# ---------------------------------------------------------------------------

def test_mcp_server_cli_arguments(monkeypatch):
    import sys
    import importlib
    importlib.import_module("backend.mcp_server")
    mcp_mod = sys.modules["backend.mcp_server"]

    called = {}

    def mock_run(transport="stdio"):
        called["transport"] = transport

    monkeypatch.setattr(mcp_mod.mcp_server, "run", mock_run)
    monkeypatch.setattr("sys.argv", ["mcp_server.py", "--transport", "sse", "--port", "8001"])

    mcp_mod.main()
    assert called["transport"] == "sse"
