r"""Executable runner for TEST CASE 2: ACO Token Budget Evaluation.

Runs the complete Synapse workflow across:
- Budgets: 500, 1000, 1500, 2500, 4000, unbounded (None)
- Modes: deterministic (all budgets), none (all budgets), llm (constrained budgets)
- Queries: 5 standard engineering queries across 5 target categories

Produces:
- benchmarks/results/budget_benchmark_results.json
- benchmarks/results/budget_benchmark_results.csv
- docs/experiments/test_case_2_budget_benchmark.md
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Ensure repo root is importable
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

load_dotenv()

from backend.ingestion_code import parse_graphify_graph
from backend.models import CodeSymbol, DocumentEntity, Memory, OptimizationMode
from backend.token_optimizer import MockLLMCompressor, OpenAILLMCompressor
from benchmarks.budget_benchmark import (
    QUERY_GROUND_TRUTH,
    AnswerQualityJudge,
    BudgetExperimentRunner,
    BudgetTrialMetric,
    compute_budget_aggregates,
    export_budget_csv,
    export_budget_json,
    export_budget_markdown,
)
from benchmarks.run_benchmark import TARGET_QUERIES, load_repository_context
from benchmarks.token_benchmark import (
    BaseBenchmarkLLM,
    MockBenchmarkLLM,
    OpenAIBenchmarkLLM,
)

BUDGET_CONFIGURATIONS: list[int | None] = [
    500,
    1000,
    1500,
    2500,
    4000,
    None,  # Unbounded
]


def get_git_commit_hash() -> str:
    """Retrieve current repository git commit SHA."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception:
        return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description="TEST CASE 2: ACO Token Budget Evaluation")
    parser.add_argument("--mock-llm", action="store_true", help="Use mock LLM instead of live OpenAI API")
    parser.add_argument("--quick", action="store_true", help="Run a quick test with subset of budgets")
    parser.add_argument("--skip-llm-compression", action="store_true", help="Skip LLM compression mode")
    args = parser.parse_args()

    print("================================================================================")
    print("TEST CASE 2: Synapse ACO Token Budget Evaluation Benchmark")
    print("================================================================================")

    # 1. Initialize LLM Client
    api_key = os.getenv("OPENAI_API_KEY")
    if args.mock_llm or not api_key:
        print("--> Using MockBenchmarkLLM (offline mode)")
        llm_client: BaseBenchmarkLLM = MockBenchmarkLLM()
        compressor = MockLLMCompressor()
        judge_model = "mock-judge"
    else:
        print("--> Using live OpenAI gpt-4o-mini (live API mode)")
        llm_client = OpenAIBenchmarkLLM(api_key=api_key, model="gpt-4o-mini", temperature=0.0)
        compressor = OpenAILLMCompressor(api_key=api_key, model="gpt-4o-mini")
        judge_model = "gpt-4o-mini"

    # 2. Load Repository Context
    symbols, entities, memories = load_repository_context(repo_root)
    print(f"--> Loaded {len(symbols)} code symbols, {len(entities)} doc entities, {len(memories)} memories")

    # 3. Setup Runner
    runner = BudgetExperimentRunner(
        llm_client=llm_client,
        llm_compressor=compressor,
    )

    budgets = [500, 1500, None] if args.quick else BUDGET_CONFIGURATIONS

    # Plan experiment trials
    # Deterministic mode across all budgets
    # None mode across all budgets
    # LLM compression mode across bounded budgets (500, 1000, 1500) if enabled
    trials: list[BudgetTrialMetric] = []
    total_planned = (
        len(TARGET_QUERIES) * len(budgets) * 2  # deterministic + none
        + (0 if args.skip_llm_compression else len(TARGET_QUERIES) * min(3, len([b for b in budgets if b is not None])))
    )

    print(f"--> Beginning execution of {total_planned} experimental trials across {len(budgets)} budget tiers...")
    start_time = time.perf_counter()
    trial_idx = 0

    # 1. Deterministic Token Optimization
    for budget in budgets:
        b_label = str(budget) if budget is not None else "unbounded"
        print(f"\n--- Budget Tier: {b_label} tokens (Mode: DETERMINISTIC) ---")
        for query, cat in TARGET_QUERIES:
            trial_idx += 1
            sys.stdout.write(f"[{trial_idx}/{total_planned}] ({cat:14}) {query[:40]}... ")
            sys.stdout.flush()

            metric = runner.run_trial(
                query=query,
                category=cat,
                budget=budget,
                mode=OptimizationMode.DETERMINISTIC,
                symbols=symbols,
                entities=entities,
                memories=memories,
            )
            trials.append(metric)
            util_str = f"{metric.budget_utilization_pct}%" if metric.budget_utilization_pct is not None else "N/A"
            print(f"-> Ctx: {metric.final_context_tokens} tok | In: {metric.actual_provider_prompt_tokens} tok | Util: {util_str} | Qual: {metric.quality_score.overall_score}/5.0")

    # 2. None / Raw Greedy Packing
    for budget in budgets:
        b_label = str(budget) if budget is not None else "unbounded"
        print(f"\n--- Budget Tier: {b_label} tokens (Mode: NONE / RAW) ---")
        for query, cat in TARGET_QUERIES:
            trial_idx += 1
            sys.stdout.write(f"[{trial_idx}/{total_planned}] ({cat:14}) {query[:40]}... ")
            sys.stdout.flush()

            metric = runner.run_trial(
                query=query,
                category=cat,
                budget=budget,
                mode=OptimizationMode.NONE,
                symbols=symbols,
                entities=entities,
                memories=memories,
            )
            trials.append(metric)
            util_str = f"{metric.budget_utilization_pct}%" if metric.budget_utilization_pct is not None else "N/A"
            print(f"-> Ctx: {metric.final_context_tokens} tok | In: {metric.actual_provider_prompt_tokens} tok | Util: {util_str} | Qual: {metric.quality_score.overall_score}/5.0")

    # 3. LLM Structured Compression (for constrained budgets: 500, 1000, 1500)
    if not args.skip_llm_compression:
        llm_budgets = [b for b in budgets if b is not None and b in (500, 1000, 1500)]
        for budget in llm_budgets:
            print(f"\n--- Budget Tier: {budget} tokens (Mode: LLM COMPRESSION) ---")
            for query, cat in TARGET_QUERIES:
                trial_idx += 1
                sys.stdout.write(f"[{trial_idx}/{total_planned}] ({cat:14}) {query[:40]}... ")
                sys.stdout.flush()

                metric = runner.run_trial(
                    query=query,
                    category=cat,
                    budget=budget,
                    mode=OptimizationMode.LLM,
                    symbols=symbols,
                    entities=entities,
                    memories=memories,
                )
                trials.append(metric)
                util_str = f"{metric.budget_utilization_pct}%" if metric.budget_utilization_pct is not None else "N/A"
                print(f"-> Ctx: {metric.final_context_tokens} tok | In: {metric.actual_provider_prompt_tokens} tok | Util: {util_str} | Qual: {metric.quality_score.overall_score}/5.0")

    elapsed_s = time.perf_counter() - start_time
    print(f"\n--> Successfully completed {len(trials)} trials in {elapsed_s:.1f} seconds.")

    # 4. Compute Statistical Aggregates
    aggregates = compute_budget_aggregates(trials)
    git_commit = get_git_commit_hash()
    timestamp_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    full_report: dict[str, Any] = {
        "timestamp": timestamp_iso,
        "metadata": {
            "repository": str(repo_root),
            "commit_sha": git_commit,
            "synapse_version": "2.0.0",
            "model": "gpt-4o-mini" if not args.mock_llm else "mock-llm",
            "judge_model": judge_model,
            "tokenizer": "tiktoken (cl100k_base)",
            "temperature": 0.0,
            "queries": [q[0] for q in TARGET_QUERIES],
            "budgets": [str(b) if b is not None else "unbounded" for b in budgets],
            "modes_evaluated": ["deterministic", "none"] + ([] if args.skip_llm_compression else ["llm"]),
        },
        "aggregates": aggregates,
        "trials": [t.to_dict() for t in trials],
    }

    # 5. Export Results
    json_path = repo_root / "benchmarks" / "results" / "budget_benchmark_results.json"
    csv_path = repo_root / "benchmarks" / "results" / "budget_benchmark_results.csv"
    md_path = repo_root / "docs" / "experiments" / "test_case_2_budget_benchmark.md"

    export_budget_json(full_report, json_path)
    print(f"--> Exported JSON: {json_path}")

    export_budget_csv(trials, csv_path)
    print(f"--> Exported CSV: {csv_path}")

    export_budget_markdown(full_report, md_path)
    print(f"--> Exported Markdown: {md_path}")

    print("\n================================================================================")
    print("SUMMARY BY BUDGET CONFIGURATION:")
    print("================================================================================")
    print(f"{'Budget':<12} | {'Context Tok':<12} | {'Prompt Tok':<12} | {'Total Tok':<12} | {'Util %':<10} | {'Latency':<12} | {'Quality':<10}")
    print("-" * 88)
    for s in aggregates["by_budget"]:
        print(f"{str(s['budget']):<12} | {s['avg_context_tokens']:<12.1f} | {s['avg_input_prompt_tokens']:<12.1f} | {s['avg_total_tokens']:<12.1f} | {str(s['avg_budget_utilization_pct']):<10} | {s['avg_latency_ms']:<12.1f} | {s['avg_quality_score']:<10.2f}")


if __name__ == "__main__":
    main()
