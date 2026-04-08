"""PHP parser — tree-sitter based."""
import sys
from parsers._treesitter_base import load_language, parse_treesitter

EXTENSIONS = [".php"]
LANGUAGE = "php"

# tree_sitter_php uses a non-standard API: language_php() instead of language().
# PHP name nodes have type "name" (not "identifier") for class/function names.
#
# class_declaration:    (class_declaration name: (name) @class)
# interface_declaration:(interface_declaration name: (name) @interface)
# trait_declaration:    (trait_declaration name: (name) @trait)
# function_definition:  top-level functions (function_definition name: (name) @fn)
# method_declaration:   class methods (method_declaration name: (name) @method)
# const_element:        class/global constants
# property_element:     class properties via variable_name > name
_QUERIES = """
(class_declaration name: (name) @class)
(interface_declaration name: (name) @interface)
(trait_declaration name: (name) @trait)
(function_definition name: (name) @fn)
(method_declaration name: (name) @method)
(const_element (name) @const)
(property_element (variable_name (name) @var))
"""

_PARENT_TYPES = (
    "class_declaration",
    "interface_declaration",
    "trait_declaration",
)


def parse(source: str, file_path: str) -> list[dict]:
    try:
        # Non-standard API: tree_sitter_php exposes language_php(), not language()
        lang = load_language("tree_sitter_php", "language_php")
    except ImportError:
        print("glossary: tree-sitter-php not available", file=sys.stderr)
        return []
    return parse_treesitter(
        source, file_path, LANGUAGE, lang, _QUERIES, "//", _PARENT_TYPES
    )
