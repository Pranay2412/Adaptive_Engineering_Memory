"""End-to-End Sample Script Demonstrating Synapse Python SDK (Phase 11).

Demonstrates:
1. Connecting to Synapse via SynapseClient
2. Checking health and readiness probes
3. Creating a development session
4. Storing architectural decisions and technical recipes
5. Querying token-budgeted context via ACO
6. Distilling session interaction into durable memory cards
7. Scoped cross-repository memory retrieval
8. Orderly session closure

Can be executed standalone against an in-memory ASGI test application:
    python examples/sdk_usage_example.py
Or against a running live service:
    python examples/sdk_usage_example.py --live http://localhost:8000
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any

# Ensure repository root is on sys.path for direct execution
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from synapse_sdk import (
    ClientConfig,
    SynapseClient,
    SynapseError,
)


def run_demo(base_url: str = "http://localhost:8000", transport: Any = None) -> None:
    print("=" * 70)
    print("Synapse Python SDK - Operational Demonstration (Phase 11)")
    print("=" * 70)

    config = ClientConfig(
        base_url=base_url,
        timeout=10.0,
        max_retries=2,
        default_principal_id="alice-engineer",
        default_tenant_id="engineering-org",
        default_allowed_repositories=["repo-payment-service"],
        default_roles=["developer"],
    )

    with SynapseClient(config=config, transport=transport) as client:
        # 1. Probes
        print("\n[1] Verifying System Probes...")
        health = client.check_health()
        print(f" -> Liveness: status={health.status}, version={health.version}, mode={health.mode}")

        readiness = client.check_readiness()
        print(f" -> Readiness: status={readiness.status}, checks={readiness.checks}")

        # 2. Session Lifecycle
        print("\n[2] Creating Engineering Session...")
        session = client.create_session(
            repository_id="repo-payment-service",
            title="Implement Idempotency Guard in Stripe Webhooks",
            client_type="vscode",
            assistant_name="synapse-copilot",
            metadata={"ticket_id": "PAY-1042"},
        )
        print(f" -> Created session: ID={session.session_id}, status={session.status}")

        # 3. Store Architectural Decision Record (ADR)
        print("\n[3] Storing Architectural Decision...")
        decision = client.store_decision(
            session_id=session.session_id,
            repository_id="repo-payment-service",
            title="Idempotency-Key Header Required on Webhook Callbacks",
            rationale="Stripe webhooks may be delivered multiple times under network partitions; keys prevent double debit.",
            scope="repository",
            category="architecture",
            impact_areas=["payments", "stripe", "webhooks"],
            affected_symbols=["process_webhook_event"],
        )
        print(f" -> Decision recorded: ID={decision.decision_id}, title='{decision.title}'")

        # 4. Store Technical Recipe / Fact
        print("\n[4] Storing Technical Recipe...")
        fact = client.store_fact(
            session_id=session.session_id,
            repository_id="repo-payment-service",
            title="Postgres Unique Constraint on Idempotency Keys",
            content="Use INSERT INTO payment_idempotency_keys ... ON CONFLICT DO NOTHING with status checking.",
            category="implementation_summary",
            scope="repository",
            actionable_takeaways=["Always wrap key insertion in database transaction"],
        )
        print(f" -> Fact recorded: ID={fact.fact_id}, title='{fact.title}'")

        # 5. Query Budgeted Context via ACO
        print("\n[5] Querying Orchestrated Context via ACO...")
        context = client.query_context(
            session_id=session.session_id,
            repository_id="repo-payment-service",
            query="How should payment webhooks handle duplicate deliveries?",
            max_tokens=1000,
        )
        print(f" -> Context query returned {len(context.items)} items.")
        print(f" -> Tokens used: {context.tokens_used}/{context.budget_allocated} ({context.budget_utilization * 100:.1f}% budget utilization)")
        for idx, item in enumerate(context.items, 1):
            print(f"    [{idx}] ({item.type}) score={item.score:.2f}: {item.content[:80]}...")

        # 6. Distill Interaction into Long-Term Memory
        print("\n[6] Distilling Interaction into Memory Cards...")
        distill = client.distill_session(
            session_id=session.session_id,
            repository_id="repo-payment-service",
            prompt="How should payment webhooks handle duplicate deliveries?",
            response_summary="Implemented idempotency keys in payment_idempotency_keys table with unique constraints.",
            affected_files=["services/payment_webhook.py"],
            engineering_decisions=["Require X-Idempotency-Key on all payment mutations"],
        )
        print(f" -> Distillation completed: status={distill.status}, memories created={distill.distilled_memories_count}")

        # 7. Scoped Memory Query
        print("\n[7] Querying Scoped Memories...")
        query_res = client.query_memories(
            repository_id="repo-payment-service",
            query="idempotency webhooks payment",
            limit=5,
        )
        print(f" -> Found {query_res.total_found} memories in scopes: {query_res.scopes_included}")
        for mem in query_res.memories:
            print(f"    - [{mem.scope}:{mem.category}] {mem.title} (score={mem.relevance_score:.2f})")

        # 8. Close Session
        print("\n[8] Closing Session...")
        closed_session = client.close_session(
            session_id=session.session_id,
            reason="completed",
            summary="Successfully integrated and verified payment webhook idempotency handling.",
        )
        print(f" -> Session closed: ID={closed_session.session_id}, status={closed_session.status}")

    print("\n" + "=" * 70)
    print("SUCCESS: Full Synapse Python SDK workflow completed successfully.")
    print("=" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(description="Synapse Python SDK Demo")
    parser.add_argument("--live", default=None, help="Live service URL (e.g. http://localhost:8000)")
    args = parser.parse_args()

    if args.live:
        run_demo(base_url=args.live)
    else:
        # Default: run against in-process FastAPI application via TestClient transport
        from starlette.testclient import TestClient
        from backend.api import create_app
        from backend.config import SynapseConfig

        print("No live server specified; launching demonstration against in-process FastAPI application...")
        test_app = create_app(SynapseConfig())
        tc = TestClient(test_app)
        run_demo(base_url="http://testserver", transport=tc._transport)


if __name__ == "__main__":
    main()
