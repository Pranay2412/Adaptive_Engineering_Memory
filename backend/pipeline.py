"""End-to-end orchestration for one user's repository ingestion."""

from __future__ import annotations

from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .bridge import build_cross_links
from .db_loader import Neo4jMemoryStore
from .ingestion_code import ingest_code
from .ingestion_docs import ingest_documents


async def ingest_repository(
    repository_path: str | Path,
    *,
    user_id: str,
    repository_id: str | None = None,
    store: Neo4jMemoryStore | None = None,
) -> dict[str, int | str]:
    """Ingest code and Markdown, bridge them, then upsert into shared Neo4j."""
    root = Path(repository_path).expanduser().resolve()
    if not user_id.strip():
        raise ValueError("user_id must not be empty")
    scoped_repository_id = repository_id or str(uuid5(NAMESPACE_URL, str(root)))
    symbols, code_edges = ingest_code(
        root, user_id=user_id, repository_id=scoped_repository_id
    )
    entities, document_edges = await ingest_documents(
        root, user_id=user_id, repository_id=scoped_repository_id
    )
    cross_links = build_cross_links(entities, symbols)
    owns_store = store is None
    memory_store = store or Neo4jMemoryStore()
    try:
        memory_store.initialize_schema()
        code_count = memory_store.upsert_code_symbols(symbols)
        document_count = memory_store.upsert_document_entities(entities)
        edge_count = memory_store.upsert_relationships(
            [*code_edges, *document_edges, *cross_links]
        )
        structural_summary = memory_store.sync_structural_hierarchy(
            repository_id=scoped_repository_id,
            user_id=user_id,
            repository_name=root.name,
            symbols=symbols,
            entities=entities,
        )
    finally:
        if owns_store:
            memory_store.close()
    return {
        "user_id": user_id,
        "repository_id": scoped_repository_id,
        "code_symbols": code_count,
        "document_entities": document_count,
        "relationships": edge_count + structural_summary.get("structural_edges", 0),
        "repositories": structural_summary.get("repositories", 0),
        "modules": structural_summary.get("modules", 0),
        "documents": structural_summary.get("documents", 0),
    }
if __name__ == "__main__":
    import argparse
    import asyncio
    import cognee

    parser = argparse.ArgumentParser(description="Run End-to-End Repository Ingestion Pipeline")
    parser.add_argument("--repo", default=".", help="Path to repository")
    parser.add_argument("--user-id", default="default_user", help="User identifier")
    parser.add_argument("--repo-id", default=None, help="Optional repository identifier")
    parser.add_argument("--reset-db", action="store_true", help="Reset DB before running")

    args = parser.parse_args()

    async def main():
        if args.reset_db:
            print("--> Resetting Cognee database state...")
            try:
                await cognee.prune.prune_data()
                await cognee.prune.prune_system()
                print("--> Cognee database reset complete.")
            except Exception as e:
                print(f"--> Prune warning (safe to ignore): {e}")

        print(f"--> Starting ingestion pipeline for: {args.repo}")
        result = await ingest_repository(
            repository_path=args.repo,
            user_id=args.user_id,
            repository_id=args.repo_id,
        )
        print("--> Pipeline execution completed successfully!")
        print("--> Execution Summary:", result)

    asyncio.run(main())