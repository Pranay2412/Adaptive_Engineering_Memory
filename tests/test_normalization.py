"""Unit tests for the entity normalization layer."""

import unittest

from backend.normalization import (
    EntityKind,
    NormalizedEntity,
    are_compatible,
    compare_entities,
    detect_kind,
    normalize_entity,
    to_canonical,
    tokenize_name,
)


class TestEntityNormalization(unittest.TestCase):
    """Test suite covering all identifier normalization cases."""

    def test_prompt_five_examples_canonical_matching(self):
        """Verify the 5 prompt examples normalize to identical canonical and token representations."""
        examples = [
            "AuthService",
            "auth_service",
            "auth-service",
            "auth.service",
            "src/auth/AuthService",
        ]

        normalized = [normalize_entity(ex) for ex in examples]

        # 1. Original strings are strictly preserved
        for original, norm in zip(examples, normalized):
            self.assertEqual(norm.original, original)

        # 2. Base names are correctly extracted
        self.assertEqual(normalized[0].base_name, "AuthService")
        self.assertEqual(normalized[1].base_name, "auth_service")
        self.assertEqual(normalized[2].base_name, "auth-service")
        self.assertEqual(normalized[3].base_name, "auth.service")
        self.assertEqual(normalized[4].base_name, "AuthService")

        # 3. All 5 share the exact same base canonical name and tokens
        for norm in normalized:
            self.assertEqual(norm.canonical, "authservice")
            self.assertEqual(norm.tokens, ("auth", "service"))

        # 4. Pairwise canonical matching succeeds across all 5 combinations
        for i in range(len(normalized)):
            for j in range(len(normalized)):
                self.assertTrue(
                    normalized[i].canonical_match(normalized[j]),
                    f"Expected canonical match between {normalized[i].original} and {normalized[j].original}",
                )
                self.assertGreaterEqual(normalized[i].similarity(normalized[j]), 0.90)

    def test_camel_case_normalization(self):
        """Test PascalCase and lowerCamelCase identifiers with and without acronyms."""
        # PascalCase class names
        class_norm = normalize_entity("AuthService")
        self.assertEqual(class_norm.tokens, ("auth", "service"))
        self.assertEqual(class_norm.canonical, "authservice")
        self.assertIn(class_norm.kind, {EntityKind.CLASS, EntityKind.CAMEL_CASE})

        # lowerCamelCase function / variable names
        func_norm = normalize_entity("parseGraphifyGraph")
        self.assertEqual(func_norm.tokens, ("parse", "graphify", "graph"))
        self.assertEqual(func_norm.canonical, "parsegraphifygraph")
        self.assertEqual(func_norm.kind, EntityKind.CAMEL_CASE)

        # CamelCase with acronyms
        http_norm = normalize_entity("HTTPClient")
        self.assertEqual(http_norm.tokens, ("http", "client"))
        self.assertEqual(http_norm.canonical, "httpclient")

        xml_norm = normalize_entity("XMLHTTPRequest")
        self.assertEqual(xml_norm.tokens, ("xmlhttp", "request"))
        self.assertEqual(xml_norm.canonical, "xmlhttprequest")

        oauth_norm = normalize_entity("OAuth2Token")
        self.assertEqual(oauth_norm.tokens, ("o", "auth2", "token"))
        self.assertEqual(oauth_norm.canonical, "oauth2token")

    def test_snake_case_normalization(self):
        """Test standard, private, and dunder snake_case identifiers."""
        # Standard snake_case
        norm = normalize_entity("auth_service")
        self.assertEqual(norm.original, "auth_service")
        self.assertEqual(norm.tokens, ("auth", "service"))
        self.assertEqual(norm.canonical, "authservice")
        self.assertEqual(norm.kind, EntityKind.SNAKE_CASE)

        # Multi-word function name
        multi = normalize_entity("parse_graphify_graph")
        self.assertEqual(multi.tokens, ("parse", "graphify", "graph"))
        self.assertEqual(multi.canonical, "parsegraphifygraph")

        # Private / protected identifiers
        private_norm = normalize_entity("_private_helper")
        self.assertEqual(private_norm.tokens, ("private", "helper"))
        self.assertEqual(private_norm.canonical, "privatehelper")
        self.assertTrue(private_norm.metadata.get("is_private"))

        # Dunder methods
        dunder_norm = normalize_entity("__init__")
        self.assertEqual(dunder_norm.tokens, ("init",))
        self.assertEqual(dunder_norm.canonical, "init")
        self.assertTrue(dunder_norm.metadata.get("is_dunder"))

    def test_kebab_case_normalization(self):
        """Test kebab-case identifiers commonly used in packages, services, and URLs."""
        norm = normalize_entity("auth-service")
        self.assertEqual(norm.original, "auth-service")
        self.assertEqual(norm.tokens, ("auth", "service"))
        self.assertEqual(norm.canonical, "authservice")
        self.assertEqual(norm.kind, EntityKind.KEBAB_CASE)

        gw = normalize_entity("api-v1-gateway")
        self.assertEqual(gw.tokens, ("api", "v1", "gateway"))
        self.assertEqual(gw.canonical, "apiv1gateway")
        self.assertEqual(gw.kind, EntityKind.KEBAB_CASE)

    def test_dotted_names_normalization(self):
        """Test dotted names representing service names, configurations, or packages."""
        # Dotted service name
        service_norm = normalize_entity("auth.service")
        self.assertEqual(service_norm.tokens, ("auth", "service"))
        self.assertEqual(service_norm.canonical, "authservice")
        self.assertEqual(service_norm.kind, EntityKind.DOTTED)

        # Dotted fully qualified class name
        fq_class = normalize_entity("backend.models.CodeSymbol")
        self.assertEqual(fq_class.original, "backend.models.CodeSymbol")
        self.assertEqual(fq_class.base_name, "CodeSymbol")
        self.assertEqual(fq_class.canonical, "codesymbol")
        self.assertEqual(fq_class.tokens, ("code", "symbol"))
        self.assertEqual(fq_class.qualifiers, ("backend", "models"))
        self.assertEqual(fq_class.qualifier_tokens, ("backend", "models"))
        self.assertEqual(fq_class.all_tokens, ("backend", "models", "code", "symbol"))
        self.assertEqual(fq_class.canonical_full, "backendmodelscodesymbol")

        # Canonical match between base class and fully qualified class
        simple_class = normalize_entity("CodeSymbol")
        self.assertTrue(simple_class.canonical_match(fq_class))

    def test_namespace_qualified_names(self):
        """Test C++/Rust/PHP style namespace-qualified identifiers (::)."""
        ns_norm = normalize_entity("auth::service")
        self.assertEqual(ns_norm.original, "auth::service")
        self.assertEqual(ns_norm.base_name, "service")
        self.assertEqual(ns_norm.canonical, "service")
        self.assertEqual(ns_norm.canonical_full, "authservice")
        self.assertEqual(ns_norm.qualifiers, ("auth",))
        self.assertEqual(ns_norm.tokens, ("service",))
        self.assertEqual(ns_norm.all_tokens, ("auth", "service"))
        self.assertEqual(ns_norm.kind, EntityKind.NAMESPACE)

        # Cross-scope canonical match with auth_service
        snake = normalize_entity("auth_service")
        self.assertTrue(ns_norm.canonical_match(snake))

        cpp_norm = normalize_entity("std::vector")
        self.assertEqual(cpp_norm.base_name, "vector")
        self.assertEqual(cpp_norm.qualifiers, ("std",))
        self.assertEqual(cpp_norm.canonical, "vector")

    def test_file_paths_normalization(self):
        """Test POSIX, Windows, and relative file paths with source extensions."""
        # Simple source path
        path_norm = normalize_entity("src/auth/AuthService")
        self.assertEqual(path_norm.original, "src/auth/AuthService")
        self.assertEqual(path_norm.base_name, "AuthService")
        self.assertEqual(path_norm.canonical, "authservice")
        self.assertEqual(path_norm.tokens, ("auth", "service"))
        self.assertEqual(path_norm.qualifiers, ("src", "auth"))
        self.assertEqual(path_norm.all_tokens, ("src", "auth", "service"))
        self.assertEqual(path_norm.kind, EntityKind.FILE_PATH)

        # File path with .py extension
        ext_norm = normalize_entity("src/auth/AuthService.py")
        self.assertEqual(ext_norm.base_name, "AuthService")
        self.assertEqual(ext_norm.canonical, "authservice")
        self.assertEqual(ext_norm.tokens, ("auth", "service"))
        self.assertEqual(ext_norm.metadata.get("file_extension"), ".py")

        # Windows backslash path with drive letter
        win_norm = normalize_entity(r"C:\FYP\backend\models.py")
        self.assertEqual(win_norm.base_name, "models")
        self.assertEqual(win_norm.canonical, "models")
        self.assertEqual(win_norm.tokens, ("models",))
        self.assertEqual(win_norm.metadata.get("file_extension"), ".py")

        # Relative markdown doc path
        doc_norm = normalize_entity("./docs/auth/jwt_flow.md")
        self.assertEqual(doc_norm.base_name, "jwt_flow")
        self.assertEqual(doc_norm.canonical, "jwtflow")
        self.assertEqual(doc_norm.tokens, ("jwt", "flow"))
        self.assertEqual(doc_norm.metadata.get("file_extension"), ".md")

    def test_module_names_normalization(self):
        """Test module name normalization."""
        mod = normalize_entity("backend.pipeline", kind="module")
        self.assertEqual(mod.canonical_full, "backendpipeline")
        self.assertEqual(mod.all_tokens, ("backend", "pipeline"))
        self.assertEqual(mod.kind, EntityKind.MODULE)

        mod2 = normalize_entity("ingestion_docs", kind="module")
        self.assertEqual(mod2.tokens, ("ingestion", "docs"))
        self.assertEqual(mod2.canonical, "ingestiondocs")
        self.assertEqual(mod2.kind, EntityKind.MODULE)

    def test_class_names_normalization(self):
        """Test class name normalization and detection."""
        cls_norm = normalize_entity("Neo4jMemoryStore", kind="class")
        self.assertEqual(cls_norm.tokens, ("neo4j", "memory", "store"))
        self.assertEqual(cls_norm.canonical, "neo4jmemorystore")
        self.assertEqual(cls_norm.kind, EntityKind.CLASS)

        cls_auto = normalize_entity("CodeSymbol")
        self.assertEqual(cls_auto.tokens, ("code", "symbol"))
        self.assertEqual(cls_auto.canonical, "codesymbol")
        self.assertEqual(cls_auto.kind, EntityKind.CLASS)

    def test_function_names_normalization(self):
        """Test function names with and without call parentheses or argument lists."""
        func1 = normalize_entity("parse_graphify_graph()")
        self.assertEqual(func1.base_name, "parse_graphify_graph")
        self.assertEqual(func1.canonical, "parsegraphifygraph")
        self.assertEqual(func1.tokens, ("parse", "graphify", "graph"))
        self.assertEqual(func1.kind, EntityKind.FUNCTION)
        self.assertTrue(func1.metadata.get("is_function_call"))

        func2 = normalize_entity("get_user_by_id(user_id: int)")
        self.assertEqual(func2.base_name, "get_user_by_id")
        self.assertEqual(func2.canonical, "getuserbyid")
        self.assertEqual(func2.tokens, ("get", "user", "by", "id"))

        # Function without parens when explicitly specified
        func3 = normalize_entity("build_cross_links", kind="function")
        self.assertEqual(func3.tokens, ("build", "cross", "links"))
        self.assertEqual(func3.kind, EntityKind.FUNCTION)

    def test_api_names_normalization(self):
        """Test HTTP routes with verbs and parameterized endpoints."""
        # Route with HTTP method
        api_norm = normalize_entity("GET /api/v1/auth/service")
        self.assertEqual(api_norm.original, "GET /api/v1/auth/service")
        self.assertEqual(api_norm.base_name, "service")
        self.assertEqual(api_norm.canonical, "service")
        self.assertEqual(api_norm.metadata.get("http_method"), "GET")
        self.assertEqual(api_norm.kind, EntityKind.API_ROUTE)
        self.assertIn("auth", api_norm.all_tokens)
        self.assertIn("service", api_norm.all_tokens)

        # Route without explicit method
        route_norm = normalize_entity("/api/v1/users/{id}/profile")
        self.assertEqual(route_norm.kind, EntityKind.API_ROUTE)
        self.assertEqual(route_norm.base_name, "profile")
        self.assertIn("users", route_norm.all_tokens)
        self.assertIn("profile", route_norm.all_tokens)

    def test_comparison_and_similarity_methods(self):
        """Test equality, token Jaccard, overlap ratio, and similarity metrics."""
        e1 = normalize_entity("AuthService")
        e2 = normalize_entity("auth_service")
        e3 = normalize_entity("AuthManager")
        e4 = normalize_entity("UnrelatedComponent")

        # Exact match
        self.assertTrue(e1.exact_match("AuthService"))
        self.assertFalse(e1.exact_match(e2))

        # Canonical match
        self.assertTrue(e1.canonical_match(e2))
        self.assertFalse(e1.canonical_match(e3))

        # Token match
        self.assertTrue(e1.token_match(e2))
        self.assertFalse(e1.token_match(e3))

        # Token Jaccard
        # e1 tokens: ("auth", "service"), e3 tokens: ("auth", "manager") -> intersection=1, union=3 -> 1/3 = 0.333...
        self.assertAlmostEqual(e1.token_jaccard(e3), 1.0 / 3.0, places=3)
        self.assertEqual(e1.token_jaccard(e4), 0.0)

        # Token overlap ratio (containment)
        self.assertEqual(e1.token_overlap_ratio(e3), 0.5)

        # Graduated similarity score
        self.assertEqual(e1.similarity("AuthService"), 1.0)
        self.assertEqual(e1.similarity(e2), 0.95)
        self.assertGreater(e1.similarity(e3), 0.2)
        self.assertEqual(e1.similarity(e4), 0.0)

        # compare_entities utility
        report = compare_entities(e1, e2)
        self.assertTrue(report["canonical_match"])
        self.assertTrue(report["token_match"])
        self.assertEqual(report["shared_tokens"], ["auth", "service"])

        # are_compatible utility
        self.assertTrue(are_compatible(e1, e2, threshold=0.8))
        self.assertFalse(are_compatible(e1, e4, threshold=0.8))

    def test_edge_cases_and_immutability(self):
        """Test error handling, frozen dataclass immutability, and serialization."""
        # Empty string handling
        with self.assertRaises(ValueError):
            normalize_entity("")
        with self.assertRaises(ValueError):
            normalize_entity("   ")
        with self.assertRaises(ValueError):
            normalize_entity(None)  # type: ignore

        # Immutability & hashing
        norm = normalize_entity("AuthService")
        with self.assertRaises(AttributeError):
            norm.base_name = "Other"  # type: ignore

        # Hashable (can be stored in set or dict key)
        entity_set = {norm, normalize_entity("auth_service")}
        self.assertEqual(len(entity_set), 2)

        # Serialization to dictionary
        as_dict = norm.to_dict()
        self.assertEqual(as_dict["original"], "AuthService")
        self.assertEqual(as_dict["canonical"], "authservice")
        self.assertEqual(as_dict["tokens"], ["auth", "service"])


if __name__ == "__main__":
    unittest.main()
