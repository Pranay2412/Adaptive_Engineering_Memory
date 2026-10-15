"""Multi-tier caching subsystem with LRU eviction, TTL, and repository invalidation.

Provides thread-safe, bounded in-memory caching for:
1. Query vector embeddings (avoiding redundant LLM/embedding API calls)
2. Intent classification results
3. Query term extraction and regex operations
4. Context token estimation calculations
5. Hybrid retrieval results with fine-grained repository invalidation
"""

from __future__ import annotations

import collections
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Hashable, TypeVar

logger = logging.getLogger(__name__)

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


@dataclass
class CacheEntry(Generic[V]):
    """Individual entry in an LRU cache with timestamp and access metrics."""

    value: V
    created_at: float
    last_accessed_at: float
    access_count: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)


class LRUCache(Generic[K, V]):
    """Thread-safe, bounded Least-Recently-Used (LRU) cache with optional TTL."""

    def __init__(self, maxsize: int = 1024, ttl_seconds: float | None = None) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self.maxsize = maxsize
        self.ttl_seconds = ttl_seconds
        self._cache: collections.OrderedDict[K, CacheEntry[V]] = collections.OrderedDict()
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.expirations = 0

    def get(self, key: K, default: V | None = None) -> V | None:
        """Retrieve value from cache, updating LRU order and recording hit/miss."""
        with self._lock:
            now = time.time()
            if key not in self._cache:
                self.misses += 1
                return default

            entry = self._cache[key]
            if self.ttl_seconds is not None and (now - entry.created_at) > self.ttl_seconds:
                del self._cache[key]
                self.expirations += 1
                self.misses += 1
                return default

            entry.last_accessed_at = now
            entry.access_count += 1
            self._cache.move_to_end(key)
            self.hits += 1
            return entry.value

    def put(self, key: K, value: V, metadata: dict[str, Any] | None = None) -> None:
        """Store key-value pair, evicting the least recently used entry if full."""
        with self._lock:
            now = time.time()
            if key in self._cache:
                self._cache[key] = CacheEntry(
                    value=value,
                    created_at=self._cache[key].created_at,
                    last_accessed_at=now,
                    access_count=self._cache[key].access_count + 1,
                    metadata=metadata or self._cache[key].metadata,
                )
                self._cache.move_to_end(key)
                return

            if len(self._cache) >= self.maxsize:
                self._cache.popitem(last=False)
                self.evictions += 1

            self._cache[key] = CacheEntry(
                value=value,
                created_at=now,
                last_accessed_at=now,
                access_count=1,
                metadata=metadata or {},
            )

    def contains(self, key: K) -> bool:
        """Check if unexpired key exists in cache without mutating LRU order."""
        with self._lock:
            if key not in self._cache:
                return False
            if self.ttl_seconds is not None:
                if (time.time() - self._cache[key].created_at) > self.ttl_seconds:
                    return False
            return True

    def delete(self, key: K) -> bool:
        """Delete specific key from cache. Returns True if key existed."""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    def clear(self) -> None:
        """Clear all entries from cache and reset hit/miss counters."""
        with self._lock:
            self._cache.clear()
            self.hits = 0
            self.misses = 0
            self.evictions = 0
            self.expirations = 0

    def purge_expired(self) -> int:
        """Purge all expired entries. Returns number of purged entries."""
        if self.ttl_seconds is None:
            return 0
        with self._lock:
            now = time.time()
            expired_keys = [
                k for k, entry in self._cache.items()
                if (now - entry.created_at) > self.ttl_seconds
            ]
            for k in expired_keys:
                del self._cache[k]
                self.expirations += 1
            return len(expired_keys)

    def invalidate_matching(self, predicate: Callable[[K, CacheEntry[V]], bool]) -> int:
        """Invalidate entries matching a custom filter predicate."""
        with self._lock:
            to_remove = [k for k, v in self._cache.items() if predicate(k, v)]
            for k in to_remove:
                del self._cache[k]
            return len(to_remove)

    @property
    def hit_rate(self) -> float:
        """Calculate cache hit rate in [0.0, 1.0]."""
        with self._lock:
            total = self.hits + self.misses
            return round(self.hits / total, 4) if total > 0 else 0.0

    def stats(self) -> dict[str, Any]:
        """Return comprehensive cache statistics."""
        with self._lock:
            total_requests = self.hits + self.misses
            return {
                "size": len(self._cache),
                "maxsize": self.maxsize,
                "hits": self.hits,
                "misses": self.misses,
                "total_requests": total_requests,
                "hit_rate": self.hit_rate,
                "evictions": self.evictions,
                "expirations": self.expirations,
                "ttl_seconds": self.ttl_seconds,
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)


class QueryEmbeddingCache:
    """Specialized cache for query dense embeddings, preventing costly redundant API calls."""

    # Estimated cost savings per embedding API call (OpenAI text-embedding-3-small approx $0.00002/1k tokens)
    ESTIMATED_COST_PER_EMBEDDING_USD = 0.0000004

    def __init__(self, maxsize: int = 1024, ttl_seconds: float | None = 3600.0) -> None:
        self.cache: LRUCache[str, list[float]] = LRUCache(maxsize=maxsize, ttl_seconds=ttl_seconds)
        self._saved_api_calls = 0

    @staticmethod
    def _make_key(model: str, text: str) -> str:
        norm_model = model.strip().lower()
        norm_text = text.strip().lower()
        content_hash = hashlib.sha256(norm_text.encode("utf-8")).hexdigest()[:16]
        return f"{norm_model}:{content_hash}:{norm_text[:40]}"

    def get(self, model: str, text: str) -> list[float] | None:
        """Retrieve cached embedding vector for model and text."""
        key = self._make_key(model, text)
        vec = self.cache.get(key)
        if vec is not None:
            self._saved_api_calls += 1
        return vec

    def put(self, model: str, text: str, vector: list[float]) -> None:
        """Cache embedding vector for model and text."""
        key = self._make_key(model, text)
        self.cache.put(key, vector)

    def stats(self) -> dict[str, Any]:
        """Return cache statistics including estimated cost savings."""
        st = self.cache.stats()
        st["saved_api_calls"] = self._saved_api_calls
        st["estimated_saved_cost_usd"] = round(
            self._saved_api_calls * self.ESTIMATED_COST_PER_EMBEDDING_USD, 6
        )
        return st

    def clear(self) -> None:
        self.cache.clear()
        self._saved_api_calls = 0


class QueryResultCache:
    """Specialized query result cache with repository-scoped invalidation."""

    def __init__(self, maxsize: int = 512, ttl_seconds: float | None = 600.0) -> None:
        self.cache: LRUCache[str, Any] = LRUCache(maxsize=maxsize, ttl_seconds=ttl_seconds)

    @staticmethod
    def _make_key(repository_id: str, query: str, config_hash: str, tenant_id: str = "default") -> str:
        norm_tenant = (tenant_id or "default").strip().lower()
        norm_repo = repository_id.strip().lower()
        norm_q = query.strip().lower()
        return f"{norm_tenant}::{norm_repo}::{config_hash}::{norm_q}"

    def get(self, repository_id: str, query: str, config_hash: str, tenant_id: str = "default") -> Any | None:
        key = self._make_key(repository_id, query, config_hash, tenant_id)
        return self.cache.get(key)

    def put(
        self,
        repository_id: str,
        query: str,
        config_hash: str,
        result: Any,
        metadata: dict[str, Any] | None = None,
        tenant_id: str = "default",
    ) -> None:
        key = self._make_key(repository_id, query, config_hash, tenant_id)
        meta = metadata or {}
        meta["repository_id"] = repository_id.strip().lower()
        meta["tenant_id"] = (tenant_id or "default").strip().lower()
        self.cache.put(key, result, metadata=meta)

    def invalidate_repository(self, repository_id: str, tenant_id: str | None = None) -> int:
        """Purge all cached query results for a specific repository (and optional tenant) upon updates."""
        target_repo = repository_id.strip().lower()
        target_tenant = tenant_id.strip().lower() if tenant_id else None

        def matcher(k: str, entry: CacheEntry[Any]) -> bool:
            entry_repo = entry.metadata.get("repository_id")
            entry_tenant = entry.metadata.get("tenant_id")
            if target_tenant:
                return (entry_repo == target_repo and entry_tenant == target_tenant) or k.startswith(f"{target_tenant}::{target_repo}::")
            return entry_repo == target_repo or f"::{target_repo}::" in k or k.startswith(f"{target_repo}::")

        removed = self.cache.invalidate_matching(matcher)
        logger.info(f"Invalidated {removed} cached query results for repository '{repository_id}' (tenant: {tenant_id})")
        return removed

    def stats(self) -> dict[str, Any]:
        return self.cache.stats()

    def clear(self) -> None:
        self.cache.clear()


class SynapseCacheManager:
    """Singleton manager coordinating all memory, embedding, and token caches."""

    _instance: SynapseCacheManager | None = None
    _lock = threading.Lock()

    def __new__(cls) -> SynapseCacheManager:
        with cls._lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._initialize()
                cls._instance = inst
            return cls._instance

    def _initialize(self) -> None:
        self.embedding_cache = QueryEmbeddingCache(maxsize=2048, ttl_seconds=7200.0)
        self.retrieval_cache = QueryResultCache(maxsize=512, ttl_seconds=1800.0)
        self.intent_cache: LRUCache[str, Any] = LRUCache(maxsize=1024, ttl_seconds=3600.0)
        self.query_terms_cache: LRUCache[str, list[str]] = LRUCache(maxsize=2048, ttl_seconds=3600.0)
        self.token_estimation_cache: LRUCache[str, int] = LRUCache(maxsize=4096, ttl_seconds=7200.0)

    def invalidate_repository(self, repository_id: str) -> dict[str, int]:
        """Invalidate all repository-specific caches when incremental or full updates occur."""
        results_invalidated = self.retrieval_cache.invalidate_repository(repository_id)
        return {"retrieval_results_invalidated": results_invalidated}

    def clear_all(self) -> None:
        """Clear all registered caches across all tiers."""
        self.embedding_cache.clear()
        self.retrieval_cache.clear()
        self.intent_cache.clear()
        self.query_terms_cache.clear()
        self.token_estimation_cache.clear()

    def get_global_stats(self) -> dict[str, Any]:
        """Aggregate performance and caching metrics across all subsystems."""
        return {
            "embedding_cache": self.embedding_cache.stats(),
            "retrieval_cache": self.retrieval_cache.stats(),
            "intent_cache": self.intent_cache.stats(),
            "query_terms_cache": self.query_terms_cache.stats(),
            "token_estimation_cache": self.token_estimation_cache.stats(),
        }


# Global singleton access
default_cache_manager = SynapseCacheManager()
