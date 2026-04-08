"""Swift parser — tree-sitter based.

NOTE: tree-sitter-swift is at v0.0.1 (low maturity). The grammar can fail on
certain constructs. The parse() function wraps everything in try/except and
returns [] with a warning on any failure.
"""
import sys
from parsers._treesitter_base import load_language, parse_treesitter

EXTENSIONS = [".swift"]
LANGUAGE = "swift"

# class_declaration covers class, struct, and enum (all use the same node type).
# protocol_declaration is distinct.
# function_declaration covers top-level and class member functions.
# protocol_function_declaration covers protocol method requirements.
# property_declaration uses pattern > simple_identifier for the name.
_QUERIES = """
(class_declaration (type_identifier) @class)
(protocol_declaration (type_identifier) @interface)
(function_declaration (simple_identifier) @fn)
(protocol_function_declaration (simple_identifier) @method)
(property_declaration (pattern (simple_identifier) @var))
"""

_PARENT_TYPES = (
    "class_declaration",
    "protocol_declaration",
)


def parse(source: str, file_path: str) -> list[dict]:
    try:
        lang = load_language("tree_sitter_swift")
    except ImportError:
        print("glossary: tree-sitter-swift not available", file=sys.stderr)
        return []
    except Exception as exc:
        print(f"glossary: tree-sitter-swift failed to load: {exc}", file=sys.stderr)
        return []

    try:
        return parse_treesitter(
            source, file_path, LANGUAGE, lang, _QUERIES, "//", _PARENT_TYPES
        )
    except Exception as exc:
        print(
            f"glossary: swift parser failed on {file_path}: {exc}",
            file=sys.stderr,
        )
        return []
