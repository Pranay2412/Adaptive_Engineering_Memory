"""Entity normalization utilities for the Graphify-Cognee bridge."""

from __future__ import annotations

import enum
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


class EntityKind(str, enum.Enum):
    """Categorization of naming conventions and identifier forms."""

    CAMEL_CASE = "camel_case"
    SNAKE_CASE = "snake_case"
    KEBAB_CASE = "kebab_case"
    DOTTED = "dotted"
    NAMESPACE = "namespace"
    FILE_PATH = "file_path"
    API_ROUTE = "api_route"
    FUNCTION = "function"
    CLASS = "class"
    MODULE = "module"
    GENERIC = "generic"


COMMON_SOURCE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".ts", ".js", ".tsx", ".jsx", ".md", ".java", ".go", ".rs",
    ".cpp", ".c", ".h", ".hpp", ".cs", ".rb", ".php", ".json", ".yaml",
    ".yml", ".html", ".css", ".sql", ".sh", ".ps1"
})

HTTP_METHODS: frozenset[str] = frozenset({
    "GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"
})


def to_canonical(text: str) -> str:
    """Return alphanumeric lowercase representation of a string."""
    return re.sub(r"[^a-zA-Z0-9]", "", text).lower()


def tokenize_name(text: str) -> tuple[str, ...]:
    """Split camelCase, snake_case, kebab-case, or delimiter-separated text into lowercase tokens."""
    if not text:
        return ()
    # Replace non-alphanumeric delimiters with spaces
    cleaned = re.sub(r"[^a-zA-Z0-9]+", " ", text)
    # Split camelCase transitions: e.g. fooBar -> foo Bar, HTMLParser -> HTML Parser
    cleaned = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", cleaned)
    cleaned = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", cleaned)
    tokens = [token.lower() for token in cleaned.split() if token]
    return tuple(tokens)


def _unique_ordered_tokens(tokens: Iterable[str]) -> tuple[str, ...]:
    """Preserve token order while removing duplicates."""
    seen: set[str] = set()
    result: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            result.append(token)
    return tuple(result)


def detect_kind(raw: str) -> EntityKind:
    """Auto-detect the naming convention or structural format of an identifier."""
    stripped = raw.strip()
    if not stripped:
        return EntityKind.GENERIC

    # API Route: starts with HTTP method or contains route-like structure
    first_word = stripped.split()[0].upper() if " " in stripped else ""
    if first_word in HTTP_METHODS or (stripped.startswith("/") and len(stripped.split("/")) > 2):
        return EntityKind.API_ROUTE

    # Function call syntax (e.g., func() or func(arg))
    if re.search(r"\(\s*.*\)$", stripped):
        return EntityKind.FUNCTION

    # File path: contains directory slashes or known file extension
    norm_path = stripped.replace("\\", "/")
    if "/" in norm_path or any(stripped.lower().endswith(ext) for ext in COMMON_SOURCE_EXTENSIONS):
        return EntityKind.FILE_PATH

    # Namespace qualification (C++, Rust, PHP ::)
    if "::" in stripped:
        return EntityKind.NAMESPACE

    # Dotted names (e.g., backend.models.CodeSymbol or auth.service)
    if "." in stripped:
        return EntityKind.DOTTED

    # kebab-case
    if "-" in stripped and not stripped.startswith("-"):
        return EntityKind.KEBAB_CASE

    # snake_case
    if "_" in stripped:
        return EntityKind.SNAKE_CASE

    # CamelCase: check for mixed casing
    has_upper = any(char.isupper() for char in stripped)
    has_lower = any(char.islower() for char in stripped)
    if has_upper and has_lower:
        if stripped[0].isupper():
            return EntityKind.CLASS
        return EntityKind.CAMEL_CASE

    return EntityKind.GENERIC


@dataclass(frozen=True)
class NormalizedEntity:
    """Normalized representation of an identifier preserving original and structured forms."""

    original: str
    base_name: str
    canonical: str
    canonical_full: str
    tokens: tuple[str, ...]
    qualifiers: tuple[str, ...] = ()
    qualifier_tokens: tuple[str, ...] = ()
    all_tokens: tuple[str, ...] = ()
    kind: EntityKind = EntityKind.GENERIC
    metadata: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)

    def exact_match(self, other: NormalizedEntity | str) -> bool:
        """Check exact equality with another entity's original string."""
        other_raw = other.original if isinstance(other, NormalizedEntity) else str(other)
        return self.original == other_raw

    def canonical_match(self, other: NormalizedEntity | str) -> bool:
        """Check if base canonical names match, or if base matches full canonical across scopes."""
        other_norm = other if isinstance(other, NormalizedEntity) else normalize_entity(str(other))
        # Direct base canonical match
        if self.canonical and self.canonical == other_norm.canonical:
            return True
        # Cross-scope match (e.g., base 'authservice' matches full 'authservice' of 'auth::service')
        if self.canonical and self.canonical == other_norm.canonical_full:
            return True
        if self.canonical_full and self.canonical_full == other_norm.canonical:
            return True
        return False

    def token_match(self, other: NormalizedEntity | str) -> bool:
        """Check if base token sequences are identical."""
        other_norm = other if isinstance(other, NormalizedEntity) else normalize_entity(str(other))
        return self.tokens == other_norm.tokens

    def token_jaccard(self, other: NormalizedEntity | str) -> float:
        """Compute Jaccard similarity over base tokens."""
        other_norm = other if isinstance(other, NormalizedEntity) else normalize_entity(str(other))
        set_a = set(self.tokens)
        set_b = set(other_norm.tokens)
        if not set_a and not set_b:
            return 1.0
        union = set_a | set_b
        if not union:
            return 0.0
        return len(set_a & set_b) / len(union)

    def all_token_jaccard(self, other: NormalizedEntity | str) -> float:
        """Compute Jaccard similarity over all tokens (qualifiers + base)."""
        other_norm = other if isinstance(other, NormalizedEntity) else normalize_entity(str(other))
        set_a = set(self.all_tokens)
        set_b = set(other_norm.all_tokens)
        if not set_a and not set_b:
            return 1.0
        union = set_a | set_b
        if not union:
            return 0.0
        return len(set_a & set_b) / len(union)

    def token_overlap_ratio(self, other: NormalizedEntity | str) -> float:
        """Compute overlap ratio relative to the shorter token set (containment)."""
        other_norm = other if isinstance(other, NormalizedEntity) else normalize_entity(str(other))
        set_a = set(self.tokens)
        set_b = set(other_norm.tokens)
        if not set_a or not set_b:
            return 0.0
        min_len = min(len(set_a), len(set_b))
        return len(set_a & set_b) / min_len

    def matches(self, other: NormalizedEntity | str, mode: str = "canonical") -> bool:
        """Check compatibility based on the requested matching mode."""
        if mode == "exact":
            return self.exact_match(other)
        if mode == "canonical":
            return self.canonical_match(other)
        if mode == "tokens":
            return self.token_match(other)
        if mode == "flexible":
            return self.canonical_match(other) or self.token_jaccard(other) >= 0.8
        raise ValueError(f"Unknown matching mode: {mode}")

    def similarity(self, other: NormalizedEntity | str) -> float:
        """Compute a graduated similarity score [0.0, 1.0] between two entities."""
        other_norm = other if isinstance(other, NormalizedEntity) else normalize_entity(str(other))
        if self.exact_match(other_norm):
            return 1.0
        if self.canonical == other_norm.canonical and self.tokens == other_norm.tokens:
            return 0.95
        if self.canonical_match(other_norm):
            return 0.90
        base_jaccard = self.token_jaccard(other_norm)
        all_jaccard = self.all_token_jaccard(other_norm)
        return max(base_jaccard * 0.85, all_jaccard * 0.80)

    def to_dict(self) -> dict[str, Any]:
        """Convert normalized entity to a serializable dictionary."""
        return {
            "original": self.original,
            "base_name": self.base_name,
            "canonical": self.canonical,
            "canonical_full": self.canonical_full,
            "tokens": list(self.tokens),
            "qualifiers": list(self.qualifiers),
            "qualifier_tokens": list(self.qualifier_tokens),
            "all_tokens": list(self.all_tokens),
            "kind": self.kind.value,
            "metadata": self.metadata,
        }


def normalize_entity(
    name: str,
    *,
    kind: str | EntityKind | None = None,
) -> NormalizedEntity:
    """Normalize any identifier, path, route, or symbol into a structured NormalizedEntity."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Entity name must be a non-empty string")

    raw = name.strip()
    detected = detect_kind(raw) if kind is None else (
        EntityKind(kind) if isinstance(kind, str) else kind
    )

    metadata: dict[str, Any] = {}
    working = raw

    # 1. API Route processing (e.g. "GET /api/v1/auth/service")
    m_http = re.match(r"^(GET|POST|PUT|DELETE|PATCH|OPTIONS|HEAD)\s+(.+)$", working, re.IGNORECASE)
    if m_http:
        metadata["http_method"] = m_http.group(1).upper()
        working = m_http.group(2).strip()

    # 2. Function call stripping (e.g. "foo()" or "foo(arg)" -> "foo")
    working_no_call = re.sub(r"\(\s*.*\)$", "", working).strip()
    if working_no_call != working:
        metadata["is_function_call"] = True
        working = working_no_call

    # 3. Path / Qualifier parsing
    qualifiers: tuple[str, ...] = ()
    base_name = working

    # Normalize Windows backslashes
    norm_path = working.replace("\\", "/")
    # Strip drive letters (e.g. C:/)
    norm_path = re.sub(r"^[a-zA-Z]:", "", norm_path)

    if "/" in norm_path:
        parts = [part for part in norm_path.split("/") if part and part != "."]
        if parts:
            qualifiers = tuple(parts[:-1])
            base_name = parts[-1]
            # Strip file extension from base_name if present
            base_stem, ext = os.path.splitext(base_name)
            if ext.lower() in COMMON_SOURCE_EXTENSIONS:
                metadata["file_extension"] = ext.lower()
                base_name = base_stem
    elif "::" in working:
        parts = [part for part in working.split("::") if part]
        if parts:
            qualifiers = tuple(parts[:-1])
            base_name = parts[-1]
    elif "." in working:
        parts = [part for part in working.split(".") if part]
        # Distinguish dotted qualified class (e.g. backend.models.CodeSymbol)
        # from simple dotted identifier (e.g. auth.service)
        if len(parts) > 1 and any(part[0].isupper() for part in parts[1:]):
            qualifiers = tuple(parts[:-1])
            base_name = parts[-1]
        elif len(parts) > 2:
            qualifiers = tuple(parts[:-1])
            base_name = parts[-1]
        else:
            # Single dotted name like "auth.service" -> treat entire as base_name
            # so canonical matches "authservice" and tokens match ("auth", "service")
            base_name = working
            qualifiers = ()

    # Check for leading/trailing private underscores
    if base_name.startswith("__") and base_name.endswith("__") and len(base_name) > 4:
        metadata["is_dunder"] = True
    elif base_name.startswith("_") and not base_name.startswith("__"):
        metadata["is_private"] = True

    # Tokens and canonical representations
    base_tokens = tokenize_name(base_name)
    qualifier_tokens = tuple(
        token
        for qualifier in qualifiers
        for token in tokenize_name(qualifier)
    )

    all_tokens = _unique_ordered_tokens(qualifier_tokens + base_tokens)
    canonical = to_canonical(base_name)
    canonical_full = to_canonical(raw)

    return NormalizedEntity(
        original=raw,
        base_name=base_name,
        canonical=canonical,
        canonical_full=canonical_full,
        tokens=base_tokens,
        qualifiers=qualifiers,
        qualifier_tokens=qualifier_tokens,
        all_tokens=all_tokens,
        kind=detected,
        metadata=metadata,
    )


def compare_entities(
    entity1: NormalizedEntity | str,
    entity2: NormalizedEntity | str,
) -> dict[str, Any]:
    """Provide a comprehensive comparison between two entity representations."""
    norm1 = entity1 if isinstance(entity1, NormalizedEntity) else normalize_entity(str(entity1))
    norm2 = entity2 if isinstance(entity2, NormalizedEntity) else normalize_entity(str(entity2))

    return {
        "exact_match": norm1.exact_match(norm2),
        "canonical_match": norm1.canonical_match(norm2),
        "token_match": norm1.token_match(norm2),
        "token_jaccard": round(norm1.token_jaccard(norm2), 4),
        "all_token_jaccard": round(norm1.all_token_jaccard(norm2), 4),
        "token_overlap_ratio": round(norm1.token_overlap_ratio(norm2), 4),
        "similarity": round(norm1.similarity(norm2), 4),
        "shared_tokens": sorted(list(set(norm1.tokens) & set(norm2.tokens))),
    }


def are_compatible(
    entity1: NormalizedEntity | str,
    entity2: NormalizedEntity | str,
    threshold: float = 0.8,
) -> bool:
    """Check if two entities are compatible above a given similarity threshold."""
    norm1 = entity1 if isinstance(entity1, NormalizedEntity) else normalize_entity(str(entity1))
    norm2 = entity2 if isinstance(entity2, NormalizedEntity) else normalize_entity(str(entity2))
    return norm1.canonical_match(norm2) or norm1.similarity(norm2) >= threshold
