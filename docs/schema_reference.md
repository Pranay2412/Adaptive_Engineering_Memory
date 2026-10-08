# Neo4j Unified Schema Reference

## 1. Node Labels & Properties

The unified schema represents source code structures, technical documentation entities, and engineering team memory within a single Neo4j database.

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

### Team Memory Nodes
- **`User`**: Team engineer or agent contributor (`id`, `name`, `email`, `role`, `created_at`).
- **`Team`**: Engineering group or domain pod (`id`, `name`, `description`, `created_at`).
- **`Project`**: Higher-level project container spanning repositories (`id`, `name`, `team_id`, `created_at`).
- **`AISession`**: Interactive AI development session (`id`, `title`, `user_id`, `repository_id`, `started_at`, `summary`).
- **`EngineeringEvent`**: Milestones, incident resolutions, releases (`id`, `event_type`, `title`, `timestamp`, `repository_id`).
- **`EngineeringDecision`**: Formal architecture design records (ADRs) (`id`, `title`, `rationale`, `status`, `alternatives_json`, `trade_offs_json`).
- **`Memory`**: Distilled engineering knowledge card (`id`, `title`, `content`, `category`, `scope`, `author_id`, `confidence`, `status`).

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
| **`MEMBER_OF`** | `User` | `Team` | Engineer membership in an engineering team or pod. |
| **`OWNS_PROJECT`** | `Team` | `Project` | Engineering team ownership of a project. |
| **`CONTAINS_REPO`** | `Project` | `Repository` | Project inclusion of a code repository. |
| **`INITIATED_SESSION`**| `User` | `AISession` | Author of an interactive AI engineering session. |
| **`IN_REPOSITORY`** | `AISession` | `Repository` | Codebase context where the session occurred. |
| **`TOUCHES_SYMBOL`**| `AISession` | `CodeSymbol` | Code symbol affected, inspected, or refactored in the session. |
| **`TOUCHES_FILE`**  | `AISession` | `Document` | Source file or document affected or inspected in the session. |
| **`AUTHORED`** | `User` | `Memory` | Author or owner of an engineering memory card. |
| **`SHARED_WITH_TEAM`**| `Memory` | `Team` | Team-scoped visibility association. |
| **`SCOPED_TO_REPO`** | `Memory` | `Repository` | Repository-scoped grounding association. |
| **`PART_OF_PROJECT`** | `Memory` | `Project` | Project association for engineering memory. |
| **`EXTRACTED_FROM_SESSION`** | `Memory` | `AISession` | Provenance trace to the originating AI session. |
| **`ORIGINATED_FROM_EVENT`** | `Memory` | `EngineeringEvent` | Traceability to an incident or milestone event. |
| **`JUSTIFIED_BY`** | `Memory` | `EngineeringDecision` | Rationale grounding in an engineering decision. |
| **`REFERENCES_SYMBOL`**| `Memory` | `CodeSymbol` | Grounding link to affected code symbols. |
| **`REFERENCES_DOC`** | `Memory` | `Document` | Grounding link to related documentation files. |
| **`SUPERSEDES`** | `Memory` | `Memory` | Evolution graph linking older memory to newer replacement. |
| **`AUTHORED_BY`** | `EngineeringDecision` | `User` | Author of an architecture decision. |
| **`AFFECTS_REPO`** | `EngineeringDecision` | `Repository` | Codebase affected by a design decision. |
| **`APPLIES_TO_TEAM`** | `EngineeringDecision` | `Team` | Engineering team governed by a decision. |

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
- `INDEX ON :CodeSymbol(name)`, `id`, `qualified_name`, `module`, `repository_id`
- `INDEX ON :DocumentEntity(name)`, `id`, `type`, `source_document`, `repository_id`
- `INDEX ON :Repository(id)`, `name`
- `INDEX ON :Module(id)`, `name`, `repository_id`
- `INDEX ON :Document(id)`, `path`, `repository_id`
- `INDEX ON :User(id)`, `email`
- `INDEX ON :Team(id)`, `name`
- `INDEX ON :Project(id)`, `team_id`
- `INDEX ON :AISession(id)`, `user_id`, `repository_id`
- `INDEX ON :EngineeringEvent(id)`, `repository_id`, `event_type`
- `INDEX ON :EngineeringDecision(id)`, `repository_id`, `team_id`, `category`
- `INDEX ON :Memory(id)`, `scope`, `category`, `repository_id`, `team_id`, `author_id`, `status`
- `RELATIONSHIP INDEX ON :SPECIFIES(confidence)`
