"""JavaScript/TypeScript parser — tree-sitter based."""
import sys
import warnings

from parsers._treesitter_base import load_language, parse_treesitter

EXTENSIONS = [".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"]
LANGUAGE = "javascript"
LANGUAGE_MAP = {
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
}

# Parent node types used to resolve which class a method belongs to.
# method_definition is a child of class_body, which is a child of class_declaration.
# The base engine walks up and finds the first node in this tuple.
_PARENT_TYPES = ("class_declaration",)

# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

# JavaScript: function/class/method/const/var.
# We use two separate variable_declarator captures so const gets its own type.
_JS_QUERIES = """
(function_declaration name: (identifier) @fn)
(class_declaration name: (identifier) @class)
(method_definition name: (property_identifier) @method)
(lexical_declaration "const" (variable_declarator name: (identifier) @const))
(lexical_declaration "let" (variable_declarator name: (identifier) @var))
(variable_declaration (variable_declarator name: (identifier) @var))
"""

# TypeScript: everything from JS plus interface/type alias/enum.
# Classes in TS use type_identifier for their name, not identifier.
_TS_QUERIES = """
(function_declaration name: (identifier) @fn)
(class_declaration name: (type_identifier) @class)
(method_definition name: (property_identifier) @method)
(lexical_declaration "const" (variable_declarator name: (identifier) @const))
(lexical_declaration "let" (variable_declarator name: (identifier) @var))
(variable_declaration (variable_declarator name: (identifier) @var))
(interface_declaration name: (type_identifier) @interface)
(type_alias_declaration name: (type_identifier) @type)
(enum_declaration name: (identifier) @enum)
"""

# Capture name -> symbol type override on top of the default map.
# The base _DEFAULT_TYPE_MAP already handles fn/class/method/var/const/interface/type/enum.
_TYPE_MAP: dict[str, str] = {
    "fn": "fn",
    "class": "class",
    "method": "method",
    "var": "var",
    "const": "const",
    "interface": "interface",
    "type": "type",
    "enum": "enum",
}


def parse(source: str, file_path: str) -> list[dict]:
    """Parse a JS/TS file and return extracted symbols.

    Selects the correct grammar based on file extension:
      .ts / .tsx  -> tree_sitter_typescript
      everything else -> tree_sitter_javascript
    """
    ext = ""
    if "." in file_path:
        ext = "." + file_path.rsplit(".", 1)[-1].lower()

    # --- Load grammar ---
    try:
        if ext == ".tsx":
            ts_language = load_language("tree_sitter_typescript", "language_tsx")
            queries = _TS_QUERIES
            lang_label = "typescript"
        elif ext == ".ts":
            ts_language = load_language("tree_sitter_typescript", "language_typescript")
            queries = _TS_QUERIES
            lang_label = "typescript"
        else:
            ts_language = load_language("tree_sitter_javascript")
            queries = _JS_QUERIES
            lang_label = "javascript"
    except ImportError as exc:
        warnings.warn(
            f"javascript parser: could not load grammar for {ext!r}: {exc}",
            stacklevel=2,
        )
        return []

    return parse_treesitter(
        source=source,
        file_path=file_path,
        language_name=lang_label,
        ts_language=ts_language,
        queries=queries,
        comment_prefix="//",
        parent_types=_PARENT_TYPES,
        type_map=_TYPE_MAP,
    )
