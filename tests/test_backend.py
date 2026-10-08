import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path

from backend.bridge import build_cross_links
from backend.db_loader import Neo4jMemoryStore
from backend.ingestion_code import parse_graphify_graph
from backend.ingestion_docs import _prepare_cognee_environment, normalize_cognee_graph
from backend.models import CodeSymbol, DocumentEntity


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


class BackendNormalizationTests(unittest.TestCase):
    def test_graphify_nodes_and_known_edges_are_normalized(self):
        graph = {
            "nodes": [
                {"id": "auth", "label": "AuthService", "file_type": "code", "source_file": "auth.py", "kind": "class"},
                {"id": "login", "label": "login", "file_type": "code", "source_file": "auth.py", "kind": "function"},
                {"id": "readme", "label": "README", "file_type": "document", "source_file": "README.md"},
            ],
            "edges": [
                {"source": "login", "target": "auth", "relation": "calls"},
                {"source": "auth", "target": "login", "relation": "mentions"},
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            graph_file = Path(temp_dir) / "graph.json"
            graph_file.write_text(json.dumps(graph), encoding="utf-8")
            symbols, edges = parse_graphify_graph(graph_file)

        self.assertEqual([symbol.name for symbol in symbols], ["AuthService", "login"])
        self.assertEqual([(edge.source_id, edge.target_id, edge.relation) for edge in edges], [("login", "auth", "CALLS")])

    def test_cognee_graph_is_scoped_and_semantic_edges_are_kept(self):
        nodes = [
            ("auth", {"name": "AuthService", "type": "Requirement", "source_dataset_ids": ["mine"]}),
            ("other", {"name": "OtherService", "type": "Concept", "source_dataset_ids": ["other"]}),
        ]
        edges = [("auth", "other", "DEFINES", {"source_dataset_ids": ["mine"]})]
        entities, edges = normalize_cognee_graph(
            nodes,
            edges,
            user_id="user-1",
            repository_id="repo-1",
            dataset_id="mine",
        )
        self.assertEqual([entity.name for entity in entities], ["AuthService"])
        self.assertEqual(edges, [])

    def test_bridge_matches_entity_mentions_to_symbol_names(self):
        entity = DocumentEntity("doc", "The AuthService must validate tokens", "Requirement")
        symbol = CodeSymbol("code", "AuthService", "auth.py", "class")
        links = build_cross_links([entity], [symbol])
        self.assertEqual(len(links), 1)
        self.assertEqual((links[0].source_id, links[0].target_id, links[0].relation), ("doc", "code", "SPECIFIES"))
        self.assertGreaterEqual(links[0].metadata["confidence"], 0.85)
        self.assertEqual(links[0].metadata["source"], "cross_link_bridge")
        self.assertIn("matching_method", links[0].metadata)
        self.assertIn("lexical_score", links[0].metadata)
        self.assertIn("explanation", links[0].metadata)

    def test_neo4j_upsert_uses_transaction_and_closes_session(self):
        driver = FakeDriver()
        store = Neo4jMemoryStore(driver=driver)
        count = store.upsert_code_symbols([CodeSymbol("scoped-id", "AuthService", "auth.py", "class")])

        self.assertEqual(count, 1)
        self.assertTrue(driver.sessions[0].closed)
        query, parameters = driver.queries[0]
        self.assertIn("MERGE (c:CodeSymbol {id: row.id})", query)
        self.assertEqual(parameters["rows"][0]["name"], "AuthService")

    def test_openai_key_is_passed_to_cognee_without_overriding_explicit_key(self):
        fake_cognee = SimpleNamespace(
            config=SimpleNamespace(
                set_llm_config=Mock(),
                set_embedding_config=Mock(),
            )
        )
        with (
            patch("backend.ingestion_docs.load_dotenv"),
            patch.dict(sys.modules, {"cognee": fake_cognee}),
            patch.dict(os.environ, {"OPENAI_API_KEY": "openai-secret"}, clear=True),
        ):
            _prepare_cognee_environment()
            self.assertEqual(os.environ["LLM_PROVIDER"], "openai")
            self.assertEqual(os.environ["LLM_API_KEY"], "openai-secret")
            self.assertNotIn("GEMINI_API_KEY", os.environ)
            self.assertEqual(os.environ["EMBEDDING_PROVIDER"], "openai")
            self.assertEqual(os.environ["EMBEDDING_API_KEY"], "openai-secret")
            fake_cognee.config.set_llm_config.assert_called_with(
                {
                    "llm_provider": "openai",
                    "llm_model": "gpt-4o-mini",
                    "llm_api_key": "openai-secret",
                }
            )
            fake_cognee.config.set_embedding_config.assert_called_with(
                {
                    "embedding_provider": "openai",
                    "embedding_model": "openai/text-embedding-3-small",
                    "embedding_api_key": "openai-secret",
                }
            )

        with (
            patch("backend.ingestion_docs.load_dotenv"),
            patch.dict(sys.modules, {"cognee": fake_cognee}),
            patch.dict(
                os.environ,
                {
                    "GEMINI_API_KEY": "gemini-secret",
                    "LLM_PROVIDER": "gemini",
                },
                clear=True,
            ),
        ):
            _prepare_cognee_environment()
            self.assertEqual(os.environ["LLM_PROVIDER"], "gemini")
            self.assertEqual(os.environ["LLM_API_KEY"], "gemini-secret")
            self.assertEqual(os.environ["EMBEDDING_PROVIDER"], "gemini")


if __name__ == "__main__":
    unittest.main()