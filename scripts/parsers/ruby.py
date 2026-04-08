"""Ruby parser — tree-sitter based."""
import sys
from parsers._treesitter_base import load_language, parse_treesitter

EXTENSIONS = [".rb"]
LANGUAGE = "ruby"

# Ruby class/module names are captured as "constant" nodes.
# method/def: (method name: (identifier) @method)
# singleton method (def self.foo): (singleton_method name: (identifier) @method)
# module-level constant assignment: (assignment left: (constant) @const right: _)
_QUERIES = """
(class name: (constant) @class)
(module name: (constant) @module)
(method name: (identifier) @method)
(singleton_method name: (identifier) @method)
(assignment left: (constant) @const right: _)
"""

_PARENT_TYPES = (
    "class",
    "module",
)


def parse(source: str, file_path: str) -> list[dict]:
    try:
        lang = load_language("tree_sitter_ruby")
    except ImportError:
        print("glossary: tree-sitter-ruby not available", file=sys.stderr)
        return []
    return parse_treesitter(
        source, file_path, LANGUAGE, lang, _QUERIES, "#", _PARENT_TYPES
    )
