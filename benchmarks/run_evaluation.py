r"""CLI Executable Runner for the Repeatable Synapse Evaluation Framework.

Phase 13: Provides a command-line interface to execute, resume, and export
benchmarks across versioned query sets and multiple configurations.

Usage Examples:
    # Deterministic offline run (zero external API keys needed):
    python -m benchmarks.run_evaluation --mock-llm --budget 1500 --repetitions 3

    # Resuming an interrupted run:
    python -m benchmarks.run_evaluation --resume --output-dir benchmarks/results/eval_v1

    # Dry-run validation of dataset and runner wiring:
    python -m benchmarks.run_evaluation --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Ensure repo root is on sys.path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

load_dotenv()

from backend.hybrid_retrieval import HybridRetrievalService
from backend.orchestrator import AdaptiveContextOrchestrator
from backend.semantic_matcher import MockEmbeddingProvider
from benchmarks.eval_framework import (
    EvaluationAggregator,
    EvaluationConfig,
    EvaluationHarness,
    EvaluationJudge,
    GraphifyBaselineRunner,
    QueryDataset,
    SynapseAcoRunner,
    TrialRecord,
    get_git_commit_hash,
)
from benchmarks.run_benchmark import load_repository_context
from benchmarks.token_benchmark import (
    BaseBenchmarkLLM,
    MockBenchmarkLLM,
    OpenAIBenchmarkLLM,
)


def build_evaluation_runners(
    repo_path: Path,
    llm_client: BaseBenchmarkLLM,
) -> dict[str, Any]:
    """Instantiate and wire up the standard suite of evaluation runners."""
    # 1. Graphify Baseline Runner
    graph_path = repo_path / "graphify-out" / "graph.json"
    graphify_runner = GraphifyBaselineRunner(graph_path=graph_path)

    # 2. Synapse ACO Runners (Deterministic & Uncompressed)
    mock_embed = MockEmbeddingProvider()
    hybrid_service = HybridRetrievalService(embedding_provider=mock_embed)
    orchestrator = AdaptiveContextOrchestrator(retrieval_service=hybrid_service)

    # Pre-load repository context
    symbols, entities, memories = load_repository_context(repo_path)

    synapse_det_runner = SynapseAcoRunner(
        name="synapse_deterministic",
        orchestrator=orchestrator,
        optimization_mode="deterministic",
        repository_id="FYP",
        symbols=symbols,
        entities=entities,
        memories=memories,
    )

    synapse_none_runner = SynapseAcoRunner(
        name="synapse_none",
        orchestrator=orchestrator,
        optimization_mode="none",
        repository_id="FYP",
        symbols=symbols,
        entities=entities,
        memories=memories,
    )

    return {
        "graphify_baseline": graphify_runner,
        "synapse_deterministic": synapse_det_runner,
        "synapse_none": synapse_none_runner,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Repeatable Synapse Evaluation Framework Runner (Phase 13)")
    parser.add_argument(
        "--dataset",
        default="benchmarks/datasets/queries_v1.json",
        help="Path to versioned query dataset JSON (default: benchmarks/datasets/queries_v1.json)",
    )
    parser.add_argument(
        "--configurations",
        nargs="+",
        default=["graphify_baseline", "synapse_deterministic", "synapse_none"],
        help="List of configuration runners to evaluate",
    )
    parser.add_argument("--budget", type=int, default=1500, help="Context token budget limit (default: 1500)")
    parser.add_argument("--repetitions", type=int, default=3, help="Repetitions per query per configuration (default: 3)")
    parser.add_argument("--mock-llm", action="store_true", help="Force deterministic MockBenchmarkLLM (offline mode)")
    parser.add_argument("--model", default="gpt-4o-mini", help="OpenAI model name if using live API (default: gpt-4o-mini)")
    parser.add_argument("--output-dir", default="benchmarks/results/eval_v1", help="Directory to save raw and summary artifacts")
    parser.add_argument("--prefix", default="eval_v1", help="Filename prefix for exported reports")
    parser.add_argument("--resume", action="store_true", help="Resume from existing checkpoint if available")
    parser.add_argument("--dry-run", action="store_true", help="Validate dataset and configuration wiring without executing")
    args = parser.parse_args()

    print("================================================================================")
    print("SYNAPSE REPEATABLE EVALUATION FRAMEWORK (Phase 13)")
    print("================================================================================")
    print(f"Repository Root:    {repo_root}")
    print(f"Dataset Path:       {args.dataset}")
    print(f"Configurations:     {args.configurations}")
    print(f"Context Budget:     {args.budget} tokens")
    print(f"Repetitions:        {args.repetitions}")
    print(f"Git Commit:         {get_git_commit_hash(repo_root)}")
    print(f"Output Directory:   {args.output_dir}")

    # 1. Load Versioned Dataset
    ds_path = repo_root / args.dataset if not Path(args.dataset).is_absolute() else Path(args.dataset)
    dataset = QueryDataset(ds_path)
    print(f"--> Loaded query dataset: '{dataset.name}' v{dataset.version} ({len(dataset)} queries)")

    if args.dry_run:
        print("\n[DRY RUN] Dataset queries:")
        for q in dataset:
            print(f"  [{q.query_id}] ({q.category}) {q.query}")
        print("\n[DRY RUN] Verification successful. Exiting without execution.")
        return

    # 2. Select LLM Provider
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    if not args.mock_llm and api_key:
        print(f"--> Using Live OpenAI API ({args.model}, temperature=0.0)")
        llm_client: BaseBenchmarkLLM = OpenAIBenchmarkLLM(api_key=api_key, model=args.model, temperature=0.0)
    else:
        print("--> Using Deterministic Mock Benchmark LLM (Zero-cost, offline mode)")
        llm_client = MockBenchmarkLLM()

    # 3. Build Runners
    runners = build_evaluation_runners(repo_root, llm_client)

    # 4. Initialize Evaluation Config & Harness
    config = EvaluationConfig(
        run_id=f"eval_{int(time.time())}",
        configurations=args.configurations,
        context_budget=args.budget,
        repetitions=args.repetitions,
        deterministic=args.mock_llm or (not api_key),
        llm_model=getattr(llm_client, "model", "mock"),
        output_dir=str(repo_root / args.output_dir if not Path(args.output_dir).is_absolute() else Path(args.output_dir)),
        resume=args.resume,
    )

    judge = EvaluationJudge(llm_client=llm_client)
    harness = EvaluationHarness(
        config=config,
        dataset=dataset,
        runners=runners,
        llm_client=llm_client,
        judge=judge,
    )

    # 5. Execute Evaluation Suite
    total_expected = len(args.configurations) * len(dataset) * args.repetitions
    print(f"\n--> Starting evaluation harness: {total_expected} total trials expected...")

    completed_count = 0

    def progress_callback(rec: TrialRecord) -> None:
        nonlocal completed_count
        completed_count += 1
        print(
            f"  [{completed_count:02d}/{total_expected:02d}] "
            f"config={rec.configuration_name:<22} "
            f"query={rec.query_id} "
            f"rep={rec.repetition_index} "
            f"tokens={rec.retrieval.context_tokens_precise:4d} "
            f"quality={rec.quality.overall_score:.1f}/5.0 "
            f"latency={rec.total_latency_ms:.0f}ms"
        )

    records = harness.run(progress_callback=progress_callback)

    # 6. Aggregate Metrics & Verify Integrity
    aggregator = EvaluationAggregator(records)
    summary = aggregator.compute_summary()
    integrity = summary.get("integrity", {})

    print("\n" + "=" * 105)
    print("EVALUATION RESULTS SUMMARY")
    print("=" * 105)
    print(f"{'Configuration':<24} | {'Mean Ctx Tokens':<15} | {'Mean Tot Tokens':<15} | {'Ctx Reduction':<14} | {'Evidence F1':<12} | {'Quality':<10}")
    print("-" * 105)

    configs = summary.get("configurations", {})
    for cname, cdata in configs.items():
        if "retrieval" not in cdata:
            continue
        ret = cdata["retrieval"]
        qual = cdata["answer_quality"]
        comp = cdata.get("comparison_vs_baseline", {})
        red_str = f"{comp.get('context_token_reduction_pct', 0.0)}%" if comp else "Baseline"
        print(
            f"{cname:<24} | "
            f"{ret['mean_context_tokens']:<15.1f} | "
            f"{qual['mean_total_tokens']:<15.1f} | "
            f"{red_str:<14} | "
            f"{ret['mean_evidence_f1']:<12.3f} | "
            f"{qual['mean_overall_score']:.2f} / 5.0"
        )
    print("=" * 105)
    print(f"Data Integrity: Valid={integrity.get('valid')} (Duplicates={integrity.get('duplicates_found')}, Failures={integrity.get('failure_count')})")

    # 7. Export Artifacts
    out_dir = Path(config.output_dir)
    json_path = out_dir / f"{args.prefix}_results.json"
    csv_path = out_dir / f"{args.prefix}_results.csv"
    md_path = repo_root / "docs" / "experiments" / f"{args.prefix}_report.md"

    aggregator.export_json(json_path)
    aggregator.export_csv(csv_path)
    aggregator.export_markdown_report(md_path)

    print(f"\n--> Successfully exported raw & aggregate JSON to: {json_path}")
    print(f"--> Successfully exported per-trial CSV to:       {csv_path}")
    print(f"--> Successfully exported Markdown report to:     {md_path}")


if __name__ == "__main__":
    main()
