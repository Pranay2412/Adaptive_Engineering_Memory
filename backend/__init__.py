"""Adaptive Engineering Memory ingestion and storage components."""

from .bridge import build_cross_links
from .db_loader import Neo4jMemoryStore
from .ingestion_code import ingest_code, parse_graphify_graph
from .hybrid_retrieval import (
    HybridRetrievalService,
    SemanticKnowledgeIndex,
    hybrid_retrieve_engineering_memory,
)
from .models import (
    CandidateRankingScore,
    CodeSymbol,
    Document,
    DocumentEntity,
    GraphEdge,
    HybridQueryResponse,
    HybridRetrievalResult,
    IntentAnalysisResult,
    MemoryQueryResponse,
    Module,
    OptimizedItemRecord,
    OrchestratedContext,
    QueryIntent,
    RankingWeights,
    RelationshipHop,
    RemovedItemRecord,
    Repository,
    ResultProvenance,
    RetrievalResult,
    TokenOptimizationResult,
    scoped_id,
)
from .token_optimizer import (
    ContextTier,
    RedundancyDetector,
    TokenEstimator,
    TokenOptimizationEngine,
    optimize_tokens,
    render_candidate_item,
)
from .ranking import (
    BaseContextRanker,
    ConfigurableContextRanker,
    DEFAULT_SOURCE_RELIABILITY_MAP,
    adapt_weights_for_intent,
    compute_confidence,
    compute_freshness,
    compute_graph_relevance,
    compute_lexical_relevance,
    compute_repository_match,
    compute_semantic_relevance,
    compute_source_reliability,
    compute_token_cost_efficiency,
)
from .orchestrator import (
    AdaptiveContextOrchestrator,
    AdaptiveContextPacker,
    BaseContextPacker,
    BaseIntentClassifier,
    DEFAULT_INTENT_STRATEGIES,
    IntentAwareContextRanker,
    RuleBasedIntentClassifier,
    TokenOptimizedContextPacker,
    deduplicate_results,
    orchestrate_context,
)
from .normalization import (
    EntityKind,
    NormalizedEntity,
    are_compatible,
    compare_entities,
    detect_kind,
    normalize_entity,
    to_canonical,
    tokenize_name,
)
from .retrieval import (
    MemoryRetrievalService,
    query_engineering_memory,
)
from .semantic_matcher import (
    BaseEmbeddingProvider,
    GeminiEmbeddingProvider,
    MockEmbeddingProvider,
    OpenAIEmbeddingProvider,
    SemanticCandidateRetriever,
    SemanticSymbolIndex,
    get_embedding_provider,
)

__all__ = [
    "AdaptiveContextOrchestrator",
    "AdaptiveContextPacker",
    "BaseContextPacker",
    "BaseContextRanker",
    "BaseEmbeddingProvider",
    "BaseIntentClassifier",
    "CandidateRankingScore",
    "CodeSymbol",
    "ConfigurableContextRanker",
    "ContextTier",
    "DEFAULT_INTENT_STRATEGIES",
    "DEFAULT_SOURCE_RELIABILITY_MAP",
    "Document",
    "DocumentEntity",
    "EntityKind",
    "GeminiEmbeddingProvider",
    "GraphEdge",
    "HybridQueryResponse",
    "HybridRetrievalResult",
    "HybridRetrievalService",
    "IntentAnalysisResult",
    "IntentAwareContextRanker",
    "MemoryQueryResponse",
    "MemoryRetrievalService",
    "MockEmbeddingProvider",
    "Module",
    "Neo4jMemoryStore",
    "NormalizedEntity",
    "OpenAIEmbeddingProvider",
    "OptimizedItemRecord",
    "OrchestratedContext",
    "QueryIntent",
    "RankingWeights",
    "RedundancyDetector",
    "RelationshipHop",
    "RemovedItemRecord",
    "Repository",
    "ResultProvenance",
    "RetrievalResult",
    "RuleBasedIntentClassifier",
    "SemanticCandidateRetriever",
    "SemanticKnowledgeIndex",
    "SemanticSymbolIndex",
    "TokenEstimator",
    "TokenOptimizationEngine",
    "TokenOptimizationResult",
    "TokenOptimizedContextPacker",
    "adapt_weights_for_intent",
    "are_compatible",
    "build_cross_links",
    "compare_entities",
    "compute_confidence",
    "compute_freshness",
    "compute_graph_relevance",
    "compute_lexical_relevance",
    "compute_repository_match",
    "compute_semantic_relevance",
    "compute_source_reliability",
    "compute_token_cost_efficiency",
    "deduplicate_results",
    "detect_kind",
    "get_embedding_provider",
    "hybrid_retrieve_engineering_memory",
    "ingest_code",
    "ingest_documents",
    "ingest_repository",
    "normalize_entity",
    "optimize_tokens",
    "orchestrate_context",
    "parse_graphify_graph",
    "query_engineering_memory",
    "render_candidate_item",
    "scoped_id",
    "to_canonical",
    "tokenize_name",
]


def ingest_documents(*args, **kwargs):
    """Lazy wrapper to prevent runpy RuntimeWarnings during direct module execution."""
    from .ingestion_docs import ingest_documents as _ingest_documents

    return _ingest_documents(*args, **kwargs)


def ingest_repository(*args, **kwargs):
    """Lazy wrapper to prevent runpy RuntimeWarnings during direct module execution."""
    from .pipeline import ingest_repository as _ingest_repository

    return _ingest_repository(*args, **kwargs)