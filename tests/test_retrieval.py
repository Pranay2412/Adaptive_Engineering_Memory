"""Unit tests for Adaptive Engineering Memory retrieval layer."""

import json
import unittest
from backend.models import (
    MemoryQueryResponse,
    RelationshipHop,
    RetrievalResult,
)
from backend.retrieval import (
    MemoryRetrievalService,
    query_engineering_memory,
)


class FakeResult:
    def __init__(self, records=None):
        self._records = records or []

    def consume(self):
        return None

    def __iter__(self):
        return iter(self._records)


class FakeSession:
    def __init__(self, queries, query_results=None):
        self.queries = queries
        self.query_results = query_results or {}
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.closed = True

    def run(self, query_str, **parameters):
        self.queries.append((query_str, parameters))
        for key, records in self.query_results.items():
            if key in query_str:
                if callable(records):
                    return FakeResult(records(query_str, parameters))
                return FakeResult(records)
        return FakeResult([])


class FakeDriver:
    def __init__(self, query_results=None):
        self.queries = []
        self.sessions = []
        self.query_results = query_results or {}

    def session(self):
        session = FakeSession(self.queries, self.query_results)
        self.sessions.append(session)
        return session

    def close(self):
        pass


class TestEngineeringMemoryRetrieval(unittest.TestCase):
    """Test suite for the Adaptive Engineering Memory retrieval service."""

    def test_query_term_extraction_and_tokenization(self):
        """Verify query term extraction splits identifiers and removes stop words."""
        service = MemoryRetrievalService(driver=FakeDriver())

        # Test CamelCase, snake_case, file extensions, and stop words
        query = "How does AuthService validate_user_token in auth.py?"
        terms = service.extract_query_terms(query)

        self.assertIn("authservice", terms)
        self.assertIn("auth", terms)
        self.assertIn("service", terms)
        self.assertIn("validate_user_token", terms)
        self.assertIn("validate", terms)
        self.assertIn("user", terms)
        self.assertIn("token", terms)
        self.assertIn("auth.py", terms)

        # Stop words should not be present
        self.assertNotIn("how", terms)
        self.assertNotIn("does", terms)
        self.assertNotIn("in", terms)

    def test_empty_or_whitespace_query_returns_empty_results(self):
        """Verify empty or blank queries gracefully return empty results."""
        driver = FakeDriver()
        service = MemoryRetrievalService(driver=driver)

        res1 = service.query(user_id="u1", repository_id="r1", query="")
        res2 = service.query(user_id="u1", repository_id="r1", query="   ")
        res3 = service.query(user_id="", repository_id="r1", query="auth")

        self.assertEqual(res1, [])
        self.assertEqual(res2, [])
        self.assertEqual(res3, [])
        # No DB queries should have been issued
        self.assertEqual(len(driver.queries), 0)

    def test_code_symbol_direct_search(self):
        """Verify direct search retrieves code symbols with rich metadata and normalized format."""
        mock_code_records = [
            {
                "c": {
                    "id": "sym_auth_svc",
                    "name": "AuthService",
                    "type": "class",
                    "file": "src/auth/service.py",
                    "qualified_name": "auth.service.AuthService",
                    "module": "auth.service",
                    "signature": "class AuthService(BaseService)",
                    "documentation": "Handles JWT authentication and user token issuance.",
                    "language": "Python",
                    "line_start": 15,
                    "line_end": 120,
                    "user_id": "user-42",
                    "repository_id": "repo-99",
                },
                "labels": ["CodeSymbol"],
            }
        ]

        driver = FakeDriver(query_results={"MATCH (c:CodeSymbol)": mock_code_records})
        service = MemoryRetrievalService(driver=driver)

        results = service.query(
            user_id="user-42",
            repository_id="repo-99",
            query="AuthService",
            include_traversal=False,
        )

        self.assertEqual(len(results), 1)
        res = results[0]
        self.assertEqual(res.entity, "AuthService")
        self.assertEqual(res.type, "CodeSymbol (class)")
        self.assertEqual(res.source, "code")
        self.assertEqual(res.repository, "repo-99")
        self.assertEqual(res.file, "src/auth/service.py")
        self.assertEqual(res.file_or_document, "src/auth/service.py")
        self.assertEqual(res.confidence, 1.0)
        self.assertGreaterEqual(res.score, 0.9)
        self.assertIn("AuthService", res.relevance_information)

        # Dictionary format must contain required prompt fields
        res_dict = res.to_dict()
        self.assertEqual(res_dict["entity"], "AuthService")
        self.assertEqual(res_dict["file/document"], "src/auth/service.py")
        self.assertEqual(res_dict["source"], "code")
        self.assertEqual(res_dict["repository"], "repo-99")

        # Verify query parameters included tenant scoping
        query_text, params = driver.queries[0]
        self.assertIn("c.user_id = $user_id", query_text)
        self.assertIn("c.repository_id = $repository_id", query_text)
        self.assertEqual(params["user_id"], "user-42")
        self.assertEqual(params["repository_id"], "repo-99")

    def test_document_entity_direct_search(self):
        """Verify direct search retrieves document entities with provenance context."""
        mock_doc_records = [
            {
                "d": {
                    "id": "doc_token_refresh",
                    "name": "TokenRefreshPolicy",
                    "type": "Requirement",
                    "description": "Refresh tokens expire after 30 days of inactivity.",
                    "source_document": "auth_spec.md",
                    "document_path": "docs/security/auth_spec.md",
                    "section": "Token Lifecycle",
                    "source_chunk": "Clients receive an access token valid for 15 mins and refresh token valid for 30 days.",
                    "dataset_id": "ds_security",
                    "user_id": "user-42",
                    "repository_id": "repo-99",
                },
                "labels": ["DocumentEntity"],
            }
        ]

        driver = FakeDriver(query_results={"MATCH (d:DocumentEntity)": mock_doc_records})
        service = MemoryRetrievalService(driver=driver)

        results = service.query(
            user_id="user-42",
            repository_id="repo-99",
            query="TokenRefreshPolicy",
            include_traversal=False,
        )

        self.assertEqual(len(results), 1)
        res = results[0]
        self.assertEqual(res.entity, "TokenRefreshPolicy")
        self.assertEqual(res.type, "DocumentEntity (Requirement)")
        self.assertEqual(res.source, "documentation")
        self.assertEqual(res.repository, "repo-99")
        self.assertEqual(res.document, "docs/security/auth_spec.md")
        self.assertEqual(res.file_or_document, "docs/security/auth_spec.md")
        self.assertIn("TokenRefreshPolicy", res.relevance_information)

        res_dict = res.to_dict()
        self.assertEqual(res_dict["file/document"], "docs/security/auth_spec.md")
        self.assertEqual(res_dict["source"], "documentation")

    def test_specifies_cross_linking_resolution(self):
        """Verify SPECIFIES relationship cross-links documentation concepts to implementing code symbols."""
        # 1. Document entity seed
        mock_doc_records = [
            {
                "d": {
                    "id": "ent_jwt_spec",
                    "name": "JWTAuthentication",
                    "type": "Requirement",
                    "document_path": "docs/auth.md",
                    "source_document": "auth.md",
                    "user_id": "u1",
                    "repository_id": "r1",
                },
                "labels": ["DocumentEntity"],
            }
        ]

        # 2. SPECIFIES traversal hop from DocumentEntity to CodeSymbol
        mock_traversal_records = [
            {
                "seed_id": "ent_jwt_spec",
                "seed_name": "JWTAuthentication",
                "seed_labels": ["DocumentEntity"],
                "rel_type": "SPECIFIES",
                "rel": {
                    "confidence": 0.94,
                    "matching_method": "lexical+semantic+context",
                    "explanation": "Document entity JWTAuthentication matches code class AuthService",
                },
                "is_outgoing": True,
                "target_id": "sym_auth_svc",
                "target_name": "AuthService",
                "target_labels": ["CodeSymbol"],
                "target_node": {
                    "id": "sym_auth_svc",
                    "name": "AuthService",
                    "type": "class",
                    "file": "src/auth/service.py",
                    "user_id": "u1",
                    "repository_id": "r1",
                },
            }
        ]

        query_results = {
            "MATCH (d:DocumentEntity)": mock_doc_records,
            "MATCH (c:CodeSymbol)": [],
            "MATCH (seed)-[r]-(target)": mock_traversal_records,
        }

        driver = FakeDriver(query_results=query_results)
        service = MemoryRetrievalService(driver=driver)

        results = service.query(
            user_id="u1",
            repository_id="r1",
            query="JWTAuthentication",
            max_hops=1,
            include_traversal=True,
        )

        # Should retrieve both the seed document entity AND the cross-linked code symbol
        self.assertEqual(len(results), 2)
        entities = {r.entity: r for r in results}

        # Check cross-linked code symbol
        code_res = entities["AuthService"]
        self.assertEqual(code_res.type, "CodeSymbol (class)")
        self.assertEqual(code_res.source, "code")
        self.assertEqual(code_res.file, "src/auth/service.py")
        self.assertEqual(code_res.confidence, 0.94)
        self.assertIn("SPECIFIES", code_res.relevance_information)

        # Verify relationship hop path
        self.assertEqual(len(code_res.relationship_path), 1)
        hop = code_res.relationship_path[0]
        self.assertEqual(hop.relation, "SPECIFIES")
        self.assertEqual(hop.source_name, "JWTAuthentication")
        self.assertEqual(hop.target_name, "AuthService")
        self.assertEqual(hop.confidence, 0.94)
        self.assertEqual(hop.matching_method, "lexical+semantic+context")
        self.assertIn("JWTAuthentication", hop.format_hop())

    def test_calls_relationship_traversal(self):
        """Verify CALLS relationship traversal links calling symbol to invoked function."""
        mock_code_records = [
            {
                "c": {
                    "id": "sym_auth_cls",
                    "name": "AuthService",
                    "type": "class",
                    "file": "auth.py",
                    "user_id": "u1",
                    "repository_id": "r1",
                },
                "labels": ["CodeSymbol"],
            }
        ]

        mock_traversal_records = [
            {
                "seed_id": "sym_auth_cls",
                "seed_name": "AuthService",
                "seed_labels": ["CodeSymbol"],
                "rel_type": "CALLS",
                "rel": {"confidence": 1.0},
                "is_outgoing": True,
                "target_id": "sym_verify_pw",
                "target_name": "verify_password_hash",
                "target_labels": ["CodeSymbol"],
                "target_node": {
                    "id": "sym_verify_pw",
                    "name": "verify_password_hash",
                    "type": "function",
                    "file": "src/crypto.py",
                    "user_id": "u1",
                    "repository_id": "r1",
                },
            }
        ]

        query_results = {
            "MATCH (c:CodeSymbol)": mock_code_records,
            "MATCH (d:DocumentEntity)": [],
            "MATCH (seed)-[r]-(target)": mock_traversal_records,
        }

        driver = FakeDriver(query_results=query_results)
        service = MemoryRetrievalService(driver=driver)

        results = service.query(
            user_id="u1",
            repository_id="r1",
            query="AuthService",
            max_hops=1,
        )

        entities = {r.entity: r for r in results}
        self.assertIn("verify_password_hash", entities)
        called_res = entities["verify_password_hash"]
        self.assertEqual(called_res.type, "CodeSymbol (function)")
        self.assertEqual(called_res.file, "src/crypto.py")
        self.assertIn("CALLS", called_res.relevance_information)

        hop = called_res.relationship_path[0]
        self.assertEqual(hop.relation, "CALLS")
        self.assertEqual(hop.source_name, "AuthService")
        self.assertEqual(hop.target_name, "verify_password_hash")

    def test_imports_relationship_traversal(self):
        """Verify IMPORTS relationship expansion discovers imported dependencies."""
        mock_code_records = [
            {
                "c": {
                    "id": "sym_app",
                    "name": "Application",
                    "type": "class",
                    "file": "main.py",
                    "user_id": "u1",
                    "repository_id": "r1",
                },
                "labels": ["CodeSymbol"],
            }
        ]

        mock_traversal_records = [
            {
                "seed_id": "sym_app",
                "seed_name": "Application",
                "seed_labels": ["CodeSymbol"],
                "rel_type": "IMPORTS",
                "rel": {},
                "is_outgoing": True,
                "target_id": "sym_jwt_pkg",
                "target_name": "jwt_helper",
                "target_labels": ["CodeSymbol"],
                "target_node": {
                    "id": "sym_jwt_pkg",
                    "name": "jwt_helper",
                    "type": "module",
                    "file": "lib/jwt_helper.py",
                    "user_id": "u1",
                    "repository_id": "r1",
                },
            }
        ]

        query_results = {
            "MATCH (c:CodeSymbol)": mock_code_records,
            "MATCH (d:DocumentEntity)": [],
            "MATCH (seed)-[r]-(target)": mock_traversal_records,
        }

        driver = FakeDriver(query_results=query_results)
        service = MemoryRetrievalService(driver=driver)

        results = service.query(user_id="u1", repository_id="r1", query="Application", max_hops=1)
        entities = {r.entity: r for r in results}

        self.assertIn("jwt_helper", entities)
        imported_res = entities["jwt_helper"]
        self.assertEqual(imported_res.relationship_path[0].relation, "IMPORTS")
        self.assertIn("IMPORTS", imported_res.relevance_information)

    def test_depends_on_relationship_traversal(self):
        """Verify DEPENDS_ON relationship expansion discovers structural module dependencies."""
        mock_code_records = [
            {
                "c": {
                    "id": "sym_payment",
                    "name": "PaymentService",
                    "type": "class",
                    "file": "payment.py",
                    "user_id": "u1",
                    "repository_id": "r1",
                },
                "labels": ["CodeSymbol"],
            }
        ]

        mock_traversal_records = [
            {
                "seed_id": "sym_payment",
                "seed_name": "PaymentService",
                "seed_labels": ["CodeSymbol"],
                "rel_type": "DEPENDS_ON",
                "rel": {"confidence": 1.0},
                "is_outgoing": True,
                "target_id": "sym_stripe_gw",
                "target_name": "StripeGateway",
                "target_labels": ["CodeSymbol"],
                "target_node": {
                    "id": "sym_stripe_gw",
                    "name": "StripeGateway",
                    "type": "class",
                    "file": "gateways/stripe.py",
                    "user_id": "u1",
                    "repository_id": "r1",
                },
            }
        ]

        query_results = {
            "MATCH (c:CodeSymbol)": mock_code_records,
            "MATCH (d:DocumentEntity)": [],
            "MATCH (seed)-[r]-(target)": mock_traversal_records,
        }

        driver = FakeDriver(query_results=query_results)
        service = MemoryRetrievalService(driver=driver)

        results = service.query(user_id="u1", repository_id="r1", query="PaymentService", max_hops=1)
        entities = {r.entity: r for r in results}

        self.assertIn("StripeGateway", entities)
        dep_res = entities["StripeGateway"]
        self.assertEqual(dep_res.relationship_path[0].relation, "DEPENDS_ON")
        self.assertIn("DEPENDS_ON", dep_res.relevance_information)

    def test_multi_hop_traversal_path_reconstruction(self):
        """Verify multi-hop traversal (SPECIFIES -> CALLS) constructs full 2-hop path."""
        # Query: "TokenExchange"
        # Seed: DocumentEntity "TokenExchange"
        # Hop 1: DocumentEntity(TokenExchange) -[:SPECIFIES]-> CodeSymbol(TokenManager)
        # Hop 2: CodeSymbol(TokenManager) -[:CALLS]-> CodeSymbol(issue_access_token)

        doc_seed = [
            {
                "d": {
                    "id": "doc_token_ex",
                    "name": "TokenExchange",
                    "type": "Requirement",
                    "document_path": "docs/tokens.md",
                    "user_id": "u1",
                    "repository_id": "r1",
                },
                "labels": ["DocumentEntity"],
            }
        ]

        def traversal_simulator(query, params):
            seed_ids = params.get("seed_ids", [])
            if "doc_token_ex" in seed_ids:
                # Hop 1 response
                return [
                    {
                        "seed_id": "doc_token_ex",
                        "seed_name": "TokenExchange",
                        "seed_labels": ["DocumentEntity"],
                        "rel_type": "SPECIFIES",
                        "rel": {"confidence": 0.95, "matching_method": "exact"},
                        "is_outgoing": True,
                        "target_id": "sym_tok_mgr",
                        "target_name": "TokenManager",
                        "target_labels": ["CodeSymbol"],
                        "target_node": {
                            "id": "sym_tok_mgr",
                            "name": "TokenManager",
                            "type": "class",
                            "file": "tokens.py",
                            "user_id": "u1",
                            "repository_id": "r1",
                        },
                    }
                ]
            elif "sym_tok_mgr" in seed_ids:
                # Hop 2 response
                return [
                    {
                        "seed_id": "sym_tok_mgr",
                        "seed_name": "TokenManager",
                        "seed_labels": ["CodeSymbol"],
                        "rel_type": "CALLS",
                        "rel": {"confidence": 1.0},
                        "is_outgoing": True,
                        "target_id": "sym_issue_fn",
                        "target_name": "issue_access_token",
                        "target_labels": ["CodeSymbol"],
                        "target_node": {
                            "id": "sym_issue_fn",
                            "name": "issue_access_token",
                            "type": "function",
                            "file": "tokens.py",
                            "user_id": "u1",
                            "repository_id": "r1",
                        },
                    }
                ]
            return []

        query_results = {
            "MATCH (d:DocumentEntity)": doc_seed,
            "MATCH (c:CodeSymbol)": [],
            "MATCH (seed)-[r]-(target)": traversal_simulator,
        }

        driver = FakeDriver(query_results=query_results)
        service = MemoryRetrievalService(driver=driver)

        results = service.query(
            user_id="u1",
            repository_id="r1",
            query="TokenExchange",
            max_hops=2,
            include_traversal=True,
        )

        entities = {r.entity: r for r in results}
        self.assertIn("TokenExchange", entities)
        self.assertIn("TokenManager", entities)
        self.assertIn("issue_access_token", entities)

        # Hop 1 node
        mgr_res = entities["TokenManager"]
        self.assertEqual(len(mgr_res.relationship_path), 1)
        self.assertEqual(mgr_res.relationship_path[0].relation, "SPECIFIES")
        self.assertEqual(mgr_res.relationship_path[0].source_name, "TokenExchange")
        self.assertEqual(mgr_res.relationship_path[0].target_name, "TokenManager")

        # Hop 2 node (2 hops deep)
        fn_res = entities["issue_access_token"]
        self.assertEqual(len(fn_res.relationship_path), 2)
        hop1 = fn_res.relationship_path[0]
        hop2 = fn_res.relationship_path[1]

        self.assertEqual(hop1.relation, "SPECIFIES")
        self.assertEqual(hop1.source_name, "TokenExchange")
        self.assertEqual(hop1.target_name, "TokenManager")

        self.assertEqual(hop2.relation, "CALLS")
        self.assertEqual(hop2.source_name, "TokenManager")
        self.assertEqual(hop2.target_name, "issue_access_token")

    def test_min_confidence_filter(self):
        """Verify min_confidence parameter filters out low-confidence edges."""
        mock_doc = [
            {
                "d": {
                    "id": "d1",
                    "name": "WeakConcept",
                    "type": "Concept",
                    "user_id": "u1",
                    "repository_id": "r1",
                },
                "labels": ["DocumentEntity"],
            }
        ]

        mock_traversal = [
            {
                "seed_id": "d1",
                "seed_name": "WeakConcept",
                "seed_labels": ["DocumentEntity"],
                "rel_type": "SPECIFIES",
                "rel": {"confidence": 0.40},  # Below 0.70 threshold
                "is_outgoing": True,
                "target_id": "s1",
                "target_name": "UnrelatedSymbol",
                "target_labels": ["CodeSymbol"],
                "target_node": {"id": "s1", "name": "UnrelatedSymbol", "type": "function"},
            }
        ]

        query_results = {
            "MATCH (d:DocumentEntity)": mock_doc,
            "MATCH (c:CodeSymbol)": [],
            "MATCH (seed)-[r]-(target)": mock_traversal,
        }

        driver = FakeDriver(query_results=query_results)
        service = MemoryRetrievalService(driver=driver)

        results = service.query(
            user_id="u1",
            repository_id="r1",
            query="WeakConcept",
            min_confidence=0.70,
            max_hops=1,
        )

        entities = [r.entity for r in results]
        self.assertIn("WeakConcept", entities)
        self.assertNotIn("UnrelatedSymbol", entities)

    def test_query_response_wrapper_container(self):
        """Verify query_response returns rich MemoryQueryResponse container."""
        driver = FakeDriver()
        service = MemoryRetrievalService(driver=driver)

        response = service.query_response(
            user_id="u1",
            repository_id="r1",
            query="Authentication tokens",
        )

        self.assertIsInstance(response, MemoryQueryResponse)
        self.assertEqual(response.query, "Authentication tokens")
        self.assertEqual(response.user_id, "u1")
        self.assertEqual(response.repository_id, "r1")
        self.assertIn("authentication", response.query_terms)
        self.assertIn("tokens", response.query_terms)

        resp_dict = response.to_dict()
        self.assertEqual(resp_dict["query"], "Authentication tokens")
        self.assertIsInstance(resp_dict["results"], list)

    def test_convenience_function_query_engineering_memory(self):
        """Verify standalone function query_engineering_memory interface."""
        driver = FakeDriver()
        from backend.db_loader import Neo4jMemoryStore

        store = Neo4jMemoryStore(driver=driver)
        results = query_engineering_memory(
            user_id="user_dev",
            repository_id="repo_core",
            query="Validate session",
            store=store,
        )
        self.assertIsInstance(results, list)


if __name__ == "__main__":
    unittest.main()
