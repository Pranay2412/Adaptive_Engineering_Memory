"""Unit and contract tests for the Synapse Model Context Protocol (MCP) Adapter (Phase 6.3)."""

import asyncio
import json
from typing import Any
import pytest

from backend.api import reset_protocol_engine
from backend.mcp_server import mcp_server
from backend.session_protocol import SessionProtocolEngine


@pytest.fixture(autouse=True)
def clean_engine():
    """Ensure a clean, isolated engine for each MCP test."""
    test_engine = SessionProtocolEngine()
    reset_protocol_engine(test_engine)
    yield test_engine
    reset_protocol_engine(None)


def _extract_tool_result(res: Any) -> dict[str, Any]:
    """Helper to parse MCP call_tool return value into a JSON dictionary."""
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
# 1. Tool Discovery & Schema Inspection
# ---------------------------------------------------------------------------

def test_mcp_tool_discovery():
    async def _test():
        tools = await mcp_server.list_tools()
        tool_names = {t.name for t in tools}

        expected_tools = {
            "synapse_query_context",
            "synapse_query_memories",
            "synapse_store_decision",
            "synapse_store_fact",
            "synapse_distill_session",
            "synapse_manage_session",
        }
        assert expected_tools.issubset(tool_names), f"Missing tools: {expected_tools - tool_names}"

        for t in tools:
            if t.name in expected_tools:
                assert t.description is not None
                assert len(t.description) > 20

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# 2. Context Query Tool
# ---------------------------------------------------------------------------

def test_mcp_query_context():
    async def _test():
        args = {
            "repository_id": "repo-core-api",
            "query": "Where could JWT verification fail and what code is involved?",
            "max_tokens": 1500,
            "optimization_mode": "deterministic",
        }
        raw_res = await mcp_server.call_tool("synapse_query_context", args)
        data = _extract_tool_result(raw_res)

        assert data.get("success") is True
        assert data["repository_id"] == "repo-core-api"
        assert data["token_budget"] == 1500
        assert data["tokens_used"] > 0
        assert data["budget_utilization_pct"] <= 100.0
        assert "formatted_context" in data
        assert "provenance" in data
        assert "untrusted_evidence_warning" in data

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# 3. Scoped Memory Query Tool
# ---------------------------------------------------------------------------

def test_mcp_query_memories():
    async def _test():
        args = {
            "repository_id": "repo-core-api",
            "query": "authentication RS256 token",
            "scopes": ["repository", "team"],
            "limit": 5,
        }
        raw_res = await mcp_server.call_tool("synapse_query_memories", args)
        data = _extract_tool_result(raw_res)

        assert data.get("success") is True
        assert data["repository_id"] == "repo-core-api"
        assert "total_found" in data
        assert "memories" in data
        assert "repository" in data["scopes_included"]

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# 4. Store Decision & Store Fact Tools
# ---------------------------------------------------------------------------

def test_mcp_store_decision():
    async def _test():
        args = {
            "repository_id": "repo-core-api",
            "title": "Adopt RS256 Asymmetric Tokens",
            "rationale": "RS256 allows third-party token verification using public key cryptography.",
            "scope": "repository",
            "category": "architecture",
            "impact_areas": ["backend/auth.py"],
        }
        raw_res = await mcp_server.call_tool("synapse_store_decision", args)
        data = _extract_tool_result(raw_res)

        assert data.get("success") is True
        assert data["decision_id"].startswith("dec_")
        assert data["status"] == "stored"
        assert data["scope"] == "repository"

    asyncio.run(_test())


def test_mcp_store_fact():
    async def _test():
        args = {
            "repository_id": "repo-core-api",
            "title": "Pytest Execution Command Rule",
            "content": "Execute test suite via python -m pytest in Windows automation.",
            "category": "recurring_solution",
            "takeaways": ["Always use python -m pytest."],
        }
        raw_res = await mcp_server.call_tool("synapse_store_fact", args)
        data = _extract_tool_result(raw_res)

        assert data.get("success") is True
        assert data["memory_id"].startswith("mem_")
        assert data["status"] == "stored"

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# 5. Session Distillation Tool
# ---------------------------------------------------------------------------

def test_mcp_distill_session():
    async def _test():
        args = {
            "repository_id": "repo-core-api",
            "prompt": "How does authentication work with JWT?",
            "response_summary": "JWT authentication uses RS256 asymmetric keys.",
            "decisions": ["Verify exp and iss claims on every request."],
            "affected_files": ["backend/auth.py"],
        }
        raw_res = await mcp_server.call_tool("synapse_distill_session", args)
        data = _extract_tool_result(raw_res)

        assert data.get("success") is True
        assert data["status"] == "recorded"
        assert data["distilled_decisions_count"] >= 1

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# 6. Session Management Tool
# ---------------------------------------------------------------------------

def test_mcp_manage_session_lifecycle():
    async def _test():
        # 1. Create
        create_args = {
            "action": "create",
            "repository_id": "repo-core-api",
            "title": "MCP Session Test",
        }
        create_res = _extract_tool_result(await mcp_server.call_tool("synapse_manage_session", create_args))
        assert create_res.get("success") is True
        session_id = create_res["session_id"]
        assert session_id.startswith("sess_")

        # 2. Resume / Inspect
        resume_args = {
            "action": "resume",
            "repository_id": "repo-core-api",
            "session_id": session_id,
        }
        resume_res = _extract_tool_result(await mcp_server.call_tool("synapse_manage_session", resume_args))
        assert resume_res.get("success") is True
        assert resume_res["status"] == "resumed"

        # 3. Close
        close_args = {
            "action": "close",
            "repository_id": "repo-core-api",
            "session_id": session_id,
            "summary": "Completed session test.",
        }
        close_res = _extract_tool_result(await mcp_server.call_tool("synapse_manage_session", close_args))
        assert close_res.get("success") is True
        assert close_res["status"] == "closed"

    asyncio.run(_test())


def test_mcp_manage_session_invalid_action():
    async def _test():
        args = {
            "action": "invalid_action_unknown",
            "repository_id": "repo-core-api",
        }
        res = _extract_tool_result(await mcp_server.call_tool("synapse_manage_session", args))
        assert res.get("success") is False
        assert res["error"]["code"] == "MALFORMED_INPUT"

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# 7. Secret Redaction Through MCP
# ---------------------------------------------------------------------------

def test_mcp_secret_redaction():
    async def _test():
        args = {
            "repository_id": "repo-core-api",
            "query": "Connect to model with sk-live1234567890abcdef12345678",
        }
        res = _extract_tool_result(await mcp_server.call_tool("synapse_query_context", args))
        assert res.get("success") is True
        ctx_text = res.get("formatted_context", "")
        assert "sk-" not in ctx_text
        assert "[REDACTED_API_KEY]" in ctx_text

    asyncio.run(_test())
