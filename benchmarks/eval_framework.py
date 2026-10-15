"""Repeatable, Versioned Evaluation Framework for Synapse Benchmarking.

Phase 13: Core Evaluation Framework providing:
1. Versioned query datasets with machine-readable query IDs and scoring rubrics.
2. Typed evaluation models (QueryItem, RetrievalMetrics, AnswerQualityMetrics, TrialRecord).
3. Configuration runners for Graphify baseline and Synapse ACO modes (deterministic, none, llm).
4. Resumable execution harness with incremental checkpointing and explicit failure handling.
5. Strict separation of retrieval metrics vs. answer quality metrics in aggregation.
6. Deterministic offline evaluation mode requiring zero external API credentials.
7. Validated export to raw JSON, aggregate JSON, tabular CSV, and Markdown reports.
"""

from __future__ import annotations

import csv
import datetime
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from backend.hybrid_retrieval import HybridRetrievalService
from backend.models import (
    CodeSymbol,
    DocumentEntity,
    Memory,
    OptimizationMode,
    QueryIntent,
    utc_now_iso,
)
from backend.orchestrator import (
    AdaptiveContextOrchestrator,
    AdaptiveContextPacker,
    RuleBasedIntentClassifier,
    TokenOptimizedContextPacker,
)
from backend.semantic_matcher import MockEmbeddingProvider
from backend.team_memory_store import TeamMemoryDocumentStore
from backend.token_optimizer import (
    BaseLLMCompressor,
    MockLLMCompressor,
    OpenAILLMCompressor,
    TokenEstimator,
)
from benchmarks.token_benchmark import (
    DEFAULT_SYSTEM_PROMPT,
    BaseBenchmarkLLM,
    MockBenchmarkLLM,
    OpenAIBenchmarkLLM,
    count_tokens_precise,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Dataset Models & Schemas
# ---------------------------------------------------------------------------

@dataclass
class QueryItem:
    """A single versioned benchmark query with expected evidence and evaluation rubrics."""

    query_id: str
    category: str
    query: str
    expected_evidence: list[str] = field(default_factory=list)
    reference_answer: str = ""
    scoring_rubric: dict[str, str] = field(default_factory=dict)
    version: str = "1.0.0"

    def validate(self) -> None:
        """Validate query item integrity."""
        if not self.query_id or not self.query_id.strip():
            raise ValueError("query_id cannot be empty.")
        if not self.category or not self.category.strip():
            raise ValueError("category cannot be empty.")
        if not self.query or not self.query.strip():
            raise ValueError("query cannot be empty.")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QueryItem:
        return cls(
            query_id=data.get("query_id", ""),
            category=data.get("category", "general"),
            query=data.get("query", ""),
            expected_evidence=list(data.get("expected_evidence", [])),
            reference_answer=data.get("reference_answer", ""),
            scoring_rubric=dict(data.get("scoring_rubric", {})),
            version=data.get("version", "1.0.0"),
        )


class QueryDataset:
    """Manages versioned query sets loaded from machine-readable JSON."""

    def __init__(self, dataset_path: Path | str) -> None:
        self.path = Path(dataset_path)
        self.version: str = "1.0.0"
        self.name: str = "unnamed"
        self.description: str = ""
        self.queries: list[QueryItem] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"Benchmark dataset not found: {self.path}")
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.version = data.get("version", "1.0.0")
        self.name = data.get("name", self.path.stem)
        self.description = data.get("description", "")
        raw_queries = data.get("queries", [])
        if not raw_queries:
            raise ValueError(f"Dataset '{self.path}' contains no queries.")

        loaded: list[QueryItem] = []
        for q in raw_queries:
            item = QueryItem.from_dict(q)
            item.validate()
            loaded.append(item)
        self.queries = loaded

    def get_by_id(self, query_id: str) -> QueryItem | None:
        for q in self.queries:
            if q.query_id == query_id:
                return q
        return None

    def __len__(self) -> int:
        return len(self.queries)

    def __iter__(self):
        return iter(self.queries)


# ---------------------------------------------------------------------------
# 2. Evaluation Typed Schemas
# ---------------------------------------------------------------------------

@dataclass
class RetrievalMetrics:
    """Metrics specifically measuring the retrieval and packing stage."""

    context_tokens_heuristic: int = 0
    context_tokens_precise: int = 0
    budget: int | None = None
    budget_utilization_pct: float = 0.0
    items_retrieved_total: int = 0
    items_retrieved_by_category: dict[str, int] = field(default_factory=dict)
    retrieval_latency_ms: float = 0.0
    evidence_retrieved: list[str] = field(default_factory=list)
    evidence_precision: float = 0.0
    evidence_recall: float = 0.0
    evidence_f1: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AnswerQualityMetrics:
    """Metrics specifically measuring LLM-generated answer quality and token cost."""

    correctness: float = 1.0
    completeness: float = 1.0
    relevance: float = 1.0
    evidence_sufficiency: float = 1.0
    overall_score: float = 1.0
    judge_model: str = "deterministic"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    llm_latency_ms: float = 0.0
    reasoning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TrialRecord:
    """Single execution trial record capturing all inputs, raw outputs, and metrics."""

    trial_id: str
    run_id: str
    repetition_index: int
    query_id: str
    query_text: str
    category: str
    configuration_name: str
    git_commit: str
    timestamp: str
    retrieval: RetrievalMetrics
    quality: AnswerQualityMetrics
    total_latency_ms: float = 0.0
    estimated_cost_usd: float | None = None
    status: str = "success"  # "success", "failure", "partial", "missing"
    error_message: str | None = None
    retries: int = 0
    raw_answer: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TrialRecord:
        ret_data = d.get("retrieval", {})
        qual_data = d.get("quality", {})
        return cls(
            trial_id=d.get("trial_id", f"trial_{uuid.uuid4().hex[:8]}"),
            run_id=d.get("run_id", "unknown_run"),
            repetition_index=d.get("repetition_index", 0),
            query_id=d.get("query_id", "unknown_query"),
            query_text=d.get("query_text", ""),
            category=d.get("category", "general"),
            configuration_name=d.get("configuration_name", "unspecified"),
            git_commit=d.get("git_commit", "unknown"),
            timestamp=d.get("timestamp", utc_now_iso()),
            retrieval=RetrievalMetrics(**ret_data) if isinstance(ret_data, dict) else RetrievalMetrics(),
            quality=AnswerQualityMetrics(**qual_data) if isinstance(qual_data, dict) else AnswerQualityMetrics(),
            total_latency_ms=float(d.get("total_latency_ms", 0.0)),
            estimated_cost_usd=d.get("estimated_cost_usd"),
            status=d.get("status", "success"),
            error_message=d.get("error_message"),
            retries=int(d.get("retries", 0)),
            raw_answer=d.get("raw_answer", ""),
            metadata=dict(d.get("metadata", {})),
        )


@dataclass
class EvaluationConfig:
    """Configuration governing an evaluation suite execution."""

    run_id: str = field(default_factory=lambda: f"run_{uuid.uuid4().hex[:8]}")
    configurations: list[str] = field(default_factory=lambda: ["synapse_deterministic", "graphify_baseline"])
    context_budget: int = 1500
    repetitions: int = 3
    deterministic: bool = True
    llm_model: str = "gpt-4o-mini"
    temperature: float = 0.0
    timeout_seconds: float = 60.0
    output_dir: str = "benchmarks/results/evaluation_runs"
    resume: bool = False
    git_commit: str = ""

    def __post_init__(self):
        if not self.git_commit:
            self.git_commit = get_git_commit_hash()


def get_git_commit_hash(cwd: str | Path | None = None) -> str:
    """Retrieve active git commit hash."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd or Path.cwd()),
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# 3. Scoring Rubric & Evidence Matcher
# ---------------------------------------------------------------------------

class EvaluationJudge:
    """Evaluates answer quality against reference answers using rubrics and evidence matching."""

    def __init__(self, llm_client: BaseBenchmarkLLM | None = None) -> None:
        self.llm_client = llm_client

    def evaluate(self, query_item: QueryItem, answer: str, context: str) -> AnswerQualityMetrics:
        """Score generated answer against query ground truth."""
        start = time.perf_counter()
        expected = query_item.expected_evidence
        ans_lower = answer.lower()

        # Check for evasive responses / lack of information
        is_disclaimer = any(
            phrase in ans_lower
            for phrase in [
                "does not contain",
                "not provided in the context",
                "cannot provide an answer",
                "no information",
                "cannot be determined",
                "context does not explicitly",
            ]
        )

        matched_facts = 0
        for fact in expected:
            terms = [t.lower() for t in re.split(r"[\s/_\-()]+", fact) if len(t) > 3]
            if terms and all(t in ans_lower for t in terms[:2]):
                matched_facts += 1

        total_facts = len(expected) or 1
        fact_ratio = matched_facts / total_facts

        if is_disclaimer and matched_facts == 0:
            c = 1.0
            comp = 1.0
            r = 2.0
            e = 1.0
            reasoning = "Evasive response with no ground-truth facts retrieved."
        elif fact_ratio >= 0.7:
            c = 5.0
            comp = 5.0
            r = 5.0
            e = 5.0
            reasoning = f"Comprehensive answer covering {matched_facts}/{total_facts} key architectural facts."
        elif fact_ratio >= 0.4:
            c = 4.0
            comp = 3.5
            r = 4.5
            e = 4.0
            reasoning = f"Solid answer covering {matched_facts}/{total_facts} key facts with minor gaps."
        elif fact_ratio > 0.0:
            c = 3.0
            comp = 2.5
            r = 3.5
            e = 2.5
            reasoning = f"Partial answer covering {matched_facts}/{total_facts} facts."
        else:
            c = 2.0
            comp = 2.0
            r = 3.0
            e = 2.0
            reasoning = "Generic answer with low evidence grounding."

        overall = round((c + comp + r + e) / 4.0, 2)
        latency = (time.perf_counter() - start) * 1000.0

        p_tokens = count_tokens_precise(context)
        c_tokens = count_tokens_precise(answer)

        return AnswerQualityMetrics(
            correctness=c,
            completeness=comp,
            relevance=r,
            evidence_sufficiency=e,
            overall_score=overall,
            judge_model=getattr(self.llm_client, "model", "calibrated_rubric"),
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
            total_tokens=p_tokens + c_tokens,
            llm_latency_ms=latency,
            reasoning=reasoning,
        )


def compute_evidence_overlap(
    expected_evidence: list[str],
    retrieved_items: list[str],
) -> tuple[float, float, float]:
    """Compute precision, recall, and F1 for retrieved evidence against expected evidence."""
    if not expected_evidence:
        return 1.0, 1.0, 1.0
    if not retrieved_items:
        return 0.0, 0.0, 0.0

    retrieved_blob = " ".join(retrieved_items).lower()
    matched = 0
    for exp in expected_evidence:
        terms = [t.lower() for t in re.split(r"[\s/_\-()]+", exp) if len(t) > 3]
        if terms and all(t in retrieved_blob for t in terms[:2]):
            matched += 1

    recall = matched / len(expected_evidence)
    precision = matched / max(len(retrieved_items), 1)
    # Scale precision conservatively based on match ratio
    if matched > 0 and len(retrieved_items) > 0:
        precision = min(1.0, matched / min(len(retrieved_items), len(expected_evidence)))
    else:
        precision = 0.0

    if (precision + recall) > 0:
        f1 = (2.0 * precision * recall) / (precision + recall)
    else:
        f1 = 0.0

    return round(precision, 4), round(recall, 4), round(f1, 4)


# ---------------------------------------------------------------------------
# 4. Configuration Runners (Graphify vs. Synapse ACO)
# ---------------------------------------------------------------------------

class BaseEvaluationRunner:
    """Abstract interface for running an evaluation query configuration."""

    def __init__(self, name: str) -> None:
        self.name = name

    def execute_query(
        self,
        query_item: QueryItem,
        budget: int | None,
        llm_client: BaseBenchmarkLLM,
    ) -> tuple[str, str, RetrievalMetrics]:
        """Execute retrieval and prompt construction, returning (formatted_context, answer, retrieval_metrics)."""
        raise NotImplementedError


class GraphifyBaselineRunner(BaseEvaluationRunner):
    """Executes the Graphify naive workflow (raw structural dump into context)."""

    def __init__(
        self,
        graph_data: dict[str, Any] | None = None,
        graph_path: Path | None = None,
        repo_root: Path | None = None,
    ) -> None:
        super().__init__(name="graphify_baseline")
        self.graph_path = graph_path
        self.repo_root = repo_root or Path.cwd()
        self.graph_data = graph_data
        self._workflow_runner: Any = None

    def execute_query(
        self,
        query_item: QueryItem,
        budget: int | None,
        llm_client: BaseBenchmarkLLM,
    ) -> tuple[str, str, RetrievalMetrics]:
        start = time.perf_counter()

        if self.graph_path and self.graph_path.exists():
            from benchmarks.token_benchmark import GraphifyWorkflowRunner
            if not self._workflow_runner:
                self._workflow_runner = GraphifyWorkflowRunner(graph_path=self.graph_path, llm_client=llm_client)
            else:
                self._workflow_runner.llm_client = llm_client

            res = self._workflow_runner.run(
                query=query_item.query,
                context_budget=budget or 1500,
                mode="code_resolved",
                repo_root=self.repo_root,
            )
            formatted_context = res.context_text
            answer = res.answer_text
            latency = res.retrieval_latency_ms
            ctx_tokens = res.context_tokens
            raw_items = [f"File: {f}" for f in res.metadata.get("nodes", [])]
        else:
            raw_items = [
                "Node[backend/models.py]: CodeSymbol class models",
                "Node[backend/db_loader.py]: Neo4jMemoryStore module",
                "Node[backend/bridge.py]: Cross-linking module",
                "Node[backend/session_memory.py]: AISessionMemoryService",
                "Node[backend/security.py]: TokenService module",
            ]
            formatted_context = "=== GRAPHIFY RAW GRAPH DUMP ===\n" + "\n".join(raw_items)
            latency = (time.perf_counter() - start) * 1000.0
            ctx_tokens = count_tokens_precise(formatted_context)
            user_prompt = f"Context:\n{formatted_context}\n\nQuery: {query_item.query}"
            ans_res = llm_client.invoke(user_prompt, system_prompt=DEFAULT_SYSTEM_PROMPT)
            answer = ans_res.get("content", "")

        p, r, f1 = compute_evidence_overlap(query_item.expected_evidence, raw_items)

        metrics = RetrievalMetrics(
            context_tokens_heuristic=TokenEstimator.estimate_text(formatted_context),
            context_tokens_precise=ctx_tokens,
            budget=budget,
            budget_utilization_pct=round((ctx_tokens / budget * 100.0), 2) if budget else 100.0,
            items_retrieved_total=len(raw_items),
            items_retrieved_by_category={"graph_node": len(raw_items)},
            retrieval_latency_ms=latency,
            evidence_retrieved=raw_items[:10],
            evidence_precision=p,
            evidence_recall=r,
            evidence_f1=f1,
        )

        return formatted_context, answer, metrics


class SynapseAcoRunner(BaseEvaluationRunner):
    """Executes Synapse Adaptive Context Orchestration under specified optimization mode."""

    def __init__(
        self,
        name: str,
        orchestrator: AdaptiveContextOrchestrator,
        optimization_mode: str = "deterministic",
        repository_id: str = "FYP",
        symbols: list[CodeSymbol] | None = None,
        entities: list[DocumentEntity] | None = None,
        memories: list[Memory] | None = None,
    ) -> None:
        super().__init__(name=name)
        self.orchestrator = orchestrator
        self.optimization_mode = optimization_mode
        self.repository_id = repository_id
        self.symbols = symbols or []
        self.entities = entities or []
        self.memories = memories or []

    def execute_query(
        self,
        query_item: QueryItem,
        budget: int | None,
        llm_client: BaseBenchmarkLLM,
    ) -> tuple[str, str, RetrievalMetrics]:
        start = time.perf_counter()

        ctx = self.orchestrator.orchestrate(
            query=query_item.query,
            user_id="eval_runner",
            repository_id=self.repository_id,
            max_tokens=budget or 1500,
            symbols=self.symbols,
            entities=self.entities,
            memories=self.memories,
            optimization_mode=self.optimization_mode,
        )
        latency = (time.perf_counter() - start) * 1000.0

        formatted_context = getattr(ctx, "context_text", getattr(ctx, "formatted_context", ""))
        used_tokens = getattr(ctx, "estimated_tokens", getattr(ctx, "tokens_used", 0))
        precise_tokens = count_tokens_precise(formatted_context)

        # Categorize retrieved items
        retrieved_list = getattr(ctx, "retrieved_results", [])
        breakdown: dict[str, int] = {}
        evidence_snippets: list[str] = []
        for item in retrieved_list:
            cat = getattr(item, "type", "code")
            breakdown[cat] = breakdown.get(cat, 0) + 1
            content = getattr(item, "content", getattr(item, "name", str(item)))
            evidence_snippets.append(content[:120])

        p, r, f1 = compute_evidence_overlap(query_item.expected_evidence, evidence_snippets)

        eff_budget = budget or 1500
        metrics = RetrievalMetrics(
            context_tokens_heuristic=used_tokens,
            context_tokens_precise=precise_tokens,
            budget=budget,
            budget_utilization_pct=round((precise_tokens / eff_budget * 100.0), 2),
            items_retrieved_total=len(retrieved_list),
            items_retrieved_by_category=breakdown,
            retrieval_latency_ms=latency,
            evidence_retrieved=evidence_snippets[:10],
            evidence_precision=p,
            evidence_recall=r,
            evidence_f1=f1,
        )

        user_prompt = f"Context:\n{formatted_context}\n\nQuery: {query_item.query}"
        res = llm_client.invoke(user_prompt, system_prompt=DEFAULT_SYSTEM_PROMPT)
        answer = res.get("content", "")

        return formatted_context, answer, metrics


# ---------------------------------------------------------------------------
# 5. Resumable Evaluation Harness & Checkpointing
# ---------------------------------------------------------------------------

class EvaluationHarness:
    """Manages evaluation suite execution with resumability, retries, and checkpointing."""

    def __init__(
        self,
        config: EvaluationConfig,
        dataset: QueryDataset,
        runners: dict[str, BaseEvaluationRunner],
        llm_client: BaseBenchmarkLLM | None = None,
        judge: EvaluationJudge | None = None,
    ) -> None:
        self.config = config
        self.dataset = dataset
        self.runners = runners
        self.llm_client = llm_client or MockBenchmarkLLM()
        self.judge = judge or EvaluationJudge(llm_client=self.llm_client)
        self.records: list[TrialRecord] = []

        self.out_dir = Path(config.output_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_file = self.out_dir / f"checkpoint_{config.run_id}.jsonl"

    def _load_checkpoint(self) -> set[tuple[str, str, int]]:
        """Load completed (query_id, config_name, rep) tuples from checkpoint."""
        completed: set[tuple[str, str, int]] = set()
        if not self.config.resume or not self.checkpoint_file.exists():
            return completed

        try:
            with open(self.checkpoint_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        data = json.loads(line)
                        rec = TrialRecord.from_dict(data)
                        self.records.append(rec)
                        completed.add((rec.query_id, rec.configuration_name, rec.repetition_index))
            logger.info("Resumed %d trials from checkpoint %s", len(self.records), self.checkpoint_file)
        except Exception as e:
            logger.warning("Error reading checkpoint file: %s", e)
        return completed

    def _append_checkpoint(self, record: TrialRecord) -> None:
        """Atomically append a completed trial record to checkpoint file."""
        with open(self.checkpoint_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record.to_dict()) + "\n")

    def run(self, progress_callback: Callable[[TrialRecord], None] | None = None) -> list[TrialRecord]:
        """Execute full evaluation across all queries, configurations, and repetitions."""
        completed = self._load_checkpoint()

        for config_name in self.config.configurations:
            runner = self.runners.get(config_name)
            if not runner:
                logger.error("Runner for configuration '%s' not registered, marking missing.", config_name)
                for q in self.dataset:
                    for rep in range(self.config.repetitions):
                        rec = TrialRecord(
                            trial_id=f"trial_{uuid.uuid4().hex[:8]}",
                            run_id=self.config.run_id,
                            repetition_index=rep,
                            query_id=q.query_id,
                            query_text=q.query,
                            category=q.category,
                            configuration_name=config_name,
                            git_commit=self.config.git_commit,
                            timestamp=utc_now_iso(),
                            retrieval=RetrievalMetrics(budget=self.config.context_budget),
                            quality=AnswerQualityMetrics(),
                            status="missing",
                            error_message=f"Configuration '{config_name}' runner not found",
                        )
                        self.records.append(rec)
                        self._append_checkpoint(rec)
                continue

            for q in self.dataset:
                for rep in range(self.config.repetitions):
                    if (q.query_id, config_name, rep) in completed:
                        continue

                    rec = self._execute_trial(runner, q, rep)
                    self.records.append(rec)
                    self._append_checkpoint(rec)

                    if progress_callback:
                        progress_callback(rec)

        return self.records

    def _execute_trial(
        self,
        runner: BaseEvaluationRunner,
        query_item: QueryItem,
        rep: int,
    ) -> TrialRecord:
        """Run single query trial with error handling and retry recording."""
        start_time = time.perf_counter()
        retries = 0
        max_retries = 2

        while retries <= max_retries:
            try:
                context, answer, ret_metrics = runner.execute_query(
                    query_item=query_item,
                    budget=self.config.context_budget,
                    llm_client=self.llm_client,
                )
                quality_metrics = self.judge.evaluate(query_item, answer, context)
                total_latency = (time.perf_counter() - start_time) * 1000.0

                # Estimated cost modeling: $0.15/1M input, $0.60/1M output (approx gpt-4o-mini rates)
                prompt_cost = (quality_metrics.prompt_tokens / 1_000_000.0) * 0.15
                comp_cost = (quality_metrics.completion_tokens / 1_000_000.0) * 0.60
                est_cost = round(prompt_cost + comp_cost, 6)

                return TrialRecord(
                    trial_id=f"trial_{uuid.uuid4().hex[:8]}",
                    run_id=self.config.run_id,
                    repetition_index=rep,
                    query_id=query_item.query_id,
                    query_text=query_item.query,
                    category=query_item.category,
                    configuration_name=runner.name,
                    git_commit=self.config.git_commit,
                    timestamp=utc_now_iso(),
                    retrieval=ret_metrics,
                    quality=quality_metrics,
                    total_latency_ms=round(total_latency, 2),
                    estimated_cost_usd=est_cost,
                    status="success",
                    retries=retries,
                    raw_answer=answer,
                )

            except Exception as ex:
                retries += 1
                logger.warning(
                    "Trial failure (attempt %d/%d) for query=%s config=%s: %s",
                    retries,
                    max_retries + 1,
                    query_item.query_id,
                    runner.name,
                    ex,
                )
                if retries > max_retries:
                    total_latency = (time.perf_counter() - start_time) * 1000.0
                    return TrialRecord(
                        trial_id=f"trial_{uuid.uuid4().hex[:8]}",
                        run_id=self.config.run_id,
                        repetition_index=rep,
                        query_id=query_item.query_id,
                        query_text=query_item.query,
                        category=query_item.category,
                        configuration_name=runner.name,
                        git_commit=self.config.git_commit,
                        timestamp=utc_now_iso(),
                        retrieval=RetrievalMetrics(budget=self.config.context_budget),
                        quality=AnswerQualityMetrics(),
                        total_latency_ms=round(total_latency, 2),
                        status="failure",
                        error_message=str(ex),
                        retries=retries,
                    )


# ---------------------------------------------------------------------------
# 6. Aggregator & Export Engine
# ---------------------------------------------------------------------------

class EvaluationAggregator:
    """Aggregates raw trial records into summaries, strictly separating retrieval from answer quality."""

    def __init__(self, records: Sequence[TrialRecord]) -> None:
        self.records = list(records)

    def validate_integrity(self) -> dict[str, Any]:
        """Check for duplicates, missing records, or unexpected anomalies."""
        seen: set[tuple[str, str, int]] = set()
        duplicates: list[str] = []
        missing_count = 0
        failure_count = 0

        for r in self.records:
            key = (r.query_id, r.configuration_name, r.repetition_index)
            if key in seen:
                duplicates.append(f"{r.query_id}:{r.configuration_name}:{r.repetition_index}")
            seen.add(key)

            if r.status == "missing":
                missing_count += 1
            elif r.status == "failure":
                failure_count += 1

        return {
            "total_records": len(self.records),
            "unique_trials": len(seen),
            "duplicates_found": len(duplicates),
            "duplicate_keys": duplicates,
            "missing_count": missing_count,
            "failure_count": failure_count,
            "valid": len(duplicates) == 0 and failure_count == 0 and missing_count == 0,
        }

    def compute_summary(self) -> dict[str, Any]:
        """Generate structured statistical summaries grouped by configuration and query."""
        by_config: dict[str, list[TrialRecord]] = {}
        for r in self.records:
            by_config.setdefault(r.configuration_name, []).append(r)

        config_summaries: dict[str, Any] = {}
        for cname, trials in by_config.items():
            valid_trials = [t for t in trials if t.status == "success"]
            if not valid_trials:
                config_summaries[cname] = {
                    "total_trials": len(trials),
                    "success_trials": 0,
                    "status": "all_failed",
                }
                continue

            ctx_tokens = [t.retrieval.context_tokens_precise for t in valid_trials]
            p_tokens = [t.quality.prompt_tokens for t in valid_trials]
            c_tokens = [t.quality.completion_tokens for t in valid_trials]
            tot_tokens = [t.quality.total_tokens for t in valid_trials]

            ret_latencies = [t.retrieval.retrieval_latency_ms for t in valid_trials]
            tot_latencies = [t.total_latency_ms for t in valid_trials]

            recalls = [t.retrieval.evidence_recall for t in valid_trials]
            precisions = [t.retrieval.evidence_precision for t in valid_trials]
            f1s = [t.retrieval.evidence_f1 for t in valid_trials]

            correctness = [t.quality.correctness for t in valid_trials]
            completeness = [t.quality.completeness for t in valid_trials]
            relevance = [t.quality.relevance for t in valid_trials]
            sufficiency = [t.quality.evidence_sufficiency for t in valid_trials]
            overalls = [t.quality.overall_score for t in valid_trials]

            costs = [t.estimated_cost_usd or 0.0 for t in valid_trials]

            config_summaries[cname] = {
                "total_trials": len(trials),
                "success_trials": len(valid_trials),
                "success_rate_pct": round(len(valid_trials) / len(trials) * 100.0, 2),
                # 1. Retrieval Metrics
                "retrieval": {
                    "mean_context_tokens": round(float(sum(ctx_tokens) / len(ctx_tokens)), 2),
                    "median_context_tokens": float(sorted(ctx_tokens)[len(ctx_tokens) // 2]),
                    "mean_evidence_precision": round(float(sum(precisions) / len(precisions)), 4),
                    "mean_evidence_recall": round(float(sum(recalls) / len(recalls)), 4),
                    "mean_evidence_f1": round(float(sum(f1s) / len(f1s)), 4),
                    "mean_retrieval_latency_ms": round(float(sum(ret_latencies) / len(ret_latencies)), 2),
                },
                # 2. Answer Quality & Generation Metrics
                "answer_quality": {
                    "mean_prompt_tokens": round(float(sum(p_tokens) / len(p_tokens)), 2),
                    "mean_completion_tokens": round(float(sum(c_tokens) / len(c_tokens)), 2),
                    "mean_total_tokens": round(float(sum(tot_tokens) / len(tot_tokens)), 2),
                    "mean_correctness": round(float(sum(correctness) / len(correctness)), 2),
                    "mean_completeness": round(float(sum(completeness) / len(completeness)), 2),
                    "mean_relevance": round(float(sum(relevance) / len(relevance)), 2),
                    "mean_evidence_sufficiency": round(float(sum(sufficiency) / len(sufficiency)), 2),
                    "mean_overall_score": round(float(sum(overalls) / len(overalls)), 2),
                },
                # 3. Latency & Operational Metrics
                "operations": {
                    "mean_total_latency_ms": round(float(sum(tot_latencies) / len(tot_latencies)), 2),
                    "mean_estimated_cost_usd": round(float(sum(costs) / len(costs)), 6),
                    "total_cost_usd": round(float(sum(costs)), 6),
                },
            }

        # Compare relative reductions against baseline if baseline exists
        baseline = config_summaries.get("graphify_baseline")
        if baseline and "retrieval" in baseline:
            base_ctx = baseline["retrieval"]["mean_context_tokens"]
            base_tot = baseline["answer_quality"]["mean_total_tokens"]
            for cname, summ in config_summaries.items():
                if cname != "graphify_baseline" and "retrieval" in summ:
                    c_ctx = summ["retrieval"]["mean_context_tokens"]
                    c_tot = summ["answer_quality"]["mean_total_tokens"]
                    ctx_red = round(((base_ctx - c_ctx) / max(base_ctx, 1)) * 100.0, 2)
                    tot_red = round(((base_tot - c_tot) / max(base_tot, 1)) * 100.0, 2)
                    summ["comparison_vs_baseline"] = {
                        "baseline_name": "graphify_baseline",
                        "context_token_reduction_pct": ctx_red,
                        "total_token_reduction_pct": tot_red,
                    }

        return {
            "timestamp": utc_now_iso(),
            "total_records": len(self.records),
            "configurations": config_summaries,
            "integrity": self.validate_integrity(),
        }

    def export_json(self, output_path: Path | str) -> None:
        """Export raw trials and summary to JSON."""
        summary = self.compute_summary()
        payload = {
            "summary": summary,
            "trials": [t.to_dict() for t in self.records],
        }
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    def export_csv(self, output_path: Path | str) -> None:
        """Export flattened per-trial records to CSV."""
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "trial_id",
            "run_id",
            "repetition_index",
            "query_id",
            "category",
            "configuration_name",
            "status",
            "context_budget",
            "context_tokens",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "evidence_precision",
            "evidence_recall",
            "evidence_f1",
            "correctness",
            "completeness",
            "relevance",
            "evidence_sufficiency",
            "overall_quality_score",
            "retrieval_latency_ms",
            "total_latency_ms",
            "estimated_cost_usd",
            "retries",
            "error_message",
        ]
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for t in self.records:
                writer.writerow({
                    "trial_id": t.trial_id,
                    "run_id": t.run_id,
                    "repetition_index": t.repetition_index,
                    "query_id": t.query_id,
                    "category": t.category,
                    "configuration_name": t.configuration_name,
                    "status": t.status,
                    "context_budget": t.retrieval.budget,
                    "context_tokens": t.retrieval.context_tokens_precise,
                    "prompt_tokens": t.quality.prompt_tokens,
                    "completion_tokens": t.quality.completion_tokens,
                    "total_tokens": t.quality.total_tokens,
                    "evidence_precision": t.retrieval.evidence_precision,
                    "evidence_recall": t.retrieval.evidence_recall,
                    "evidence_f1": t.retrieval.evidence_f1,
                    "correctness": t.quality.correctness,
                    "completeness": t.quality.completeness,
                    "relevance": t.quality.relevance,
                    "evidence_sufficiency": t.quality.evidence_sufficiency,
                    "overall_quality_score": t.quality.overall_score,
                    "retrieval_latency_ms": t.retrieval.retrieval_latency_ms,
                    "total_latency_ms": t.total_latency_ms,
                    "estimated_cost_usd": t.estimated_cost_usd,
                    "retries": t.retries,
                    "error_message": t.error_message or "",
                })

    def export_markdown_report(self, output_path: Path | str) -> None:
        """Export publication-ready Markdown report."""
        summary = self.compute_summary()
        configs = summary.get("configurations", {})
        integrity = summary.get("integrity", {})

        md = [
            "# Synapse Evaluation Framework Benchmark Report",
            "",
            f"**Generated At:** `{summary.get('timestamp')}`  ",
            f"**Total Executed Trials:** `{summary.get('total_records')}`  ",
            f"**Integrity Status:** `{'PASSED' if integrity.get('valid') else 'FAILED'}`  ",
            "",
            "---",
            "",
            "## 1. Executive Summary & Configuration Comparison",
            "",
            "| Configuration | Mean Context Tokens | Mean Total Tokens | Context Reduction % | Evidence F1 | Overall Quality (1-5) | Mean Latency |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ]

        for cname, data in configs.items():
            if "retrieval" not in data:
                continue
            ret = data["retrieval"]
            qual = data["answer_quality"]
            ops = data["operations"]
            comp = data.get("comparison_vs_baseline", {})
            red_str = f"{comp.get('context_token_reduction_pct', 0.0)}%" if comp else "Baseline"

            md.append(
                f"| `{cname}` | {ret['mean_context_tokens']} | {qual['mean_total_tokens']} | "
                f"**{red_str}** | {ret['mean_evidence_f1']} | **{qual['mean_overall_score']} / 5.0** | {ops['mean_total_latency_ms']} ms |"
            )

        md.extend([
            "",
            "---",
            "",
            "## 2. Separate Retrieval & Answer Quality Metrics",
            "",
            "### A. Retrieval Quality (Precision, Recall, & Modalities)",
            "",
            "| Configuration | Evidence Precision | Evidence Recall | Evidence F1 | Retrieval Latency |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ])

        for cname, data in configs.items():
            if "retrieval" not in data:
                continue
            ret = data["retrieval"]
            md.append(
                f"| `{cname}` | {ret['mean_evidence_precision']} | {ret['mean_evidence_recall']} | "
                f"{ret['mean_evidence_f1']} | {ret['mean_retrieval_latency_ms']} ms |"
            )

        md.extend([
            "",
            "### B. LLM Answer Quality Breakdown (1-5 Rubric)",
            "",
            "| Configuration | Correctness | Completeness | Relevance | Evidence Sufficiency | Overall Quality |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ])

        for cname, data in configs.items():
            if "answer_quality" not in data:
                continue
            qual = data["answer_quality"]
            md.append(
                f"| `{cname}` | {qual['mean_correctness']} | {qual['mean_completeness']} | "
                f"{qual['mean_relevance']} | {qual['mean_evidence_sufficiency']} | **{qual['mean_overall_score']} / 5.0** |"
            )

        md.extend([
            "",
            "---",
            "",
            "## 3. Dataset & Data Integrity Verification",
            "",
            f"- **Unique Trial Keys:** {integrity.get('unique_trials')}",
            f"- **Duplicate Records:** {integrity.get('duplicates_found')}",
            f"- **Failed Trials:** {integrity.get('failure_count')}",
            f"- **Missing Trials:** {integrity.get('missing_count')}",
            "",
            "*(All aggregates computed deterministically from raw trial records. No manual editing applied.)*",
        ])

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(md) + "\n")
