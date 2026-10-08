# Neo4j Unified Schema Reference

## 1. Node Labels & Properties

The unified schema represents both source code structures and technical documentation entities within a single Neo4j database.

### `CodeSymbol`
Represents AST code entities (classes, methods, functions, modules, interfaces).
- **`id`** (`String`, Unique Indexed): Scoped deterministic UUID.
- **`name`** (`String`, Indexed): Unqualified symbol identifier (e.g., `AuthService`).
- **`type`** (`String`): Symbol category (`class`, `function`, `method`, `module`, `interface`).
- **`file`** (`String`): Relative or absolute source file path.
- **`qualified_name`** (`String`, Optional): Dot-separated full module path.
- **`signature`** (`String`, Optional): Function signature or class header.
- **`documentation`** (`String`, Optional): Extracted docstring or leading comments.
- **`line_start`** / **`line_end`** (`Integer`, Optional): Source code line boundaries.
- **`user_id`** / **`repository_id`** (`String`): Multi-tenant isolation scope.

### `DocumentEntity`
Represents architectural concepts, requirements, constraints, and RFC entities extracted from documentation.
- **`id`** (`String`, Unique Indexed): Scoped deterministic UUID.
- **`name`** (`String`, Indexed): Concept name (e.g., `Authentication Architecture`).
- **`type`** (`String`): Concept type (`concept`, `spec`, `RFC`, `feature`, `requirement`).
- **`description`** (`String`): Extracted summary description.
- **`source_document`** / **`document_path`** (`String`): Source markdown document.
- **`section`** (`String`, Optional): Markdown heading or section name.
- **`source_chunk`** (`String`, Optional): Raw text snippet.
- **`user_id`** / **`repository_id`** (`String`): Multi-tenant isolation scope.

### Structural Nodes
- **`Repository`**: Top-level code repository node.
- **`Module`**: Architectural module or package folder.
- **`Document`**: Raw source documentation file node.

---

## 2. Relationships

| Relationship | Source Node | Target Node | Semantics |
| :--- | :--- | :--- | :--- |
| **`SPECIFIES`** | `DocumentEntity` | `CodeSymbol` | Technical requirement/design specified by code implementation. |
| **`CALLS`** | `CodeSymbol` | `CodeSymbol` | Invocation of a function, method, or constructor. |
| **`IMPORTS`** | `CodeSymbol` | `CodeSymbol` | Dependency import or module inclusion. |
| **`INHERITS`** | `CodeSymbol` | `CodeSymbol` | Class inheritance or interface implementation. |
| **`DEFINES`** | `Module` / `CodeSymbol` | `CodeSymbol` | Lexical scope containment. |
| **`DEPENDS_ON`** | `Module` / `CodeSymbol` | `Module` / `CodeSymbol` | Architectural dependency relationship. |
| **`MOTIVATES`** | `DocumentEntity` | `DocumentEntity` | Architectural decision or rationale linkage. |

### First-Class Properties on `SPECIFIES`
The cross-linking bridge stores first-class properties on `SPECIFIES` edges:
- **`confidence`** (`Float`): Composite confidence in $[0.0, 1.0]$.
- **`matching_method`** (`String`): Matching technique (`exact`, `normalized`, `contextual`, `semantic`, or combination).
- **`lexical_score`** (`Float`): Lexical string similarity score.
- **`semantic_score`** (`Float`): Dense vector cosine similarity score.
- **`structural_score`** (`Float`): Entity type and AST compatibility score.
- **`context_score`** (`Float`): Module path and namespace overlap score.
- **`explanation`** (`String`): Human-readable explanation of why the link was established.
- **`created_at`** (`String`): ISO 8601 UTC timestamp.

---

## 3. Database Indexes & Constraints

Initialized automatically via `Neo4jMemoryStore.initialize_schema()`:
- `INDEX ON :CodeSymbol(name)`
- `INDEX ON :CodeSymbol(id)`
- `INDEX ON :CodeSymbol(repository_id)`
- `INDEX ON :DocumentEntity(name)`
- `INDEX ON :DocumentEntity(id)`
- `INDEX ON :DocumentEntity(repository_id)`
- `INDEX ON :Repository(id)`
- `INDEX ON :Module(id)`
- `INDEX ON :Document(id)`
- `RELATIONSHIP INDEX ON :SPECIFIES(confidence)`
