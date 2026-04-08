"""C# parser — tree-sitter based."""
import sys
from parsers._treesitter_base import load_language, parse_treesitter

EXTENSIONS = [".cs"]
LANGUAGE = "csharp"

# Capture the name identifier of each declaration.
# class_declaration:     (class_declaration name: (identifier) @class)
# interface_declaration: (interface_declaration name: (identifier) @interface)
# enum_declaration:      (enum_declaration name: (identifier) @enum)
# method_declaration:    (method_declaration name: (identifier) @method)
# constructor_declaration: (constructor_declaration name: (identifier) @method)
# property_declaration:  name identifier of property (field/auto-property)
# field_declaration:     variable declarator inside field declarations
_QUERIES = """
(class_declaration name: (identifier) @class)
(interface_declaration name: (identifier) @interface)
(enum_declaration name: (identifier) @enum)
(struct_declaration name: (identifier) @class)
(record_declaration name: (identifier) @class)
(method_declaration name: (identifier) @method)
(constructor_declaration name: (identifier) @method)
(property_declaration name: (identifier) @var)
(field_declaration (variable_declaration (variable_declarator name: (identifier) @var)))
"""

_PARENT_TYPES = (
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "struct_declaration",
    "record_declaration",
)


def parse(source: str, file_path: str) -> list[dict]:
    try:
        lang = load_language("tree_sitter_c_sharp")
    except ImportError:
        print("glossary: tree-sitter-c-sharp not available", file=sys.stderr)
        return []
    return parse_treesitter(
        source, file_path, LANGUAGE, lang, _QUERIES, "//", _PARENT_TYPES
    )
