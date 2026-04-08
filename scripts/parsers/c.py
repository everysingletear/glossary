"""C and C++ parser — tree-sitter based.

Uses tree_sitter_c for .c/.h files and tree_sitter_cpp for .cpp/.cc/.cxx/.hpp.
Both share the same query patterns since the C++ grammar is a superset of C.
"""
import os
import sys
from parsers._treesitter_base import load_language, parse_treesitter

EXTENSIONS = [".c", ".h", ".cpp", ".cc", ".cxx", ".hpp"]
LANGUAGE = "c"

# Maps file extension to language name (for display) and grammar package.
LANGUAGE_MAP = {
    ".c":   ("c",   "tree_sitter_c"),
    ".h":   ("c",   "tree_sitter_c"),
    ".cpp": ("cpp", "tree_sitter_cpp"),
    ".cc":  ("cpp", "tree_sitter_cpp"),
    ".cxx": ("cpp", "tree_sitter_cpp"),
    ".hpp": ("cpp", "tree_sitter_cpp"),
}

# --- C queries ---
# function_definition:  function_declarator -> identifier
# struct_specifier:     type_identifier
# enum_specifier:       type_identifier
# type_definition:      last type_identifier (the alias name)
_C_QUERIES = """
(function_definition declarator: (function_declarator declarator: (identifier) @fn))
(struct_specifier name: (type_identifier) @class)
(enum_specifier name: (type_identifier) @enum)
"""

# --- C++ queries (superset of C, adds class_specifier and namespace_definition) ---
_CPP_QUERIES = """
(function_definition declarator: (function_declarator declarator: (identifier) @fn))
(struct_specifier name: (type_identifier) @class)
(enum_specifier name: (type_identifier) @enum)
(class_specifier name: (type_identifier) @class)
(namespace_definition name: (namespace_identifier) @module)
"""

_C_PARENT_TYPES = (
    "struct_specifier",
    "enum_specifier",
)

_CPP_PARENT_TYPES = (
    "struct_specifier",
    "enum_specifier",
    "class_specifier",
    "namespace_definition",
)


def parse(source: str, file_path: str) -> list[dict]:
    ext = os.path.splitext(file_path)[1].lower()
    lang_name, pkg_name = LANGUAGE_MAP.get(ext, ("c", "tree_sitter_c"))

    try:
        lang = load_language(pkg_name)
    except ImportError:
        print(f"glossary: {pkg_name} not available", file=sys.stderr)
        return []

    if lang_name == "cpp":
        return parse_treesitter(
            source, file_path, lang_name, lang, _CPP_QUERIES, "//", _CPP_PARENT_TYPES
        )
    return parse_treesitter(
        source, file_path, lang_name, lang, _C_QUERIES, "//", _C_PARENT_TYPES
    )
