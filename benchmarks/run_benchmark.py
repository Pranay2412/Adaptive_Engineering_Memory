r"""Executable runner for TEST CASE 1: Graphify vs Synapse Token Usage Benchmark.

Executes the benchmark against the real C:\FYP repository across the 5 target query categories,
measuring token consumption, latencies, and information parity, then exports JSON, CSV, and Markdown reports.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Ensure backend and benchmarks are importable
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

load_dotenv()

from backend.hybrid_retrieval import HybridRetrievalService
from backend.ingestion_code import parse_graphify_graph
from backend.models import CodeSymbol, DocumentEntity, Memory
from backend.orchestrator import AdaptiveContextOrchestrator
from backend.semantic_matcher import MockEmbeddingProvider
from backend.team_memory_store import TeamMemoryDocumentStore
from benchmarks.token_benchmark import (
    GraphifyWorkflowRunner,
    MockBenchmarkLLM,
    OpenAIBenchmarkLLM,
    SynapseWorkflowRunner,
    TokenBenchmarkHarness,
    calculate_aggregates,
    export_csv,
    export_json,
    export_markdown_report,
)

TARGET_QUERIES = [
    ("What modules import models.py?", "structural"),
    ("How does authentication work with JWT?", "implementation"),
    ("Which components call the authentication service?", "dependency"),
    ("Where could JWT verification fail and what code is involved?", "debugging"),
    ("Why was RS256 chosen for authentication?", "architecture"),
]


def load_repository_context(repo_path: Path) -> tuple[list[CodeSymbol], list[DocumentEntity], list[Memory]]:
    """Load real symbols, extracted doc entities, and team memories from the repository."""
    graph_json = repo_path / "graphify-out" / "graph.json"
    symbols: list[CodeSymbol] = []
    if graph_json.exists():
        try:
            symbols, _ = parse_graphify_graph(graph_json, user_id="benchmark_user", repository_id="FYP")
            print(f"--> Loaded {len(symbols)} real code symbols from graph.json")
        except Exception as e:
            print(f"--> Warning loading graph.json: {e}")

    # Load documentation entities from docs/
    entities: list[DocumentEntity] = []
    docs_dir = repo_path / "docs"
    if docs_dir.exists():
        for doc_file in docs_dir.glob("*.md"):
            try:
                content = doc_file.read_text(encoding="utf-8")
                # Extract main headings as DocumentEntity
                for line in content.splitlines():
                    if line.startswith("## ") or line.startswith("### "):
                        title = line.lstrip("#").strip()
                        if len(title) > 3:
                            entities.append(
                                DocumentEntity(
                                    id=f"doc-{doc_file.stem}-{title[:15].lower().replace(' ', '_')}",
                                    name=title,
                                    type="architecture_spec",
                                    source_document=f"docs/{doc_file.name}",
                                    description=f"Specification and design guidelines for {title} in {doc_file.name}.",
                                )
                            )
            except Exception:
                pass
    print(f"--> Extracted {len(entities)} architectural document concepts from docs/")

    # Load representative team memories
    memories = [
        Memory(
            id="mem-auth-rs256",
            title="Architecture Decision: Sign JWT tokens with asymmetric RS256",
            content="Chose RS256 with private key rotation over HS256 to eliminate shared symmetric secret vulnerabilities across services.",
            category="architecture_decision",
            scope="repository",
            author_id="dev-security",
            impact_areas=["backend/session_memory.py", "backend/models.py"],
            actionable_takeaways=["Store RS256 private key securely; expose public JWKS with 1h TTL."],
        ),
        Memory(
            id="mem-token-opt-knapsack",
            title="Important Refactor: Utility-density knapsack token optimization",
            content="Migrated from naive greedy cutoffs to utility-density knapsack optimization to preserve code provenance.",
            category="important_refactor",
            scope="repository",
            author_id="dev-architecture",
            impact_areas=["backend/token_optimizer.py"],
            actionable_takeaways=["Never drop source file citations during compaction."],
        ),
        Memory(
            id="mem-debugging-jwt",
            title="Bug Root Cause: JWT signature verification failure on clock skew",
            content="Token verification failed in staging due to 60s clock skew. Added leeway parameter to jwt.decode.",
            category="bug_root_cause",
            scope="repository",
            author_id="dev-ops",
            impact_areas=["backend/session_memory.py"],
            actionable_takeaways=["Always configure leeway=60 in JWT validators."],
        ),
    ]
    print(f"--> Loaded {len(memories)} representative team engineering memories")

    return symbols, entities, memories


def main():
    parser = argparse.ArgumentParser(description="Run Graphify vs Synapse Token Benchmark")
    parser.add_argument("--budget", type=int, default=1500, help="Context token budget (default: 1500)")
    parser.add_argument("--use-mock-llm", action="store_true", help="Force mock LLM instead of real OpenAI API")
    parser.add_argument("--repetitions", type=int, default=1, help="Number of repetitions per query")
    args = parser.parse_args()

    print("================================================================================")
    print("TEST CASE 1: Graphify vs Synapse Token Usage Benchmark")
    print("================================================================================")
    print(f"Repository Root: {repo_root}")
    print(f"Context Budget:  {args.budget} tokens")

    # Select LLM Provider
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    if not args.use_mock_llm and api_key:
        print("--> Using Live OpenAI API (gpt-4o-mini, temperature=0.0)")
        llm_client = OpenAIBenchmarkLLM(api_key=api_key, model="gpt-4o-mini", temperature=0.0)
    else:
        print("--> Using Deterministic Mock Benchmark LLM")
        llm_client = MockBenchmarkLLM()

    # Load Repository Assets
    symbols, entities, memories = load_repository_context(repo_root)

    # Initialize Runners
    graph_path = repo_root / "graphify-out" / "graph.json"
    graphify_runner = GraphifyWorkflowRunner(graph_path=graph_path, llm_client=llm_client)

    # Synapse Runner uses MockEmbeddingProvider to avoid external rate limits during retrieval
    mock_embed = MockEmbeddingProvider()
    hybrid_service = HybridRetrievalService(embedding_provider=mock_embed)
    orchestrator = AdaptiveContextOrchestrator(retrieval_service=hybrid_service)
    synapse_runner = SynapseWorkflowRunner(orchestrator=orchestrator, llm_client=llm_client)

    harness = TokenBenchmarkHarness(
        graphify_runner=graphify_runner,
        synapse_runner=synapse_runner,
    )

    print(f"\n--> Executing benchmark across {len(TARGET_QUERIES)} target queries...")
    reports = harness.run_suite(
        test_queries=TARGET_QUERIES,
        context_budget=args.budget,
        user_id="benchmark_engineer",
        repository_id="FYP",
        symbols=symbols,
        entities=entities,
        memories=memories,
        repo_root=repo_root,
        repetitions=args.repetitions,
    )

    # Calculate Aggregates
    agg = calculate_aggregates(reports)

    # Print Summary Table
    print("\n" + "=" * 95)
    print(f"{'Query':<40} | {'Graphify In':<12} | {'Synapse In':<12} | {'Reduction %':<12} | {'Total Red %':<12}")
    print("-" * 95)
    for r in reports:
        g_in = r.graphify_metrics.provider_prompt_tokens or r.graphify_metrics.input_tokens
        s_in = r.synapse_metrics.provider_prompt_tokens or r.synapse_metrics.input_tokens
        q_label = r.query if len(r.query) <= 38 else r.query[:35] + "..."
        print(f"{q_label:<40} | {g_in:<12} | {s_in:<12} | {r.percentage_input_token_reduction:<11}% | {r.percentage_total_token_reduction:<11}%")
    print("=" * 95)
    print(f"AVERAGE INPUT TOKEN REDUCTION:   {agg.get('percentage_input_token_reduction', 0)}%")
    print(f"AVERAGE CONTEXT TOKEN REDUCTION: {agg.get('percentage_context_token_reduction', 0)}%")
    print(f"AVERAGE ABSOLUTE TOKENS SAVED:   {agg.get('average_input_token_reduction', 0)} tokens / query")
    print(f"AVERAGE GRAPHIFY LATENCY:        {agg.get('average_graphify_latency_ms', 0)} ms")
    print(f"AVERAGE SYNAPSE LATENCY:         {agg.get('average_synapse_latency_ms', 0)} ms")
    print("=" * 95)

    # Export Results
    json_path = repo_root / "benchmarks" / "results" / "benchmark_results.json"
    csv_path = repo_root / "benchmarks" / "results" / "benchmark_results.csv"
    md_path = repo_root / "docs" / "experiments" / "test_case_1_token_benchmark.md"

    export_json(reports, json_path)
    export_csv(reports, csv_path)
    export_markdown_report(reports, md_path)

    print(f"\n--> Successfully exported raw JSON to: {json_path}")
    print(f"--> Successfully exported CSV to:      {csv_path}")
    print(f"--> Successfully exported Markdown to: {md_path}")


if __name__ == "__main__":
    main()
