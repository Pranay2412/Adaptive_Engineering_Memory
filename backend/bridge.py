"""Cross-link document concepts to code symbols by name."""

from __future__ import annotations

import re

from .models import CodeSymbol, DocumentEntity, GraphEdge


def _tokens(value: str) -> set[str]:
    return {token.casefold() for token in re.findall(r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|\d+", value)}


def build_cross_links(
    entities: list[DocumentEntity], symbols: list[CodeSymbol]
) -> list[GraphEdge]:
    """Match exact names or complete name-token sequences in entity names."""
    links: list[GraphEdge] = []
    seen: set[tuple[str, str]] = set()
    normalized_symbols = [(symbol, symbol.name.casefold(), _tokens(symbol.name)) for symbol in symbols]
    for entity in entities:
        entity_name = entity.name.casefold()
        entity_tokens = _tokens(entity.name)
        for symbol, symbol_name, symbol_tokens in normalized_symbols:
            exact = entity_name == symbol_name
            token_match = bool(symbol_tokens) and symbol_tokens <= entity_tokens
            substring_match = len(symbol_name) >= 3 and symbol_name in entity_name
            if not (exact or token_match or substring_match):
                continue
            pair = (entity.id, symbol.id)
            if pair in seen:
                continue
            seen.add(pair)
            links.append(
                GraphEdge(
                    source_id=entity.id,
                    target_id=symbol.id,
                    relation="SPECIFIES",
                    metadata={"confidence": 1.0, "source": "cross_link_bridge"},
                )
            )
    return links