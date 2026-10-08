"""Unit tests for semantic candidate retrieval and embedding-based matching."""

import os
import unittest
from unittest.mock import patch

from backend.bridge import build_cross_links
from backend.models import CodeSymbol, DocumentEntity
from backend.semantic_matcher import (
    BaseEmbeddingProvider,
    GeminiEmbeddingProvider,
    MockEmbeddingProvider,
    OpenAIEmbeddingProvider,
    SemanticCandidateRetriever,
    SemanticSymbolIndex,
    code_symbol_to_text,
    cosine_similarity,
    document_entity_to_text,
    get_embedding_provider,
    normalize_vector,
)


class TestSemanticMatcher(unittest.TestCase):
    """Test suite covering vector math, embedding providers, semantic indexing, and candidate retrieval."""

    def setUp(self):
        # Known concept vectors for the prompt's 3 domain examples:
        # 1. 'authentication manager' vs 'AuthService'
        # 2. 'payment processor' vs 'StripePaymentHandler'
        # 3. 'database access layer' vs 'UserRepository'
        self.concept_vectors = {
            "authentication manager": [1.0, 0.0, 0.0, 0.1],
            "authservice": [0.95, 0.05, 0.0, 0.1],
            "payment processor": [0.0, 1.0, 0.0, 0.1],
            "stripepaymenthandler": [0.05, 0.95, 0.0, 0.1],
            "database access layer": [0.0, 0.0, 1.0, 0.1],
            "userrepository": [0.0, 0.05, 0.95, 0.1],
            "unrelated": [0.0, 0.0, 0.0, 1.0],
        }
        self.mock_provider = MockEmbeddingProvider(concept_vectors=self.concept_vectors, dimension=4)

    def test_vector_normalization_and_cosine_similarity(self):
        """Test unit vector normalization and cosine similarity behavior."""
        v1 = [3.0, 4.0]
        norm_v1 = normalize_vector(v1)
        self.assertAlmostEqual(norm_v1[0], 0.6)
        self.assertAlmostEqual(norm_v1[1], 0.8)

        # Zero vector handling (must not raise ZeroDivisionError)
        self.assertEqual(normalize_vector([0.0, 0.0]), [0.0, 0.0])
        self.assertEqual(cosine_similarity([0.0, 0.0], [1.0, 1.0]), 0.0)

        # Parallel, orthogonal, and opposite vectors
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [2.0, 0.0]), 1.0)
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0)
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [-1.0, 0.0]), -1.0)

    def test_text_serialization_includes_metadata_context(self):
        """Verify text serialization incorporates names, types, files, descriptions, and docstrings."""
        entity = DocumentEntity("e1", "AuthManager", "Requirement")
        # Add metadata / attributes
        entity_text = document_entity_to_text(entity)
        self.assertIn("AuthManager", entity_text)
        self.assertIn("Requirement", entity_text)

        symbol = CodeSymbol("s1", "AuthService", "backend/auth/service.py", "class")
        symbol_text = code_symbol_to_text(symbol)
        self.assertIn("AuthService", symbol_text)
        self.assertIn("class", symbol_text)
        self.assertIn("backend/auth/service.py", symbol_text)

    def test_embeddings_computed_once_per_entity_and_symbol_not_cartesian(self):
        """Verify embeddings are computed M + N times (linear), strictly avoiding M × N calls."""
        provider = MockEmbeddingProvider(concept_vectors=self.concept_vectors, dimension=4)

        entities = [
            DocumentEntity("e1", "authentication manager", "Requirement"),
            DocumentEntity("e2", "payment processor", "Requirement"),
            DocumentEntity("e3", "database access layer", "Requirement"),
        ]

        symbols = [
            CodeSymbol("s1", "AuthService", "src/auth.py", "class"),
            CodeSymbol("s2", "StripePaymentHandler", "src/billing.py", "class"),
            CodeSymbol("s3", "UserRepository", "src/db.py", "class"),
            CodeSymbol("s4", "UnrelatedTool", "src/util.py", "function"),
        ]

        retriever = SemanticCandidateRetriever(provider=provider)
        _results = retriever.retrieve_candidates(entities, symbols, top_n=3)

        num_entities = len(entities)
        num_symbols = len(symbols)
        cartesian_product = num_entities * num_symbols  # 12

        # Provider must execute exactly 2 batch operations (one for symbols, one for entities)
        self.assertEqual(provider.batch_call_count, 2)
        # Total texts embedded must equal M + N = 7, NOT M * N = 12
        self.assertEqual(provider.total_texts_embedded, num_entities + num_symbols)
        self.assertLess(provider.total_texts_embedded, cartesian_product)

    def test_three_domain_examples_semantic_retrieval(self):
        """Verify the 3 prompt examples retrieve the correct code symbols using dense embeddings."""
        entities = [
            DocumentEntity("e1", "authentication manager", "Requirement"),
            DocumentEntity("e2", "payment processor", "Requirement"),
            DocumentEntity("e3", "database access layer", "Requirement"),
        ]

        symbols = [
            CodeSymbol("s1", "AuthService", "src/auth/service.py", "class"),
            CodeSymbol("s2", "StripePaymentHandler", "src/billing/stripe.py", "class"),
            CodeSymbol("s3", "UserRepository", "src/db/repo.py", "class"),
            CodeSymbol("s4", "UnrelatedTool", "src/util/tool.py", "function"),
        ]

        retriever = SemanticCandidateRetriever(provider=self.mock_provider)
        results = retriever.retrieve_candidates(entities, symbols, top_n=1, min_similarity=0.70)

        # 1. "authentication manager" -> "AuthService"
        self.assertIn("e1", results)
        self.assertEqual(results["e1"][0][0].name, "AuthService")
        self.assertGreaterEqual(results["e1"][0][1], 0.90)

        # 2. "payment processor" -> "StripePaymentHandler"
        self.assertIn("e2", results)
        self.assertEqual(results["e2"][0][0].name, "StripePaymentHandler")
        self.assertGreaterEqual(results["e2"][0][1], 0.90)

        # 3. "database access layer" -> "UserRepository"
        self.assertIn("e3", results)
        self.assertEqual(results["e3"][0][0].name, "UserRepository")
        self.assertGreaterEqual(results["e3"][0][1], 0.90)

    def test_semantic_bridge_creates_specifies_edges_for_different_terminology(self):
        """Verify build_cross_links creates SPECIFIES edges when semantic similarity is high despite 0 lexical overlap."""
        entities = [
            DocumentEntity("e1", "authentication manager", "Requirement"),
            DocumentEntity("e2", "payment processor", "Requirement"),
            DocumentEntity("e3", "database access layer", "Requirement"),
        ]

        symbols = [
            CodeSymbol("s1", "AuthService", "src/auth/service.py", "class"),
            CodeSymbol("s2", "StripePaymentHandler", "src/billing/stripe.py", "class"),
            CodeSymbol("s3", "UserRepository", "src/db/repo.py", "class"),
            CodeSymbol("s4", "UnrelatedTool", "src/util/tool.py", "function"),
        ]

        links = build_cross_links(entities, symbols, embedding_provider=self.mock_provider)

        # 3 SPECIFIES edges created; UnrelatedTool is omitted
        self.assertEqual(len(links), 3)

        source_target_pairs = {(l.source_id, l.target_id) for l in links}
        self.assertIn(("e1", "s1"), source_target_pairs)
        self.assertIn(("e2", "s2"), source_target_pairs)
        self.assertIn(("e3", "s3"), source_target_pairs)

        for link in links:
            self.assertEqual(link.relation, "SPECIFIES")
            metadata = link.metadata
            self.assertGreaterEqual(metadata["confidence"], 0.75)
            self.assertIn("semantic", metadata["matching_method"])
            self.assertIn("semantic_score", metadata)
            self.assertGreaterEqual(metadata["semantic_score"], 0.90)
            self.assertIn("semantically matches", metadata["explanation"])

    def test_provider_configuration_resolution(self):
        """Test get_embedding_provider resolution for mock, openai, and gemini."""
        # 1. Explicit mock provider
        mock_p = get_embedding_provider(provider_name="mock")
        self.assertIsInstance(mock_p, MockEmbeddingProvider)

        # 2. OpenAI provider error when key missing
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(EnvironmentError):
                OpenAIEmbeddingProvider()

        # 3. Gemini provider error when key missing
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(EnvironmentError):
                GeminiEmbeddingProvider()

        # 4. Fallback to MockEmbeddingProvider when no credentials exist
        with patch.dict(os.environ, {}, clear=True):
            fallback_p = get_embedding_provider()
            self.assertIsInstance(fallback_p, MockEmbeddingProvider)


if __name__ == "__main__":
    unittest.main()
