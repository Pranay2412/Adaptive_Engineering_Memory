# Adaptive Engineering Memory

Adaptive Engineering Memory builds a repository knowledge graph by combining Graphify's AST extraction with Cognee's document entity extraction. A bridge links document concepts to code symbols, and the normalized nodes and relationships are upserted into a shared Neo4j database.

## Architecture

1. Graphify scans the target repository and emits code symbols and structural relationships.
2. Cognee ingests Markdown files into a user-and-repository-specific dataset and extracts document entities and semantic relationships using Gemini.
3. The bridge matches document entity names and mentions to code symbol names, creating `SPECIFIES` links.
4. The Neo4j memory store upserts the combined graph with `MERGE`, making repeated ingestion idempotent.

Cognee's internal staging database is distinct from the shared Neo4j graph written by `Neo4jMemoryStore`. Cognee defaults to local Ladybug storage unless separately configured.

## Requirements

- Windows, PowerShell, and Python 3.12 recommended. Cognee 1.6.2's Ladybug 0.19 Windows dependency expects OpenSSL 3; Python 3.10's bundled OpenSSL runtime can cause `Could not find lbug C API shared library` during migrations.
- A reachable Neo4j instance with Bolt enabled, normally on `localhost:7687`.
- A Google Gemini API key for document extraction.
- Git is recommended for Graphify workflows.

## Setup

Create and activate a virtual environment from the project directory:

```powershell
py -3.12 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Create a `.env` file in the project root. Do not commit it or share its secret values.

```dotenv
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=replace-with-your-password

OPENAI_API_KEY=replace-with-your-openai-key
# Optional overrides; OpenAI is selected automatically when this key is set.
# LLM_PROVIDER=openai
# LLM_MODEL=gpt-4o-mini
# EMBEDDING_PROVIDER=openai
# EMBEDDING_MODEL=openai/text-embedding-3-small
```

`ingest_documents()` selects OpenAI when `OPENAI_API_KEY` is available and no provider is explicitly selected. It uses `gpt-4o-mini` for LLM extraction and `openai/text-embedding-3-small` for embeddings by default. To use Gemini instead, set `GEMINI_API_KEY` and `LLM_PROVIDER=gemini`; `LLM_MODEL`, `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, and `EMBEDDING_API_KEY` can be used to override these defaults. A provider-specific key is preferred over the generic `LLM_API_KEY`.

Cognee stores its system database separately from Neo4j. Set `SYSTEM_ROOT_DIRECTORY` to an absolute, stable directory if you want that data outside the Python installation. When migrating an existing installation, point it at the existing Cognee system directory first; do not accidentally upgrade a new, empty directory instead.

## Upgrade Cognee

Run Cognee's forward migration before the first ingestion:

```powershell
cognee-cli upgrade
cognee-cli current
```

For the earlier Python 3.10 installation described in this project's setup, Cognee logged its current data directory as:

```text
C:\FYP\Adaptive_Memory\.venv\Lib\site-packages\cognee\.cognee_system
```

To upgrade that existing data using a Python 3.12 environment, set `SYSTEM_ROOT_DIRECTORY` to that same system directory before running `cognee-cli upgrade`, or set it in `.env`. Keep using the same directory on subsequent runs to avoid splitting Cognee state across environments.

## Run Ingestion

Run the pipeline from the project root:

```powershell
python -m backend.pipeline --repo "C:\path\to\repository" --user-id "user-123"
```

Optionally provide a stable repository ID:

```powershell
python -m backend.pipeline --repo "C:\path\to\repository" --user-id "user-123" --repo-id "service-api"
```

If `--repo-id` is omitted, it is derived from the resolved repository path. The user and repository identifiers scope generated node IDs and Cognee datasets.

The pipeline requires Neo4j to be running and its credentials to be valid. It invokes Graphify, ingests Markdown through Cognee, creates cross-links, initializes indexes, and writes nodes and relationships to Neo4j.

**Do not use `--reset-db` for normal runs.** It invokes Cognee prune operations and can remove Cognee data.

## Graph Contents

The Neo4j store uses these node labels:

- `CodeSymbol`: code functions, classes, modules, and files as emitted by Graphify.
- `DocumentEntity`: named concepts and entities extracted by Cognee.

Supported relationships include `CALLS`, `IMPORTS`, `INHERITS`, `DEFINES`, `MOTIVATES`, `DEPENDS_ON`, and `SPECIFIES`. Cross-links include metadata identifying the bridge and its confidence.

Indexes are created for `CodeSymbol.name`, `CodeSymbol.id`, and `DocumentEntity.id`. Node IDs are scoped by user and repository, and writes use `MERGE` for repeatable ingestion.

## Tests

The unit tests do not require live Neo4j, Graphify execution, or Gemini API calls:

```powershell
python -m unittest discover -s tests -v
```

The tests cover Graphify JSON normalization, Cognee dataset scoping, cross-link matching, Neo4j transactional upserts, and environment setup.

## Troubleshooting

- **Neo4j connection refused:** confirm the container/server is running, Bolt is published on the configured port, and `NEO4J_URI` points to it. Test connectivity before running ingestion.
- **`Relational DB Migrations failed` / `Bookkeeping schema missing`:** use `cognee-cli upgrade` and verify `SYSTEM_ROOT_DIRECTORY` points to the intended Cognee store.
- **`Could not find lbug C API shared library` on Windows:** use Python 3.12 for this dependency set, then rerun the Cognee migration from the environment that will run the pipeline.
- **Missing model credentials:** provide `OPENAI_API_KEY` or `GEMINI_API_KEY` in `.env`, or set `LLM_API_KEY` with an explicit `LLM_PROVIDER`. Never commit secrets.
- **Graphify output missing:** confirm Graphify is installed in the active environment and the repository path exists. The ingestion parser looks for `.graphify/graph.json` and `graphify-out/graph.json`.
