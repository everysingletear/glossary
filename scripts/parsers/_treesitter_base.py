"""Shared tree-sitter engine for all tree-sitter language parsers.

Provides dynamic grammar loading, query execution, and symbol extraction
(signature, docstring, parent scope) that individual language parsers
build on top of.
"""

from __future__ import annotations

import importlib
import re
from typing import Any

# ---------------------------------------------------------------------------
# Language cache
# ---------------------------------------------------------------------------
_LANG_CACHE: dict[str, Any] = {}


def load_language(package_name: str, func_name: str = "language") -> Any:
    """Import a tree-sitter grammar package and return a ``Language`` object.

    Most grammars expose ``language()``, but some differ:
      - ``tree_sitter_typescript.language_typescript()``
      - ``tree_sitter_php.language_php()``

    Results are cached so repeated calls are cheap.
    """
    cache_key = f"{package_name}.{func_name}"
    cached = _LANG_CACHE.get(cache_key)
    if cached is not None:
        return cached

    module = importlib.import_module(package_name)
    raw = getattr(module, func_name)()

    from tree_sitter import Language

    lang = Language(raw)
    _LANG_CACHE[cache_key] = lang
    return lang


# ---------------------------------------------------------------------------
# Default capture-name -> symbol-type mapping
# ---------------------------------------------------------------------------
_DEFAULT_TYPE_MAP: dict[str, str] = {
    "fn": "fn",
    "class": "class",
    "method": "method",
    "var": "var",
    "const": "const",
    "interface": "interface",
    "type": "type",
    "enum": "enum",
    "impl": "class",
    "arrow": "fn",
    "struct": "class",
    "trait": "interface",
    "module": "class",
    "object": "class",
}

# Regex that strips common comment markers from the beginning of a line.
_COMMENT_STRIP_RE = re.compile(
    r"^\s*(?:///|///?|/\*\*|\*/|/\*|\*/|##|#)\s?",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_signature(node: Any, source_bytes: bytes) -> str:
    """Return the first line of the parent definition, cleaned up."""
    parent = node.parent
    if parent is None:
        return node.text.decode("utf-8", errors="replace")

    full = parent.text.decode("utf-8", errors="replace")
    first_line = full.split("\n", 1)[0]
    first_line = first_line.rstrip("{: \t")
    if len(first_line) > 120:
        first_line = first_line[:117] + "..."
    return first_line


def _extract_comment(
    node: Any,
    source_bytes: bytes,
    comment_prefix: str = "//",
) -> str | None:
    """Look for a doc-comment immediately before the symbol's definition.

    Uses ``node.parent.prev_sibling`` (not ``prev_named_sibling``) because
    comments are typically unnamed/extra nodes in tree-sitter grammars.
    """
    parent = node.parent
    if parent is None:
        return None

    sibling = parent.prev_sibling
    if sibling is None:
        return None

    if "comment" not in sibling.type:
        return None

    raw = sibling.text.decode("utf-8", errors="replace")

    # Walk lines, strip markers, return first non-empty line.
    for line in raw.splitlines():
        cleaned = _COMMENT_STRIP_RE.sub("", line).strip()
        if cleaned:
            return cleaned[:200]

    return None


def _find_parent_name(node: Any, parent_types: tuple[str, ...]) -> str | None:
    """Walk up the tree looking for an enclosing scope of given types."""
    cursor = node.parent
    while cursor is not None:
        if cursor.type in parent_types:
            # Try common field-name conventions across languages.
            for field in ("name", "type_identifier", "identifier", "field_identifier"):
                child = cursor.child_by_field_name(field)
                if child is not None:
                    return child.text.decode("utf-8", errors="replace")
            # Fallback: scan named children for an identifier-like node.
            for child in cursor.named_children:
                if child.type in ("identifier", "type_identifier", "field_identifier"):
                    return child.text.decode("utf-8", errors="replace")
            return None
        cursor = cursor.parent
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def parse_treesitter(
    source: str,
    file_path: str,
    language_name: str,
    ts_language: Any,
    queries: str,
    comment_prefix: str = "//",
    parent_types: tuple[str, ...] = ("class_definition", "class_declaration"),
    type_map: dict[str, str] | None = None,
) -> list[dict]:
    """Parse *source* with tree-sitter and return extracted symbols.

    Parameters
    ----------
    source:
        The source code as a string.
    file_path:
        Path to the file (for context; not read from disk).
    language_name:
        Human-readable language name (e.g. ``"go"``).
    ts_language:
        A ``tree_sitter.Language`` object obtained via :func:`load_language`.
    queries:
        A tree-sitter S-expression query string.  Capture names are mapped
        to symbol types via *type_map*.
    comment_prefix:
        The line-comment prefix for stripping doc-comments (``"//"``).
    parent_types:
        Node types considered enclosing scopes (classes, structs, etc.).
    type_map:
        Mapping from query capture name to symbol type string.
        Captures whose name is absent from the map are silently skipped.
    """
    if type_map is None:
        type_map = _DEFAULT_TYPE_MAP

    if not source:
        return []

    source_bytes = source.encode("utf-8")

    from tree_sitter import Parser

    parser = Parser(ts_language)
    tree = parser.parse(source_bytes)

    captures = _run_query(ts_language, queries, tree.root_node)

    symbols: list[dict] = []
    for capture_name, node in captures:
        sym_type = type_map.get(capture_name)
        if sym_type is None:
            continue

        name = node.text.decode("utf-8", errors="replace")
        signature = _extract_signature(node, source_bytes)
        parent = _find_parent_name(node, parent_types)
        line = node.start_point[0] + 1
        description = _extract_comment(node, source_bytes, comment_prefix)

        symbols.append(
            {
                "name": name,
                "type": sym_type,
                "signature": signature,
                "parent": parent,
                "line": line,
                "description": description,
            }
        )

    return symbols


def _run_query(
    ts_language: Any,
    query_str: str,
    root_node: Any,
) -> list[tuple[str, Any]]:
    """Execute a tree-sitter query and return ``(capture_name, node)`` pairs.

    Handles the API difference between tree-sitter < 0.23 (old ``query.captures``
    returning ``[(node, name), ...]``) and >= 0.23 / 0.25 (``QueryCursor``
    returning ``{name: [nodes]}``) transparently.
    """
    # ------------------------------------------------------------------
    # Try the modern API first (tree-sitter >= 0.25 with QueryCursor).
    # ------------------------------------------------------------------
    try:
        from tree_sitter import Query, QueryCursor

        q = Query(ts_language, query_str)
        cursor = QueryCursor(q)
        result = cursor.captures(root_node)

        # result is dict[str, list[Node]]
        if isinstance(result, dict):
            pairs: list[tuple[str, Any]] = []
            for name, nodes in result.items():
                for node in nodes:
                    pairs.append((name, node))
            # Sort by position so symbols come out in source order.
            pairs.sort(key=lambda p: (p[1].start_point[0], p[1].start_point[1]))
            return pairs
    except (ImportError, AttributeError):
        pass

    # ------------------------------------------------------------------
    # Fallback: old API (tree-sitter < 0.23).
    # lang.query(...).captures(root) -> [(node, capture_name), ...]
    # ------------------------------------------------------------------
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        q = ts_language.query(query_str)
        raw = q.captures(root_node)

    # Old API: list of (node, name) tuples
    if isinstance(raw, list):
        return [(name, node) for node, name in raw]

    # Unexpected format — treat as dict just in case.
    if isinstance(raw, dict):
        pairs = []
        for name, nodes in raw.items():
            for node in nodes:
                pairs.append((name, node))
        pairs.sort(key=lambda p: (p[1].start_point[0], p[1].start_point[1]))
        return pairs

    return []
