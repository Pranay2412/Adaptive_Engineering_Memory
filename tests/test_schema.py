"""Unit tests for the enriched Neo4j schema, entities, and relationships."""

import json
import unittest
from backend.db_loader import RELATION_TYPES, Neo4jMemoryStore
from backend.models import (
    CodeSymbol,
    Document,
    DocumentEntity,
    GraphEdge,
    Module,
    Repository,
    scoped_id,
)


class FakeResult:
    def __init__(self, records=None):
        self._records = records or []

    def consume(self):
        return None

    def __iter__(self):
        return iter(self._records)


class FakeTransaction:
    def __init__(self, queries):
        self.queries = queries

    def run(self, query, **parameters):
        self.queries.append((query, parameters))
        return FakeResult()


class FakeSession:
    def __init__(self, queries, query_results=None):
        self.queries = queries
        self.query_results = query_results or {}
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.closed = True

    def execute_write(self, callback):
        return callback(FakeTransaction(self.queries))

    def run(self, query, **parameters):
        self.queries.append((query, parameters))
        for key, records in self.query_results.items():
            if key in query:
                return FakeResult(records)
        return FakeResult()


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


class TestNeo4jUnifiedSchema(unittest.TestCase):
    """Test suite for the unified Graphify-Cognee Neo4j schema."""

    def test_repository_model_lifecycle_and_serialization(self):
        """Verify Repository dataclass creation and lossless serialization."""
        repo = Repository(
            id="repo-1",
            name="adaptive-memory",
            user_id="user-123",
            repository_id="repo-1",
            url="https://github.com/org/adaptive-memory",
            default_branch="main",
            language="Python",
            description="Engineering memory system",
            metadata={"stars": 42},
        )

        repo_dict = repo.to_dict()
        self.assertEqual(repo_dict["id"], "repo-1")
        self.assertEqual(repo_dict["name"], "adaptive-memory")
        self.assertEqual(repo_dict["language"], "Python")
        self.assertEqual(repo_dict["metadata"], {"stars": 42})

        reconstructed = Repository.from_dict(repo_dict)
        self.assertEqual(reconstructed, repo)

    def test_module_model_lifecycle_and_serialization(self):
        """Verify Module dataclass creation and lossless serialization."""
        module = Module(
            id="mod-1",
            name="auth.service",
            package="auth",
            path="src/auth/service.py",
            language="Python",
            user_id="user-123",
            repository_id="repo-1",
            metadata={"loc": 250},
        )

        mod_dict = module.to_dict()
        self.assertEqual(mod_dict["id"], "mod-1")
        self.assertEqual(mod_dict["name"], "auth.service")
        self.assertEqual(mod_dict["package"], "auth")
        self.assertEqual(mod_dict["path"], "src/auth/service.py")

        reconstructed = Module.from_dict(mod_dict)
        self.assertEqual(reconstructed, module)

    def test_document_model_lifecycle_and_serialization(self):
        """Verify Document dataclass creation and lossless serialization."""
        doc = Document(
            id="doc-1",
            name="architecture.md",
            path="docs/architecture.md",
            format="markdown",
            dataset_id="ds-memory",
            user_id="user-123",
            repository_id="repo-1",
            metadata={"version": "1.0"},
        )

        doc_dict = doc.to_dict()
        self.assertEqual(doc_dict["id"], "doc-1")
        self.assertEqual(doc_dict["name"], "architecture.md")
        self.assertEqual(doc_dict["path"], "docs/architecture.md")
        self.assertEqual(doc_dict["dataset_id"], "ds-memory")

        reconstructed = Document.from_dict(doc_dict)
        self.assertEqual(reconstructed, doc)

    def test_supported_relationship_types(self):
        """Verify all 7 required relationships are recognized."""
        expected_types = {
            "CALLS",
            "IMPORTS",
            "INHERITS",
            "DEFINES",
            "DEPENDS_ON",
            "MOTIVATES",
            "SPECIFIES",
        }
        self.assertTrue(expected_types.issubset(RELATION_TYPES))

    def test_initialize_schema_creates_indexes_for_all_five_labels_and_relationships(self):
        """Verify initialize_schema sets up indexes on CodeSymbol, DocumentEntity, Repository, Module, Document, and SPECIFIES."""
        driver = FakeDriver()
        store = Neo4jMemoryStore(driver=driver)
        store.initialize_schema()

        queries = [q[0] for q in driver.queries]
        # CodeSymbol
        self.assertTrue(any("code_symbol_id" in q for q in queries))
        self.assertTrue(any("code_symbol_name" in q for q in queries))
        self.assertTrue(any("code_symbol_qualified_name" in q for q in queries))
        self.assertTrue(any("code_symbol_module" in q for q in queries))
        self.assertTrue(any("code_symbol_repo" in q for q in queries))
        # DocumentEntity
        self.assertTrue(any("document_entity_id" in q for q in queries))
        self.assertTrue(any("document_entity_name" in q for q in queries))
        self.assertTrue(any("document_entity_type" in q for q in queries))
        self.assertTrue(any("document_entity_document" in q for q in queries))
        self.assertTrue(any("document_entity_repo" in q for q in queries))
        # Repository
        self.assertTrue(any("repository_id" in q for q in queries))
        self.assertTrue(any("repository_name" in q for q in queries))
        # Module
        self.assertTrue(any("module_id" in q for q in queries))
        self.assertTrue(any("module_name" in q for q in queries))
        self.assertTrue(any("module_repo" in q for q in queries))
        # Document
        self.assertTrue(any("document_id" in q for q in queries))
        self.assertTrue(any("document_path" in q for q in queries))
        self.assertTrue(any("document_repo" in q for q in queries))
        # SPECIFIES relationship index
        self.assertTrue(any("specifies_confidence" in q for q in queries))

    def test_upsert_repositories(self):
        """Verify upsert_repositories writes Repository nodes with correct Cypher and parameters."""
        driver = FakeDriver()
        store = Neo4jMemoryStore(driver=driver)

        repo = Repository(
            id="r-test",
            name="test-repo",
            user_id="u1",
            repository_id="r-test",
            url="https://github.com/example/test",
            default_branch="master",
            language="TypeScript",
            description="Test repo description",
            metadata={"private": True},
        )
        count = store.upsert_repositories([repo])
        self.assertEqual(count, 1)

        query, parameters = driver.queries[0]
        self.assertIn("MERGE (r:Repository {id: row.id})", query)
        self.assertIn("r.name = row.name", query)
        self.assertIn("r.url = row.url", query)
        self.assertIn("r.default_branch = row.default_branch", query)
        self.assertIn("r.language = row.language", query)
        self.assertIn("r.description = row.description", query)
        self.assertIn("r.metadata_json = row.metadata_json", query)

        row = parameters["rows"][0]
        self.assertEqual(row["id"], "r-test")
        self.assertEqual(row["name"], "test-repo")
        self.assertEqual(row["language"], "TypeScript")
        self.assertEqual(json.loads(row["metadata_json"]), {"private": True})

    def test_upsert_modules(self):
        """Verify upsert_modules writes Module nodes with correct Cypher and parameters."""
        driver = FakeDriver()
        store = Neo4jMemoryStore(driver=driver)

        mod = Module(
            id="m-auth",
            name="auth.handlers",
            package="auth",
            path="src/auth/handlers.py",
            language="Python",
            user_id="u1",
            repository_id="r1",
            metadata={"classes": 3},
        )
        count = store.upsert_modules([mod])
        self.assertEqual(count, 1)

        query, parameters = driver.queries[0]
        self.assertIn("MERGE (m:Module {id: row.id})", query)
        self.assertIn("m.name = row.name", query)
        self.assertIn("m.package = row.package", query)
        self.assertIn("m.path = row.path", query)
        self.assertIn("m.metadata_json = row.metadata_json", query)

        row = parameters["rows"][0]
        self.assertEqual(row["id"], "m-auth")
        self.assertEqual(row["name"], "auth.handlers")
        self.assertEqual(row["package"], "auth")
        self.assertEqual(json.loads(row["metadata_json"]), {"classes": 3})

    def test_upsert_documents(self):
        """Verify upsert_documents writes Document nodes with correct Cypher and parameters."""
        driver = FakeDriver()
        store = Neo4jMemoryStore(driver=driver)

        doc = Document(
            id="doc-api",
            name="api.md",
            path="docs/api.md",
            format="markdown",
            dataset_id="ds1",
            user_id="u1",
            repository_id="r1",
            metadata={"author": "team"},
        )
        count = store.upsert_documents([doc])
        self.assertEqual(count, 1)

        query, parameters = driver.queries[0]
        self.assertIn("MERGE (doc:Document {id: row.id})", query)
        self.assertIn("doc.name = row.name", query)
        self.assertIn("doc.path = row.path", query)
        self.assertIn("doc.format = row.format", query)
        self.assertIn("doc.dataset_id = row.dataset_id", query)
        self.assertIn("doc.metadata_json = row.metadata_json", query)

        row = parameters["rows"][0]
        self.assertEqual(row["id"], "doc-api")
        self.assertEqual(row["name"], "api.md")
        self.assertEqual(row["path"], "docs/api.md")
        self.assertEqual(json.loads(row["metadata_json"]), {"author": "team"})

    def test_upsert_relationships_specifies_first_class_properties(self):
        """Verify upsert_relationships writes first-class properties for SPECIFIES edges."""
        driver = FakeDriver()
        store = Neo4jMemoryStore(driver=driver)

        scores_map = {
            "lexical": 0.95,
            "semantic": 0.88,
            "context": 0.90,
            "type": 1.0,
            "final": 0.91,
        }
        edge = GraphEdge(
            source_id="doc_ent_1",
            target_id="code_sym_1",
            relation="SPECIFIES",
            metadata={
                "confidence": 0.91,
                "matching_method": "lexical+semantic+context",
                "scores": scores_map,
                "explanation": "Doc AuthService specifies class AuthService",
                "created_at": "2026-10-08T12:00:00+00:00",
                "source": "cross_link_bridge",
            },
        )

        count = store.upsert_relationships([edge])
        self.assertEqual(count, 1)

        query, parameters = driver.queries[0]
        self.assertIn("MERGE (source)-[r:SPECIFIES]->(target)", query)
        self.assertIn("r.confidence = row.confidence", query)
        self.assertIn("r.matching_method = row.matching_method", query)
        self.assertIn("r.scores = row.scores", query)
        self.assertIn("r.explanation = row.explanation", query)
        self.assertIn("r.created_at = row.created_at", query)
        self.assertIn("r.source = row.source", query)
        self.assertIn("r.metadata_json = row.metadata_json", query)

        row = parameters["rows"][0]
        self.assertEqual(row["source_id"], "doc_ent_1")
        self.assertEqual(row["target_id"], "code_sym_1")
        self.assertEqual(row["confidence"], 0.91)
        self.assertEqual(row["matching_method"], "lexical+semantic+context")
        self.assertEqual(json.loads(row["scores"]), scores_map)
        self.assertEqual(row["explanation"], "Doc AuthService specifies class AuthService")
        self.assertEqual(row["created_at"], "2026-10-08T12:00:00+00:00")
        self.assertEqual(row["source"], "cross_link_bridge")
        self.assertTrue(len(row["metadata_json"]) > 0)

    def test_upsert_relationships_other_relationship_types(self):
        """Verify upsert_relationships supports all other relationships (CALLS, IMPORTS, INHERITS, DEFINES, DEPENDS_ON, MOTIVATES)."""
        driver = FakeDriver()
        store = Neo4jMemoryStore(driver=driver)

        edges = [
            GraphEdge("sym1", "sym2", "CALLS", {"calls_count": 5}),
            GraphEdge("sym1", "sym3", "IMPORTS"),
            GraphEdge("sym2", "sym4", "INHERITS"),
            GraphEdge("mod1", "sym1", "DEFINES"),
            GraphEdge("mod1", "mod2", "DEPENDS_ON"),
            GraphEdge("doc1", "doc2", "MOTIVATES"),
        ]

        count = store.upsert_relationships(edges)
        self.assertEqual(count, 6)

        executed_relations = set()
        for query, parameters in driver.queries:
            for rel in ("CALLS", "IMPORTS", "INHERITS", "DEFINES", "DEPENDS_ON", "MOTIVATES"):
                if f"r:{rel}" in query:
                    executed_relations.add(rel)
        self.assertEqual(executed_relations, {"CALLS", "IMPORTS", "INHERITS", "DEFINES", "DEPENDS_ON", "MOTIVATES"})

    def test_upsert_relationships_invalid_type_raises_value_error(self):
        """Verify invalid relationship type is rejected."""
        driver = FakeDriver()
        store = Neo4jMemoryStore(driver=driver)

        invalid_edge = GraphEdge("s1", "s2", "INVALID_RELATION_NAME")
        with self.assertRaises(ValueError):
            store.upsert_relationships([invalid_edge])

    def test_sync_structural_hierarchy(self):
        """Verify sync_structural_hierarchy derives and links Repository, Module, Document and DEFINES edges."""
        driver = FakeDriver()
        store = Neo4jMemoryStore(driver=driver)

        symbols = [
            CodeSymbol("s1", "AuthService", "src/auth/service.py", "class", module="auth.service"),
            CodeSymbol("s2", "UserSession", "src/auth/service.py", "class", module="auth.service"),
            CodeSymbol("s3", "standalone_fn", "root_fn.py", "function", module=None),
        ]
        entities = [
            DocumentEntity("e1", "AuthReq", "Requirement", document_path="docs/auth.md", source_document="auth.md"),
            DocumentEntity("e2", "GlobalConcept", "Concept", document_path=None),
        ]

        summary = store.sync_structural_hierarchy(
            repository_id="repo-main",
            user_id="user-1",
            repository_name="Adaptive Memory",
            symbols=symbols,
            entities=entities,
        )

        self.assertEqual(summary["repositories"], 1)
        self.assertEqual(summary["modules"], 1)
        self.assertEqual(summary["documents"], 1)
        # Structural edges:
        # Repo -> Module(auth.service) [1]
        # Module -> s1, s2 [2]
        # Repo -> s3 [1]
        # Repo -> Document(docs/auth.md) [1]
        # Document -> e1 [1]
        # Repo -> e2 [1]
        # Total DEFINES edges = 1 + 2 + 1 + 1 + 1 + 1 = 7
        self.assertEqual(summary["structural_edges"], 7)

    def test_migrate_legacy_schema_backfills_properties_from_metadata(self):
        """Verify migrate_legacy_schema reads legacy SPECIFIES metadata_json and updates first-class properties."""
        legacy_records = [
            {
                "source_id": "legacy_doc_1",
                "target_id": "legacy_code_1",
                "metadata_json": json.dumps(
                    {
                        "confidence": 0.85,
                        "matching_method": "legacy_exact",
                        "lexical_score": 0.90,
                        "explanation": "Legacy exact match",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "source": "legacy_pipeline",
                    }
                ),
            }
        ]

        driver = FakeDriver(query_results={"MATCH (s)-[r:SPECIFIES]->(t)": legacy_records})
        store = Neo4jMemoryStore(driver=driver)

        result = store.migrate_legacy_schema()
        self.assertEqual(result["migrated_specifies"], 1)

        # Check update query was run with extracted first-class parameters
        update_queries = [
            (q, p) for q, p in driver.queries if "MATCH (source)-[r:SPECIFIES]->(target)" in q
        ]
        self.assertEqual(len(update_queries), 1)
        query, params = update_queries[0]
        self.assertIn("r.confidence = row.confidence", query)
        self.assertIn("r.matching_method = row.matching_method", query)
        self.assertIn("r.scores = row.scores", query)
        self.assertIn("r.explanation = row.explanation", query)
        self.assertIn("r.created_at = row.created_at", query)
        self.assertIn("r.source = row.source", query)

        row = params["rows"][0]
        self.assertEqual(row["source_id"], "legacy_doc_1")
        self.assertEqual(row["target_id"], "legacy_code_1")
        self.assertEqual(row["confidence"], 0.85)
        self.assertEqual(row["matching_method"], "legacy_exact")
        self.assertEqual(row["explanation"], "Legacy exact match")
        self.assertEqual(row["source"], "legacy_pipeline")


if __name__ == "__main__":
    unittest.main()
