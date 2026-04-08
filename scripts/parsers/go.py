"""Go parser — tree-sitter based."""
import sys
from parsers._treesitter_base import load_language, parse_treesitter

EXTENSIONS = [".go"]
LANGUAGE = "go"

# Capture the name identifier of each declaration.
# function_declaration:  (function_declaration name: (identifier) @fn)
# method_declaration:    (method_declaration name: (field_identifier) @method)
# type_spec:             inside type_declaration, name: (type_identifier) @class
_QUERIES = """
(function_declaration name: (identifier) @fn)
(method_declaration name: (field_identifier) @method)
(type_spec name: (type_identifier) @class)
"""

# Go methods are declared at package level with a receiver — there is no
# meaningful enclosing class scope to report, so _PARENT_TYPES is empty.
# (type_spec is intentionally excluded to avoid self-reference artifacts.)
_PARENT_TYPES = ()


def parse(source: str, file_path: str) -> list[dict]:
    try:
        lang = load_language("tree_sitter_go")
    except ImportError:
        print("glossary: tree-sitter-go not available", file=sys.stderr)
        return []
    return parse_treesitter(
        source, file_path, LANGUAGE, lang, _QUERIES, "//", _PARENT_TYPES
    )
