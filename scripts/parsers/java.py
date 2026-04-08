"""Java parser — tree-sitter based."""
import sys
from parsers._treesitter_base import load_language, parse_treesitter

EXTENSIONS = [".java"]
LANGUAGE = "java"

# Capture the name identifier of each declaration.
# class_declaration:       (class_declaration name: (identifier) @class)
# interface_declaration:   (interface_declaration name: (identifier) @interface)
# enum_declaration:        (enum_declaration name: (identifier) @enum)
# method_declaration:      (method_declaration name: (identifier) @method)
# constructor_declaration: (constructor_declaration name: (identifier) @method)
# field_declaration:       captures the variable declarator identifier as @var
_QUERIES = """
(class_declaration name: (identifier) @class)
(interface_declaration name: (identifier) @interface)
(enum_declaration name: (identifier) @enum)
(method_declaration name: (identifier) @method)
(constructor_declaration name: (identifier) @method)
(field_declaration (variable_declarator name: (identifier) @var))
"""

# class_declaration/interface_declaration/enum_declaration serve as enclosing
# scopes for methods and fields, but we keep them so that nested classes can
# report their outer class as parent. The self-reference on top-level types
# (where the type is its own parent) is cosmetically odd but harmless.
_PARENT_TYPES = (
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
)


def parse(source: str, file_path: str) -> list[dict]:
    try:
        lang = load_language("tree_sitter_java")
    except ImportError:
        print("glossary: tree-sitter-java not available", file=sys.stderr)
        return []
    return parse_treesitter(
        source, file_path, LANGUAGE, lang, _QUERIES, "//", _PARENT_TYPES
    )
