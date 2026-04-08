"""Rust parser — tree-sitter based."""
import sys
from parsers._treesitter_base import load_language, parse_treesitter

EXTENSIONS = [".rs"]
LANGUAGE = "rust"

# Capture the name identifier of each declaration.
# Functions at module level:   anchored to source_file to exclude impl methods.
# Structs:                     (struct_item name: (type_identifier) @struct)
# Enums:                       (enum_item name: (type_identifier) @enum)
# Traits:                      (trait_item name: (type_identifier) @trait)
# Constants:                   (const_item name: (identifier) @const)
# Impl blocks:                 (impl_item type: (type_identifier) @impl)
# Methods inside impl blocks:  nested function_item — captured as @method.
#
# The (source_file ...) anchor on @fn prevents impl methods from matching
# both @fn and @method (which would cause duplicates).
_QUERIES = """
(source_file (function_item name: (identifier) @fn))
(struct_item name: (type_identifier) @struct)
(enum_item name: (type_identifier) @enum)
(trait_item name: (type_identifier) @trait)
(const_item name: (identifier) @const)
(impl_item type: (type_identifier) @impl)
(impl_item (declaration_list (function_item name: (identifier) @method)))
"""

# Only impl_item and trait_item are meaningful enclosing scopes for methods.
# struct_item/enum_item are excluded to avoid self-reference artifacts
# (capturing the name of a struct and then finding the struct itself as parent).
_PARENT_TYPES = (
    "impl_item",
    "trait_item",
)


def parse(source: str, file_path: str) -> list[dict]:
    try:
        lang = load_language("tree_sitter_rust")
    except ImportError:
        print("glossary: tree-sitter-rust not available", file=sys.stderr)
        return []
    return parse_treesitter(
        source, file_path, LANGUAGE, lang, _QUERIES, "//", _PARENT_TYPES
    )
