"""Unit tests for the enriched normalized Graphify representation (CodeSymbol)."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from backend.db_loader import Neo4jMemoryStore
from backend.ingestion_code import (
    _infer_language,
    _infer_module_and_package,
    _parse_documentation,
    _parse_line_range,
    _parse_signature,
    parse_graphify_graph,
)
from backend.models import CodeSymbol, DocumentEntity, GraphEdge, scoped_id
from backend.semantic_matcher import code_symbol_to_text


class FakeResult:
    def consume(self):
        return None


class FakeTransaction:
    def __init__(self, queries):
        self.queries = queries

    def run(self, query, **parameters):
        self.queries.append((query, parameters))
        return FakeResult()


class FakeSession:
    def __init__(self, queries):
        self.queries = queries
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.closed = True

    def execute_write(self, callback):
        return callback(FakeTransaction(self.queries))

    def run(self, query, **parameters):
        self.queries.append((query, parameters))
        return FakeResult()


class FakeDriver:
    def __init__(self):
        self.queries = []
        self.sessions = []

    def session(self):
        session = FakeSession(self.queries)
        self.sessions.append(session)
        return session

    def close(self):
        pass


class TestCodeSymbolRepresentation(unittest.TestCase):
    """Test suite covering CodeSymbol model, parsing, serialization, and persistence."""

    def test_dataclass_fields_defaults_and_post_init(self):
        """Test default values and mutual synchronization between file and source_file."""
        # 1. Minimal instantiation with file
        sym1 = CodeSymbol(id="s1", name="AuthService", file="src/auth.py", type="class")
        self.assertEqual(sym1.id, "s1")
        self.assertEqual(sym1.name, "AuthService")
        self.assertEqual(sym1.file, "src/auth.py")
        self.assertEqual(sym1.source_file, "src/auth.py")
        self.assertIsNone(sym1.qualified_name)
        self.assertIsNone(sym1.module)
        self.assertIsNone(sym1.package)
        self.assertIsNone(sym1.line_start)
        self.assertIsNone(sym1.line_end)
        self.assertIsNone(sym1.parent_symbol)
        self.assertIsNone(sym1.signature)
        self.assertIsNone(sym1.documentation)
        self.assertIsNone(sym1.language)
        self.assertIsNone(sym1.user_id)
        self.assertIsNone(sym1.repository_id)

        # 2. Instantiation with source_file only
        sym2 = CodeSymbol(id="s2", name="login", file="", type="function", source_file="src/auth.py")
        self.assertEqual(sym2.file, "src/auth.py")
        self.assertEqual(sym2.source_file, "src/auth.py")

        # 3. Full instantiation with all fields
        sym3 = CodeSymbol(
            id="s3",
            name="validate_token",
            file="src/auth/service.py",
            type="method",
            user_id="user-1",
            repository_id="repo-1",
            qualified_name="src.auth.service.AuthService.validate_token",
            module="src.auth.service",
            package="src.auth",
            source_file="src/auth/service.py",
            line_start=30,
            line_end=45,
            parent_symbol="AuthService",
            signature="(token: str) -> bool",
            documentation="Validates JWT signature and expiration.",
            language="python",
        )
        self.assertEqual(sym3.qualified_name, "src.auth.service.AuthService.validate_token")
        self.assertEqual(sym3.module, "src.auth.service")
        self.assertEqual(sym3.package, "src.auth")
        self.assertEqual(sym3.line_start, 30)
        self.assertEqual(sym3.line_end, 45)
        self.assertEqual(sym3.parent_symbol, "AuthService")
        self.assertEqual(sym3.signature, "(token: str) -> bool")
        self.assertEqual(sym3.documentation, "Validates JWT signature and expiration.")
        self.assertEqual(sym3.language, "python")

    def test_serialization_to_dict_and_from_dict(self):
        """Verify to_dict and from_dict round-trip lossless serialization."""
        original = CodeSymbol(
            id="s_full",
            name="handle_payment",
            file="billing/processor.py",
            type="function",
            user_id="u123",
            repository_id="repo456",
            qualified_name="billing.processor.handle_payment",
            module="billing.processor",
            package="billing",
            source_file="billing/processor.py",
            line_start=15,
            line_end=55,
            parent_symbol="PaymentGateway",
            signature="(amount: float, currency: str) -> PaymentResult",
            documentation="Process payment transaction via Stripe API.",
            language="python",
        )

        data = original.to_dict()
        self.assertEqual(data["id"], "s_full")
        self.assertEqual(data["name"], "handle_payment")
        self.assertEqual(data["qualified_name"], "billing.processor.handle_payment")
        self.assertEqual(data["line_start"], 15)
        self.assertEqual(data["line_end"], 55)
        self.assertEqual(data["signature"], "(amount: float, currency: str) -> PaymentResult")

        # JSON round-trip
        json_str = json.dumps(data)
        reconstituted_dict = json.loads(json_str)
        reconstituted = CodeSymbol.from_dict(reconstituted_dict)

        self.assertEqual(reconstituted, original)

    def test_from_dict_with_missing_optional_fields(self):
        """Verify from_dict gracefully handles payloads with only minimal fields."""
        data = {
            "id": "s_min",
            "name": "ping",
            "file": "health.py",
            "type": "function",
        }
        symbol = CodeSymbol.from_dict(data)
        self.assertEqual(symbol.id, "s_min")
        self.assertEqual(symbol.name, "ping")
        self.assertEqual(symbol.file, "health.py")
        self.assertEqual(symbol.source_file, "health.py")
        self.assertIsNone(symbol.qualified_name)
        self.assertIsNone(symbol.module)
        self.assertIsNone(symbol.line_start)

    def test_models_document_entity_and_graph_edge_serialization(self):
        """Verify DocumentEntity and GraphEdge serialization methods."""
        entity = DocumentEntity(
            id="e1",
            name="OAuth Specification",
            type="Requirement",
            user_id="u1",
            repository_id="r1",
            description="RFC 6749 OAuth 2.0 framework",
            source_file="docs/security.md",
            section="Authentication",
        )
        e_dict = entity.to_dict()
        self.assertEqual(e_dict["name"], "OAuth Specification")
        self.assertEqual(e_dict["description"], "RFC 6749 OAuth 2.0 framework")
        e_rebuilt = DocumentEntity.from_dict(e_dict)
        self.assertEqual(e_rebuilt, entity)

        edge = GraphEdge("e1", "s1", "SPECIFIES", {"confidence": 0.95})
        edge_dict = edge.to_dict()
        self.assertEqual(edge_dict["relation"], "SPECIFIES")
        edge_rebuilt = GraphEdge.from_dict(edge_dict)
        self.assertEqual(edge_rebuilt, edge)

    def test_parse_graphify_graph_rich_extraction(self):
        """Verify parse_graphify_graph populates rich CodeSymbol fields from AST JSON."""
        graph = {
            "nodes": [
                {
                    "id": "node_auth_class",
                    "name": "AuthService",
                    "file_type": "code",
                    "source_file": "backend/auth/service.py",
                    "kind": "class",
                    "source_location": "L25-L120",
                    "documentation": "Handles OAuth2 and JWT session validation.",
                },
                {
                    "id": "node_auth_login",
                    "name": "login",
                    "file_type": "code",
                    "source_file": "backend/auth/service.py",
                    "kind": "method",
                    "source_location": "L45-L65",
                    "signature": "(user_id: str, password: str) -> bool",
                    "docstring": "Authenticates user credentials.",
                },
                {
                    "id": "node_hash_pw",
                    "name": "hash_password",
                    "file_type": "code",
                    "source_file": "backend/auth/utils.py",
                    "kind": "function",
                    "line_start": 10,
                    "line_end": 18,
                    "sig": "(raw_pw: str) -> str",
                },
            ],
            "edges": [
                {"source": "node_auth_class", "target": "node_auth_login", "relation": "defines"},
                {"source": "node_auth_login", "target": "node_hash_pw", "relation": "calls"},
            ],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            graph_path = Path(temp_dir) / "graph.json"
            graph_path.write_text(json.dumps(graph), encoding="utf-8")
            symbols, edges = parse_graphify_graph(
                graph_path, user_id="user-1", repository_id="repo-1"
            )

        self.assertEqual(len(symbols), 3)
        self.assertEqual(len(edges), 2)

        sym_by_name = {s.name: s for s in symbols}

        # 1. AuthService
        auth = sym_by_name["AuthService"]
        self.assertEqual(auth.module, "backend.auth.service")
        self.assertEqual(auth.package, "backend.auth")
        self.assertEqual(auth.source_file, "backend/auth/service.py")
        self.assertEqual(auth.language, "python")
        self.assertEqual(auth.line_start, 25)
        self.assertEqual(auth.line_end, 120)
        self.assertEqual(auth.documentation, "Handles OAuth2 and JWT session validation.")
        self.assertEqual(auth.qualified_name, "backend.auth.service.AuthService")

        # 2. login (parent_symbol inferred via DEFINES edge from AuthService)
        login = sym_by_name["login"]
        self.assertEqual(login.parent_symbol, "AuthService")
        self.assertEqual(login.qualified_name, "backend.auth.service.AuthService.login")
        self.assertEqual(login.signature, "(user_id: str, password: str) -> bool")
        self.assertEqual(login.line_start, 45)
        self.assertEqual(login.line_end, 65)
        self.assertEqual(login.documentation, "Authenticates user credentials.")

        # 3. hash_password
        hasher = sym_by_name["hash_password"]
        self.assertEqual(hasher.module, "backend.auth.utils")
        self.assertEqual(hasher.line_start, 10)
        self.assertEqual(hasher.line_end, 18)
        self.assertEqual(hasher.signature, "(raw_pw: str) -> str")
        self.assertEqual(hasher.qualified_name, "backend.auth.utils.hash_password")

        # Edges
        rel_types = [(e.relation) for e in edges]
        self.assertIn("DEFINES", rel_types)
        self.assertIn("CALLS", rel_types)

    def test_line_range_parsing_formats(self):
        """Test extraction of line numbers across multiple Graphify format variations."""
        self.assertEqual(_parse_line_range({"source_location": "L25-L60"}), (25, 60))
        self.assertEqual(_parse_line_range({"source_location": "L42"}), (42, 42))
        self.assertEqual(_parse_line_range({"location": "100:150"}), (100, 150))
        self.assertEqual(_parse_line_range({"line_start": 5, "line_end": 20}), (5, 20))
        self.assertEqual(_parse_line_range({"lineno": 14}), (14, 14))
        self.assertEqual(_parse_line_range({"line": 88}), (88, 88))
        self.assertEqual(_parse_line_range({}), (None, None))
        self.assertEqual(_parse_line_range({"source_location": "invalid"}), (None, None))

    def test_language_inference(self):
        """Test language detection from explicit node field and file extensions."""
        # Explicit
        self.assertEqual(_infer_language({"language": "Rust"}, "main.rs"), "rust")
        self.assertEqual(_infer_language({"lang": "GO"}, "main.go"), "go")

        # Inferred from file extension
        self.assertEqual(_infer_language({}, "app/service.ts"), "typescript")
        self.assertEqual(_infer_language({}, "app/component.tsx"), "typescript")
        self.assertEqual(_infer_language({}, "lib/math.py"), "python")
        self.assertEqual(_infer_language({}, "server.go"), "go")
        self.assertEqual(_infer_language({}, "Main.java"), "java")
        self.assertEqual(_infer_language({}, "engine.cpp"), "cpp")
        self.assertEqual(_infer_language({}, "script.rb"), "ruby")
        self.assertIsNone(_infer_language({}, "unknown.xyz"))
        self.assertIsNone(_infer_language({}, ""))

    def test_module_and_package_inference(self):
        """Test hierarchical module and package inference from file paths."""
        m, p = _infer_module_and_package("backend/services/auth_service.py")
        self.assertEqual(m, "backend.services.auth_service")
        self.assertEqual(p, "backend.services")

        m, p = _infer_module_and_package("auth.py")
        self.assertEqual(m, "auth")
        self.assertIsNone(p)

        m, p = _infer_module_and_package("", explicit_module="custom_module")
        self.assertEqual(m, "custom_module")
        self.assertIsNone(p)

    def test_signature_parsing(self):
        """Test signature parsing from string and parameter list formats."""
        self.assertEqual(
            _parse_signature({"signature": "(x: int, y: int) -> int"}),
            "(x: int, y: int) -> int",
        )
        self.assertEqual(_parse_signature({"params": ["a", "b"]}), "(a, b)")
        self.assertEqual(
            _parse_signature({
                "params": [{"name": "req", "type": "Request"}, {"name": "res", "type": "Response"}],
                "return_type": "Promise<void>",
            }),
            "(req: Request, res: Response) -> Promise<void>",
        )
        self.assertIsNone(_parse_signature({}))

    def test_documentation_parsing(self):
        """Test documentation extraction from various docstring and comment keys."""
        self.assertEqual(_parse_documentation({"docstring": "Authenticates user."}), "Authenticates user.")
        self.assertEqual(_parse_documentation({"comments": "// Cache TTL = 300s"}), "// Cache TTL = 300s")
        self.assertEqual(_parse_documentation({"description": "Repository layer."}), "Repository layer.")
        self.assertIsNone(_parse_documentation({}))

    def test_rationale_attachment_to_code_symbol(self):
        """Verify Graphify rationale nodes are attached as documentation to explained symbols."""
        graph = {
            "nodes": [
                {
                    "id": "sym1",
                    "name": "calculate_discount",
                    "file_type": "code",
                    "source_file": "pricing.py",
                    "kind": "function",
                },
                {
                    "id": "rat1",
                    "label": "Applies tier 3 volume discount for enterprise customers.",
                    "file_type": "rationale",
                    "source_file": "pricing.py",
                },
            ],
            "edges": [
                {"source": "rat1", "target": "sym1", "relation": "explains"},
            ],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            graph_path = Path(temp_dir) / "graph.json"
            graph_path.write_text(json.dumps(graph), encoding="utf-8")
            symbols, _ = parse_graphify_graph(graph_path)

        self.assertEqual(len(symbols), 1)
        self.assertEqual(
            symbols[0].documentation,
            "Applies tier 3 volume discount for enterprise customers.",
        )

    def test_neo4j_upsert_persists_rich_code_symbol_attributes(self):
        """Verify Neo4jMemoryStore writes all rich properties into Cypher query parameters."""
        driver = FakeDriver()
        store = Neo4jMemoryStore(driver=driver)

        symbol = CodeSymbol(
            id="scoped-1",
            name="AuthService",
            file="backend/auth.py",
            type="class",
            user_id="u1",
            repository_id="r1",
            qualified_name="backend.auth.AuthService",
            module="backend.auth",
            package="backend",
            source_file="backend/auth.py",
            line_start=10,
            line_end=50,
            parent_symbol=None,
            signature=None,
            documentation="Core authentication service",
            language="python",
        )

        count = store.upsert_code_symbols([symbol])
        self.assertEqual(count, 1)

        query, parameters = driver.queries[0]
        self.assertIn("MERGE (c:CodeSymbol {id: row.id})", query)
        self.assertIn("c.qualified_name = row.qualified_name", query)
        self.assertIn("c.module = row.module", query)
        self.assertIn("c.package = row.package", query)
        self.assertIn("c.source_file = row.source_file", query)
        self.assertIn("c.line_start = row.line_start", query)
        self.assertIn("c.line_end = row.line_end", query)
        self.assertIn("c.documentation = row.documentation", query)
        self.assertIn("c.language = row.language", query)

        row = parameters["rows"][0]
        self.assertEqual(row["qualified_name"], "backend.auth.AuthService")
        self.assertEqual(row["module"], "backend.auth")
        self.assertEqual(row["package"], "backend")
        self.assertEqual(row["source_file"], "backend/auth.py")
        self.assertEqual(row["line_start"], 10)
        self.assertEqual(row["line_end"], 50)
        self.assertEqual(row["documentation"], "Core authentication service")
        self.assertEqual(row["language"], "python")

    def test_neo4j_schema_indexes_include_rich_fields(self):
        """Verify initialize_schema creates indexes for qualified_name and module."""
        driver = FakeDriver()
        store = Neo4jMemoryStore(driver=driver)
        store.initialize_schema()

        queries = [q[0] for q in driver.queries]
        self.assertTrue(any("code_symbol_qualified_name" in q for q in queries))
        self.assertTrue(any("code_symbol_module" in q for q in queries))

    def test_semantic_matcher_incorporates_rich_symbol_context(self):
        """Verify code_symbol_to_text includes qualified_name, module, signature, and documentation."""
        symbol = CodeSymbol(
            id="s1",
            name="authenticate",
            file="backend/auth/service.py",
            type="method",
            qualified_name="backend.auth.service.AuthService.authenticate",
            module="backend.auth.service",
            package="backend.auth",
            source_file="backend/auth/service.py",
            signature="(token: str) -> User",
            documentation="Validates token and returns active User.",
            language="python",
        )

        text = code_symbol_to_text(symbol)
        self.assertIn("Code Symbol: authenticate", text)
        self.assertIn("Kind: method", text)
        self.assertIn("Qualified Name: backend.auth.service.AuthService.authenticate", text)
        self.assertIn("Module: backend.auth.service", text)
        self.assertIn("Package: backend.auth", text)
        self.assertIn("File: backend/auth/service.py", text)
        self.assertIn("Language: python", text)
        self.assertIn("Signature: (token: str) -> User", text)
        self.assertIn("Docstring: Validates token and returns active User.", text)


if __name__ == "__main__":
    unittest.main()
