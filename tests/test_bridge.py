"""Unit tests for the multi-stage candidate matching bridge."""

import unittest

from backend.bridge import (
    BridgeConfig,
    build_cross_links,
    compute_context_score,
    compute_lexical_score,
    compute_type_compatibility,
)
from backend.models import CodeSymbol, DocumentEntity
from backend.normalization import normalize_entity


class TestMultiStageBridge(unittest.TestCase):
    """Test suite covering the multi-stage candidate matching engine in backend/bridge.py."""

    def test_exact_name_matching_preserved(self):
        """Verify exact name matching achieves high confidence and valid metadata."""
        entity = DocumentEntity("e1", "AuthService", "Requirement")
        symbol = CodeSymbol("s1", "AuthService", "backend/auth/service.py", "class")

        links = build_cross_links([entity], [symbol])
        self.assertEqual(len(links), 1)

        link = links[0]
        self.assertEqual(link.source_id, "e1")
        self.assertEqual(link.target_id, "s1")
        self.assertEqual(link.relation, "SPECIFIES")

        metadata = link.metadata
        self.assertGreaterEqual(metadata["confidence"], 0.90)
        self.assertEqual(metadata["lexical_score"], 1.0)
        self.assertIn("matching_method", metadata)
        self.assertIn("explanation", metadata)
        self.assertIn("AuthService", metadata["explanation"])

    def test_exact_mention_in_sentence_preserved(self):
        """Verify entity text mentioning the exact symbol name is successfully matched."""
        entity = DocumentEntity("e2", "The AuthService must validate tokens", "Requirement")
        symbol = CodeSymbol("s2", "AuthService", "auth.py", "class")

        links = build_cross_links([entity], [symbol])
        self.assertEqual(len(links), 1)
        self.assertGreaterEqual(links[0].metadata["confidence"], 0.85)
        self.assertGreaterEqual(links[0].metadata["lexical_score"], 0.95)

    def test_normalized_identifier_matching(self):
        """Verify snake_case, kebab-case, and dotted names match PascalCase symbols."""
        entities = [
            DocumentEntity("e_snake", "auth_service", "Requirement"),
            DocumentEntity("e_kebab", "auth-service", "Requirement"),
            DocumentEntity("e_dot", "auth.service", "Requirement"),
        ]
        symbol = CodeSymbol("s_class", "AuthService", "backend/auth/service.py", "class")

        for entity in entities:
            links = build_cross_links([entity], [symbol])
            self.assertEqual(len(links), 1, f"Failed for {entity.name}")
            metadata = links[0].metadata
            self.assertGreaterEqual(metadata["confidence"], 0.85)
            self.assertGreaterEqual(metadata["lexical_score"], 0.90)
            self.assertIn("normalized", metadata["explanation"].lower())

    def test_contextual_matching_repository_scoping(self):
        """Verify repository isolation rejects cross-repository matches and accepts same-repo."""
        e_same = DocumentEntity("e1", "AuthService", "Requirement", repository_id="repo-a")
        e_diff = DocumentEntity("e2", "AuthService", "Requirement", repository_id="repo-b")
        symbol = CodeSymbol("s1", "AuthService", "backend/auth.py", "class", repository_id="repo-a")

        # Same repo -> accepted with high confidence
        links_same = build_cross_links([e_same], [symbol])
        self.assertEqual(len(links_same), 1)
        self.assertGreaterEqual(links_same[0].metadata["context_score"], 0.80)

        # Different repo -> rejected
        links_diff = build_cross_links([e_diff], [symbol])
        self.assertEqual(len(links_diff), 0)

    def test_contextual_matching_module_and_file_information(self):
        """Verify file and module keywords boost context score and appear in explanation."""
        entity = DocumentEntity("e1", "AuthService", "Requirement")
        symbol_in_auth = CodeSymbol("s1", "AuthService", "src/auth/service.py", "class")
        symbol_in_other = CodeSymbol("s2", "AuthService", "src/billing/service.py", "class")

        links_auth = build_cross_links([entity], [symbol_in_auth])
        links_other = build_cross_links([entity], [symbol_in_other])

        self.assertEqual(len(links_auth), 1)
        self.assertEqual(len(links_other), 1)

        # Symbol in auth module has higher context score and mentions auth module in explanation
        self.assertGreater(links_auth[0].metadata["context_score"], links_other[0].metadata["context_score"])
        self.assertIn("auth module", links_auth[0].metadata["explanation"])

    def test_entity_type_compatibility(self):
        """Verify type compatibility scoring reflects architectural alignment."""
        # Requirement aligns strongly with class and function
        req_class = compute_type_compatibility("Requirement", "class")
        req_func = compute_type_compatibility("Requirement", "function")
        req_var = compute_type_compatibility("Requirement", "variable")
        self.assertGreater(req_class, req_var)
        self.assertGreater(req_func, req_var)

        # Architecture aligns with package/module over function
        arch_mod = compute_type_compatibility("Architecture", "module")
        arch_func = compute_type_compatibility("Architecture", "function")
        self.assertGreater(arch_mod, arch_func)

    def test_semantic_similarity_optional_stage(self):
        """Verify optional semantic_scorer affects confidence and appears in metadata."""
        entity = DocumentEntity("e1", "AuthService", "Requirement")
        symbol = CodeSymbol("s1", "AuthService", "backend/auth/service.py", "class")

        # 1. Without semantic scorer: semantic_score key is omitted
        links_no_sem = build_cross_links([entity], [symbol])
        self.assertEqual(len(links_no_sem), 1)
        self.assertNotIn("semantic_score", links_no_sem[0].metadata)

        # 2. With semantic scorer: semantic_score is included and matching_method reflects it
        def fake_semantic_scorer(e, s):
            return 0.88

        links_with_sem = build_cross_links([entity], [symbol], semantic_scorer=fake_semantic_scorer)
        self.assertEqual(len(links_with_sem), 1)
        metadata = links_with_sem[0].metadata

        self.assertEqual(metadata["semantic_score"], 0.88)
        self.assertIn("semantic", metadata["matching_method"])
        self.assertIn("lexical", metadata["matching_method"])
        self.assertIn("context", metadata["matching_method"])

    def test_candidate_generation_is_cheap_and_rejects_disjoint_pairs(self):
        """Verify disjoint entities and symbols are pruned early without cross-linking."""
        entity = DocumentEntity("e1", "PaymentGateway", "Requirement")
        symbol = CodeSymbol("s1", "render_template", "views.py", "function")

        links = build_cross_links([entity], [symbol])
        self.assertEqual(len(links), 0)

    def test_llm_verification_selective_execution(self):
        """Verify LLM is only called for ambiguous candidates and never for clear matches or non-matches."""
        calls = []

        def mock_llm_verifier(e, s):
            calls.append((e.id, s.id))
            return True, 0.85, "Confirmed semantic requirement link"

        # 1. Clear high-confidence match (score >= 0.75): LLM must NOT be called
        e_clear = DocumentEntity("e_clear", "AuthService", "Requirement")
        s_clear = CodeSymbol("s_clear", "AuthService", "auth.py", "class")
        links_clear = build_cross_links([e_clear], [s_clear], llm_verifier=mock_llm_verifier)

        self.assertEqual(len(links_clear), 1)
        self.assertEqual(len(calls), 0, "LLM should not be called for high-confidence match")

        # 2. Clear non-match: LLM must NOT be called
        e_non = DocumentEntity("e_non", "PaymentGateway", "Requirement")
        s_non = CodeSymbol("s_non", "unrelated_tool", "util.py", "function")
        links_non = build_cross_links([e_non], [s_non], llm_verifier=mock_llm_verifier)

        self.assertEqual(len(links_non), 0)
        self.assertEqual(len(calls), 0, "LLM should not be called for disjoint non-match")

        # 3. Ambiguous candidate with partial token overlap in borderline band
        e_ambig = DocumentEntity("e_ambig", "Session Cache", "Architecture")
        s_ambig = CodeSymbol("s_ambig", "session_cache_manager", "cache.py", "class")

        links_ambig = build_cross_links([e_ambig], [s_ambig], llm_verifier=mock_llm_verifier)
        # LLM was invoked for the ambiguous candidate
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ("e_ambig", "s_ambig"))
        if links_ambig:
            self.assertIn("llm", links_ambig[0].metadata["matching_method"])
            self.assertIn("LLM verification", links_ambig[0].metadata["explanation"])

    def test_metadata_schema_matches_expected_specification(self):
        """Verify metadata conforms exactly to the required specification fields."""
        entity = DocumentEntity("e1", "AuthService", "Requirement")
        symbol = CodeSymbol("s1", "AuthService", "backend/auth/service.py", "class")

        def dummy_semantic(e, s):
            return 0.88

        links = build_cross_links([entity], [symbol], semantic_scorer=dummy_semantic)
        self.assertEqual(len(links), 1)
        meta = links[0].metadata

        # Required fields check
        self.assertIn("confidence", meta)
        self.assertIn("matching_method", meta)
        self.assertIn("lexical_score", meta)
        self.assertIn("semantic_score", meta)
        self.assertIn("context_score", meta)
        self.assertIn("explanation", meta)

        self.assertIsInstance(meta["confidence"], float)
        self.assertIsInstance(meta["lexical_score"], float)
        self.assertIsInstance(meta["semantic_score"], float)
        self.assertIsInstance(meta["context_score"], float)
        self.assertIsInstance(meta["matching_method"], str)
        self.assertIsInstance(meta["explanation"], str)

    def test_determinism_and_sorting(self):
        """Verify build_cross_links produces identical results and ordering on repeated runs."""
        entities = [
            DocumentEntity("e2", "UserSession", "Requirement"),
            DocumentEntity("e1", "AuthService", "Requirement"),
        ]
        symbols = [
            CodeSymbol("s2", "UserSession", "session.py", "class"),
            CodeSymbol("s1", "AuthService", "auth.py", "class"),
        ]

        fixed_time = "2026-01-01T00:00:00+00:00"
        run1 = build_cross_links(entities, symbols, created_at=fixed_time)
        run2 = build_cross_links(entities, symbols, created_at=fixed_time)

        self.assertEqual(len(run1), 2)
        self.assertEqual(len(run2), 2)

        # Consistent deterministic sorting by (source_id, target_id)
        self.assertEqual([link.source_id for link in run1], ["e1", "e2"])
        self.assertEqual([link.source_id for link in run2], ["e1", "e2"])
        self.assertEqual(run1[0].metadata, run2[0].metadata)
        self.assertEqual(run1[1].metadata, run2[1].metadata)
        self.assertEqual(run1[0].metadata["created_at"], fixed_time)


if __name__ == "__main__":
    unittest.main()
