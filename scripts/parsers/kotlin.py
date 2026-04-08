"""Kotlin parser — tree-sitter based."""
import sys
from parsers._treesitter_base import load_language, parse_treesitter

EXTENSIONS = [".kt", ".kts"]
LANGUAGE = "kotlin"

# Kotlin uses class_declaration for class/interface/data class/enum class.
# object_declaration covers singleton objects.
# function_declaration covers both top-level and member functions.
# property_declaration uses variable_declaration > identifier for the name.
#
# Note: interface, data class, and enum class all produce class_declaration nodes;
# the keyword difference (interface/data/enum) is in child nodes, not the node type.
_QUERIES = """
(class_declaration name: (identifier) @class)
(object_declaration name: (identifier) @class)
(function_declaration name: (identifier) @fn)
(property_declaration (variable_declaration (identifier) @var))
"""

_PARENT_TYPES = (
    "class_declaration",
    "object_declaration",
)


def parse(source: str, file_path: str) -> list[dict]:
    try:
        lang = load_language("tree_sitter_kotlin")
    except ImportError:
        print("glossary: tree-sitter-kotlin not available", file=sys.stderr)
        return []
    return parse_treesitter(
        source, file_path, LANGUAGE, lang, _QUERIES, "//", _PARENT_TYPES
    )
