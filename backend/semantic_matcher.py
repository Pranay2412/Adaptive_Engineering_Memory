"""Semantic candidate retrieval using dense embeddings for the Graphify-Cognee bridge."""

from __future__ import annotations

import abc
import hashlib
import math
import os
import re
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from .models import CodeSymbol, DocumentEntity


def normalize_vector(vec: list[float]) -> list[float]:
    """Return unit-normalized vector; return zero vector if norm is zero."""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return [0.0] * len(vec)
    return [x / norm for x in vec]


def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """Compute cosine similarity between two numeric vectors in [-1.0, 1.0]."""
    if len(v1) != len(v2) or not v1:
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (norm1 * norm2)))


def document_entity_to_text(entity: DocumentEntity) -> str:
    """Serialize a DocumentEntity into a descriptive text representation for embedding."""
    parts = [f"Documentation Concept: {entity.name}", f"Type: {entity.type}"]
    desc = entity.description or getattr(entity, "description", "")
    if not desc and isinstance(getattr(entity, "metadata", None), dict):
        desc = entity.metadata.get("description", "")
    if desc:
        parts.append(f"Description: {desc}")

    source_doc = getattr(entity, "source_document", "")
    if source_doc:
        parts.append(f"Document: {source_doc}")

    source_file = getattr(entity, "document_path", "") or getattr(entity, "source_file", "")
    if not source_file and isinstance(getattr(entity, "metadata", None), dict):
        source_file = entity.metadata.get("source_file", "")
    if source_file:
        parts.append(f"Path: {source_file}")

    section = getattr(entity, "section", "")
    if not section and isinstance(getattr(entity, "metadata", None), dict):
        section = entity.metadata.get("section", "")
    if section:
        parts.append(f"Section: {section}")

    source_chunk = getattr(entity, "source_chunk", "")
    if not source_chunk and isinstance(getattr(entity, "metadata", None), dict):
        source_chunk = entity.metadata.get("source_chunk", "")
    if source_chunk:
        parts.append(f"Context: {source_chunk}")

    return " | ".join(parts)


def code_symbol_to_text(symbol: CodeSymbol) -> str:
    """Serialize a CodeSymbol into a descriptive text representation for embedding."""
    parts = [f"Code Symbol: {symbol.name}", f"Kind: {symbol.type}"]
    if symbol.qualified_name and symbol.qualified_name != symbol.name:
        parts.append(f"Qualified Name: {symbol.qualified_name}")
    if symbol.module:
        parts.append(f"Module: {symbol.module}")
    if symbol.package:
        parts.append(f"Package: {symbol.package}")
    file_path = symbol.source_file or symbol.file
    if file_path:
        parts.append(f"File: {file_path}")
    if symbol.language:
        parts.append(f"Language: {symbol.language}")

    doc = symbol.documentation or getattr(symbol, "docstring", "")
    if not doc and isinstance(getattr(symbol, "metadata", None), dict):
        doc = symbol.metadata.get("docstring", "") or symbol.metadata.get("documentation", "")
    if doc:
        parts.append(f"Docstring: {doc}")

    sig = symbol.signature or getattr(symbol, "signature", "")
    if not sig and isinstance(getattr(symbol, "metadata", None), dict):
        sig = symbol.metadata.get("signature", "")
    if sig:
        parts.append(f"Signature: {sig}")

    return " | ".join(parts)


class BaseEmbeddingProvider(abc.ABC):
    """Abstract interface for dense embedding generation."""

    @abc.abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Compute embeddings for a batch of input texts."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Compute embedding for a single text."""
        results = self.embed_batch([text])
        return results[0] if results else []


class MockEmbeddingProvider(BaseEmbeddingProvider):
    """Deterministic, zero-dependency embedding provider for unit testing and offline runs."""

    def __init__(
        self,
        concept_vectors: dict[str, list[float]] | None = None,
        dimension: int = 64,
    ) -> None:
        self.dimension = dimension
        self.concept_vectors: dict[str, list[float]] = {}
        if concept_vectors:
            for k, v in concept_vectors.items():
                self.concept_vectors[k.lower()] = normalize_vector(v)

        self.batch_call_count = 0
        self.total_texts_embedded = 0

    def _generate_deterministic_vector(self, text: str) -> list[float]:
        """Generate a reproducible, normalized pseudo-semantic vector from text tokens."""
        lower = text.lower()

        # Check explicit concept mappings
        for concept, vec in self.concept_vectors.items():
            if concept in lower:
                return vec

        # Deterministic projection from token hashes
        vector = [0.0] * self.dimension
        tokens = re.findall(r"[a-zA-Z0-9]+", lower)
        if not tokens:
            tokens = [lower]

        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            for idx in range(self.dimension):
                byte_val = digest[idx % len(digest)]
                vector[idx] += (byte_val - 128) / 128.0

        return normalize_vector(vector)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.batch_call_count += 1
        self.total_texts_embedded += len(texts)
        return [self._generate_deterministic_vector(text) for text in texts]


class OpenAIEmbeddingProvider(BaseEmbeddingProvider):
    """OpenAI embeddings provider using the official openai-python client."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("EMBEDDING_API_KEY") or os.environ.get("LLM_API_KEY")
        if not key:
            raise EnvironmentError("OpenAI API key missing for OpenAIEmbeddingProvider.")
        raw_model = model or os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
        self.model = raw_model.replace("openai/", "") if raw_model.startswith("openai/") else raw_model

        import openai
        self.client = openai.OpenAI(api_key=key)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self.client.embeddings.create(input=texts, model=self.model)
        return [item.embedding for item in response.data]


class GeminiEmbeddingProvider(BaseEmbeddingProvider):
    """Google Gemini embeddings provider using google.genai."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("EMBEDDING_API_KEY") or os.environ.get("LLM_API_KEY")
        if not key:
            raise EnvironmentError("Gemini API key missing for GeminiEmbeddingProvider.")
        raw_model = model or os.environ.get("EMBEDDING_MODEL", "text-embedding-004")
        self.model = raw_model.replace("gemini/", "") if raw_model.startswith("gemini/") else raw_model

        try:
            from google import genai
            self.client = genai.Client(api_key=key)
            self._use_new_sdk = True
        except ImportError:
            import google.generativeai as gai
            gai.configure(api_key=key)
            self._use_new_sdk = False

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        embeddings: list[list[float]] = []
        if self._use_new_sdk:
            for text in texts:
                resp = self.client.models.embed_content(
                    model=self.model,
                    contents=text,
                )
                embeddings.append(resp.embedding.values)
        else:
            import google.generativeai as gai
            for text in texts:
                resp = gai.embed_content(
                    model=f"models/{self.model}" if not self.model.startswith("models/") else self.model,
                    content=text,
                )
                embeddings.append(resp["embedding"])
        return embeddings


def get_embedding_provider(
    provider_name: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> BaseEmbeddingProvider:
    """Resolve and instantiate an embedding provider based on configuration or environment."""
    name = (provider_name or os.environ.get("EMBEDDING_PROVIDER") or os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if name == "mock":
        return MockEmbeddingProvider()
    has_openai_key = bool(os.environ.get("OPENAI_API_KEY") or (os.environ.get("LLM_PROVIDER") == "openai" and os.environ.get("LLM_API_KEY")))
    has_gemini_key = bool(os.environ.get("GEMINI_API_KEY") or (os.environ.get("LLM_PROVIDER") == "gemini" and os.environ.get("LLM_API_KEY")))

    if name == "openai" or (not name and has_openai_key):
        return OpenAIEmbeddingProvider(api_key=api_key, model=model)
    if name == "gemini" or (not name and has_gemini_key):
        return GeminiEmbeddingProvider(api_key=api_key, model=model)

    return MockEmbeddingProvider()


class SemanticSymbolIndex:
    """In-memory dense vector index for code symbols, computing embeddings exactly once."""

    def __init__(
        self,
        symbols: list[CodeSymbol],
        provider: BaseEmbeddingProvider,
        batch_size: int = 128,
    ) -> None:
        self.symbols = symbols
        self.provider = provider
        self.symbol_embeddings: list[list[float]] = []
        self.symbol_id_to_index: dict[str, int] = {}

        self._build_index(batch_size=batch_size)

    def _build_index(self, batch_size: int = 128) -> None:
        """Batch-generate embeddings for all symbols once."""
        if not self.symbols:
            return

        texts = [code_symbol_to_text(s) for s in self.symbols]
        for i in range(0, len(texts), batch_size):
            chunk = texts[i : i + batch_size]
            vectors = self.provider.embed_batch(chunk)
            for vec in vectors:
                self.symbol_embeddings.append(normalize_vector(vec))

        for idx, symbol in enumerate(self.symbols):
            self.symbol_id_to_index[symbol.id] = idx

    def query(
        self,
        entity_vector: list[float],
        top_n: int = 5,
        min_similarity: float = 0.50,
    ) -> list[tuple[CodeSymbol, float]]:
        """Retrieve top-N most similar code symbols for an entity query vector."""
        if not self.symbols or not self.symbol_embeddings or not entity_vector:
            return []

        norm_query = normalize_vector(entity_vector)
        scored: list[tuple[CodeSymbol, float]] = []

        for symbol, sym_vec in zip(self.symbols, self.symbol_embeddings):
            sim = cosine_similarity(norm_query, sym_vec)
            if sim >= min_similarity:
                scored.append((symbol, round(sim, 4)))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_n]


class SemanticCandidateRetriever:
    """High-level semantic retriever generating entity-to-symbol candidates via embeddings."""

    def __init__(
        self,
        provider: BaseEmbeddingProvider | None = None,
    ) -> None:
        self.provider = provider or get_embedding_provider()
        self._cache: dict[tuple[str, str], float] = {}

    def retrieve_candidates(
        self,
        entities: list[DocumentEntity],
        symbols: list[CodeSymbol],
        top_n: int = 5,
        min_similarity: float = 0.50,
    ) -> dict[str, list[tuple[CodeSymbol, float]]]:
        """
        Embed all symbols once and all entities once (M + N embeddings, NOT M × N).
        Returns mapping from entity.id to list of (CodeSymbol, similarity).
        """
        if not entities or not symbols:
            return {}

        # 1. Build index for all symbols (N embeddings generated once)
        symbol_index = SemanticSymbolIndex(symbols, self.provider)

        # 2. Batch embed all entities (M embeddings generated once)
        entity_texts = [document_entity_to_text(e) for e in entities]
        entity_vectors = self.provider.embed_batch(entity_texts)

        results: dict[str, list[tuple[CodeSymbol, float]]] = {}

        # 3. Retrieve top-N candidates per entity
        for entity, entity_vec in zip(entities, entity_vectors):
            matches = symbol_index.query(entity_vec, top_n=top_n, min_similarity=min_similarity)
            results[entity.id] = matches
            for sym, score in matches:
                self._cache[(entity.id, sym.id)] = score

        return results

    def get_similarity(self, entity: DocumentEntity, symbol: CodeSymbol) -> float | None:
        """Retrieve cached cosine similarity for an (entity, symbol) pair if available."""
        return self._cache.get((entity.id, symbol.id))
