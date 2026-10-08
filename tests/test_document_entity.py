"""Unit tests for the enriched Cognee normalization layer and DocumentEntity provenance."""

import json
import unittest

from backend.db_loader import Neo4jMemoryStore
from backend.ingestion_docs import (
    _clean_metadata,
    _extract_dataset_id,
    _has_dataset_provenance,
    normalize_cognee_graph,
)
from backend.models import DocumentEntity, GraphEdge, scoped_id
from backend.semantic_matcher import document_entity_to_text


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


class TestDocumentEntityNormalization(unittest.TestCase):
    """Test suite covering DocumentEntity model, provenance extraction, scoping, and Neo4j persistence."""

    def test_document_entity_dataclass_defaults_and_post_init(self):
        """Test default values, bidirectional file path syncing, and source document inference."""
        # Minimal instantiation
        ent1 = DocumentEntity(id="e1", name="AuthRequirement", type="Requirement")
        self.assertEqual(ent1.id, "e1")
        self.assertEqual(ent1.name, "AuthRequirement")
        self.assertEqual(ent1.type, "Requirement")
        self.assertEqual(ent1.entity_type, "Requirement")
        self.assertIsNone(ent1.description)
        self.assertIsNone(ent1.entity_description)
        self.assertIsNone(ent1.source_file)
        self.assertIsNone(ent1.document_path)
        self.assertIsNone(ent1.source_document)
        self.assertIsNone(ent1.section)
        self.assertIsNone(ent1.source_chunk)
        self.assertIsNone(ent1.dataset_id)
        self.assertIsNone(ent1.user_id)
        self.assertIsNone(ent1.repository_id)

        # Path synchronization and source document inference
        ent2 = DocumentEntity(
            id="e2",
            name="SessionSecurity",
            type="SecurityRule",
            document_path="docs/security/session_specs.md",
        )
        self.assertEqual(ent2.document_path, "docs/security/session_specs.md")
        self.assertEqual(ent2.source_file, "docs/security/session_specs.md")
        self.assertEqual(ent2.source_document, "session_specs.md")

        # Reverse synchronization when source_file is provided
        ent3 = DocumentEntity(
            id="e3",
            name="BillingArchitecture",
            type="Architecture",
            source_file="architecture/billing.md",
        )
        self.assertEqual(ent3.document_path, "architecture/billing.md")
        self.assertEqual(ent3.source_document, "billing.md")

    def test_document_entity_serialization_to_dict_and_from_dict(self):
        """Verify lossless dictionary and JSON round-trip serialization."""
        original = DocumentEntity(
            id="e_full",
            name="OAuth2Flow",
            type="Requirement",
            user_id="u_dev",
            repository_id="repo_app",
            description="Authorization Code Grant with PKCE",
            source_document="oauth_spec.md",
            document_path="docs/security/oauth_spec.md",
            section="Token Exchange",
            source_chunk="Client sends authorization_code and code_verifier to /token endpoint.",
            dataset_id="dataset_sec",
            metadata={"status": "approved", "priority": "high"},
        )

        data = original.to_dict()
        self.assertEqual(data["id"], "e_full")
        self.assertEqual(data["name"], "OAuth2Flow")
        self.assertEqual(data["source_document"], "oauth_spec.md")
        self.assertEqual(data["document_path"], "docs/security/oauth_spec.md")
        self.assertEqual(data["section"], "Token Exchange")
        self.assertIn("authorization_code", data["source_chunk"])
        self.assertEqual(data["dataset_id"], "dataset_sec")
        self.assertEqual(data["metadata"]["priority"], "high")

        # JSON serialization round-trip
        json_str = json.dumps(data)
        reconstructed = DocumentEntity.from_dict(json.loads(json_str))
        self.assertEqual(reconstructed, original)

    def test_document_entity_hybrid_retrieval_text_formatting(self):
        """Verify to_retrieval_text formats rich text suitable for dense embeddings and BM25 index."""
        entity = DocumentEntity(
            id="e_ret",
            name="RateLimiter",
            type="Component",
            description="Token bucket algorithm for IP rate limiting",
            source_document="rate_limiting.md",
            document_path="docs/architecture/rate_limiting.md",
            section="Implementation Strategy",
            source_chunk="Requests exceeding 100 req/min receive HTTP 429 Too Many Requests.",
        )

        retrieval_text = entity.to_retrieval_text()
        self.assertIn("Documentation Concept: RateLimiter", retrieval_text)
        self.assertIn("Type: Component", retrieval_text)
        self.assertIn("Description: Token bucket algorithm for IP rate limiting", retrieval_text)
        self.assertIn("Document: rate_limiting.md", retrieval_text)
        self.assertIn("Path: docs/architecture/rate_limiting.md", retrieval_text)
        self.assertIn("Section: Implementation Strategy", retrieval_text)
        self.assertIn("Context: Requests exceeding 100 req/min receive HTTP 429", retrieval_text)

    def test_normalize_cognee_graph_with_direct_entity_provenance(self):
        """Test normalization when entity nodes directly provide provenance properties."""
        nodes = [
            (
                "ent_auth",
                {
                    "name": "AuthService",
                    "type": "Architecture",
                    "description": "Central identity and authentication service",
                    "source_document": "identity.md",
                    "document_path": "docs/identity.md",
                    "section": "Overview",
                    "source_chunk": "AuthService manages user identity and token signing.",
                    "source_dataset_ids": ["ds_main"],
                    "custom_owner": "sec-team",
                },
            ),
        ]
        edges = []

        entities, rels = normalize_cognee_graph(
            nodes,
            edges,
            user_id="user_1",
            repository_id="repo_1",
            dataset_id="ds_main",
        )

        self.assertEqual(len(entities), 1)
        self.assertEqual(len(rels), 0)

        ent = entities[0]
        self.assertEqual(ent.id, scoped_id("user_1", "repo_1", "ent_auth"))
        self.assertEqual(ent.name, "AuthService")
        self.assertEqual(ent.type, "Architecture")
        self.assertEqual(ent.description, "Central identity and authentication service")
        self.assertEqual(ent.source_document, "identity.md")
        self.assertEqual(ent.document_path, "docs/identity.md")
        self.assertEqual(ent.section, "Overview")
        self.assertEqual(ent.source_chunk, "AuthService manages user identity and token signing.")
        self.assertEqual(ent.dataset_id, "ds_main")
        self.assertEqual(ent.user_id, "user_1")
        self.assertEqual(ent.repository_id, "repo_1")
        self.assertEqual(ent.metadata, {"custom_owner": "sec-team"})

    def test_normalize_cognee_graph_with_chunk_and_document_linking(self):
        """Verify provenance resolution when entities are linked to chunks and documents via edges."""
        nodes = [
            # Document node
            (
                "doc_security",
                {
                    "name": "security.md",
                    "type": "Document",
                    "path": "docs/security.md",
                    "source_dataset_ids": ["ds_proj"],
                },
            ),
            # Chunk node linked to Document
            (
                "chunk_jwt",
                {
                    "type": "DocumentChunk",
                    "text": "JWT tokens expire after 3600 seconds and must be signed with RS256.",
                    "section": "Token Expiration Policy",
                    "document_id": "doc_security",
                    "source_dataset_ids": ["ds_proj"],
                },
            ),
            # Domain Entity node
            (
                "ent_jwt",
                {
                    "name": "TokenExpirationRule",
                    "type": "SecurityRule",
                    "description": "Rules governing token lifetimes",
                    "source_dataset_ids": ["ds_proj"],
                },
            ),
            # Target Domain Entity
            (
                "ent_session",
                {
                    "name": "SessionManager",
                    "type": "Architecture",
                    "description": "Manages user sessions",
                    "source_dataset_ids": ["ds_proj"],
                },
            ),
        ]

        edges = [
            # Chunk -> Entity extraction edge
            ("chunk_jwt", "ent_jwt", "EXTRACTED_FROM", {"source_dataset_ids": ["ds_proj"]}),
            # Domain relationship edge
            ("ent_jwt", "ent_session", "DEFINES", {"source_dataset_ids": ["ds_proj"]}),
        ]

        entities, rels = normalize_cognee_graph(
            nodes,
            edges,
            user_id="user_admin",
            repository_id="repo_main",
            dataset_id="ds_proj",
        )

        self.assertEqual(len(entities), 2)
        ent_by_name = {e.name: e for e in entities}

        # 1. TokenExpirationRule enriched from chunk_jwt and doc_security
        rule = ent_by_name["TokenExpirationRule"]
        self.assertEqual(rule.section, "Token Expiration Policy")
        self.assertEqual(
            rule.source_chunk,
            "JWT tokens expire after 3600 seconds and must be signed with RS256.",
        )
        self.assertEqual(rule.document_path, "docs/security.md")
        self.assertEqual(rule.source_document, "security.md")
        self.assertEqual(rule.dataset_id, "ds_proj")

        # 2. Domain edge preserved, internal edge excluded
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0].relation, "DEFINES")
        self.assertEqual(rels[0].source_id, rule.id)
        self.assertEqual(rels[0].target_id, ent_by_name["SessionManager"].id)

    def test_normalize_cognee_graph_filters_out_internal_keys(self):
        """Verify Cognee runtime internals (embeddings, vectors, internal IDs) are purged from metadata."""
        nodes = [
            (
                "ent_item",
                {
                    "name": "DataCache",
                    "type": "Concept",
                    "embedding": [0.12, 0.45, 0.78],
                    "vector_id": "vec_998",
                    "_cognee_internal": "private",
                    "source_dataset_ids": ["ds_1"],
                    "domain_tag": "in-memory",
                },
            ),
        ]
        entities, _ = normalize_cognee_graph(
            nodes,
            [],
            user_id="u1",
            repository_id="r1",
            dataset_id="ds_1",
        )
        metadata = entities[0].metadata
        self.assertIn("domain_tag", metadata)
        self.assertNotIn("embedding", metadata)
        self.assertNotIn("vector_id", metadata)
        self.assertNotIn("_cognee_internal", metadata)
        self.assertNotIn("source_dataset_ids", metadata)

    def test_scoping_enforcement_rejects_unscoped_or_mismatched_graphs(self):
        """Verify strict multi-tenant scoping raises errors when dataset provenance is missing."""
        # 1. Nodes belonging to another dataset are skipped
        nodes = [
            ("other", {"name": "OtherService", "type": "Concept", "source_dataset_ids": ["other_ds"]}),
        ]
        entities, _ = normalize_cognee_graph(
            nodes,
            [],
            user_id="u1",
            repository_id="r1",
            dataset_id="expected_ds",
        )
        self.assertEqual(len(entities), 0)

        # 2. Nodes with zero dataset provenance raise RuntimeError when dataset_id is expected
        unscoped_nodes = [
            ("bad_node", {"name": "LeakedEntity", "type": "Concept"}),
        ]
        with self.assertRaises(RuntimeError):
            normalize_cognee_graph(
                unscoped_nodes,
                [],
                user_id="u1",
                repository_id="r1",
                dataset_id="my_dataset",
            )

    def test_neo4j_upsert_persists_rich_document_entity_attributes(self):
        """Verify Neo4jMemoryStore writes all provenance fields to Cypher query parameters."""
        driver = FakeDriver()
        store = Neo4jMemoryStore(driver=driver)

        entity = DocumentEntity(
            id="scoped_doc_1",
            name="SessionInvalidation",
            type="Requirement",
            user_id="u_test",
            repository_id="r_test",
            description="Session must terminate on user logout",
            source_document="auth_workflow.md",
            document_path="docs/auth_workflow.md",
            section="Logout Sequence",
            source_chunk="POST /logout clears active sessions.",
            dataset_id="ds_auth",
            metadata={"priority": "critical"},
        )

        count = store.upsert_document_entities([entity])
        self.assertEqual(count, 1)

        query, parameters = driver.queries[0]
        self.assertIn("MERGE (d:DocumentEntity {id: row.id})", query)
        self.assertIn("d.description = row.description", query)
        self.assertIn("d.source_document = row.source_document", query)
        self.assertIn("d.document_path = row.document_path", query)
        self.assertIn("d.section = row.section", query)
        self.assertIn("d.source_chunk = row.source_chunk", query)
        self.assertIn("d.dataset_id = row.dataset_id", query)
        self.assertIn("d.metadata_json = row.metadata_json", query)

        row = parameters["rows"][0]
        self.assertEqual(row["name"], "SessionInvalidation")
        self.assertEqual(row["type"], "Requirement")
        self.assertEqual(row["description"], "Session must terminate on user logout")
        self.assertEqual(row["source_document"], "auth_workflow.md")
        self.assertEqual(row["document_path"], "docs/auth_workflow.md")
        self.assertEqual(row["section"], "Logout Sequence")
        self.assertEqual(row["source_chunk"], "POST /logout clears active sessions.")
        self.assertEqual(row["dataset_id"], "ds_auth")
        self.assertEqual(json.loads(row["metadata_json"]), {"priority": "critical"})

    def test_neo4j_schema_indexes_for_document_entities(self):
        """Verify initialize_schema sets up indexes on DocumentEntity properties."""
        driver = FakeDriver()
        store = Neo4jMemoryStore(driver=driver)
        store.initialize_schema()

        queries = [q[0] for q in driver.queries]
        self.assertTrue(any("document_entity_id" in q for q in queries))
        self.assertTrue(any("document_entity_name" in q for q in queries))
        self.assertTrue(any("document_entity_type" in q for q in queries))
        self.assertTrue(any("document_entity_document" in q for q in queries))

    def test_semantic_matcher_incorporates_document_entity_provenance(self):
        """Verify document_entity_to_text includes Document, Path, Section, and Context."""
        entity = DocumentEntity(
            id="e_sem",
            name="TokenRefresh",
            type="Requirement",
            description="Refresh token exchange mechanism",
            source_document="tokens.md",
            document_path="docs/tokens.md",
            section="Renewal",
            source_chunk="Refresh tokens have a 30-day sliding window.",
        )

        text = document_entity_to_text(entity)
        self.assertIn("Documentation Concept: TokenRefresh", text)
        self.assertIn("Type: Requirement", text)
        self.assertIn("Description: Refresh token exchange mechanism", text)
        self.assertIn("Document: tokens.md", text)
        self.assertIn("Path: docs/tokens.md", text)
        self.assertIn("Section: Renewal", text)
        self.assertIn("Context: Refresh tokens have a 30-day sliding window.", text)


if __name__ == "__main__":
    unittest.main()
