#!/usr/bin/env python3
"""
Glossary Scanner — deterministic symbol extraction from source files.

Parses code files using language-specific parsers (Python ast, JS/TS regex,
optional tree-sitter for other languages) and stores symbols in SQLite.

Designed to run from a hook after every Edit/Write — must be fast and use
zero LLM tokens.

Usage:
    # Full project scan (auto-detects project root from CWD)
    python glossary_scanner.py --full

    # Full scan with explicit root
    python glossary_scanner.py --project-root /path/to/project --full

    # Single file scan (used by hook)
    python glossary_scanner.py --file src/auth.py

    # Scan from hook stdin (receives JSON with file_path)
    python glossary_scanner.py --stdin

    # Initialize: full scan + add .gitignore entry
    python glossary_scanner.py --init
"""

import argparse
import ast
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from glossary_common import DB_RELATIVE_PATH, PROJECT_MARKERS, find_project_root

SKIP_DIRS = {
    "node_modules", "__pycache__", ".venv", "venv", ".env", "env",
    ".git", ".svn", ".hg", "dist", "build", ".next", ".nuxt",
    "coverage", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "target",  # Rust
    "vendor",  # Go, PHP
    ".claude",
}

SKIP_FILE_PATTERNS = [
    re.compile(r"^\."),           # dotfiles
    re.compile(r".*\.min\.js$"),  # minified
    re.compile(r".*\.bundle\."),  # bundles
    re.compile(r".*\.generated\."),
    re.compile(r".*\.d\.ts$"),    # TS declarations
    re.compile(r".*_pb2\.py$"),   # protobuf generated
]

# Patterns for files to tag as "test" (used by --duplicates --exclude-tests)
TEST_PATH_PATTERNS = [
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)__tests__/"),
    re.compile(r"(^|/)spec/"),
    re.compile(r"test_[^/]*\.py$"),
    re.compile(r"[^/]*_test\.py$"),
    re.compile(r"[^/]*\.test\.[jt]sx?$"),
    re.compile(r"[^/]*\.spec\.[jt]sx?$"),
    re.compile(r"[^/]*\.e2e\.[jt]sx?$"),
]

MIGRATION_PATH_PATTERNS = [
    re.compile(r"(^|/)migrations?/"),
    re.compile(r"(^|/)alembic/versions/"),
    re.compile(r"(^|/)versions/.*\.py$"),
]

LANGUAGE_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".gd": "gdscript",
}

# Debounce: skip if last scan of this file was < N seconds ago
DEBOUNCE_SECONDS = 3


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db_path(project_root: str) -> str:
    return os.path.join(project_root, DB_RELATIVE_PATH)


def init_db(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS symbols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            symbol_name TEXT NOT NULL,
            symbol_type TEXT NOT NULL,
            signature TEXT,
            parent TEXT,
            line_number INTEGER,
            description TEXT,
            description_manual INTEGER DEFAULT 0,
            is_test INTEGER DEFAULT 0,
            is_migration INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS files (
            file_path TEXT PRIMARY KEY,
            mtime REAL NOT NULL,
            language TEXT,
            symbol_count INTEGER DEFAULT 0,
            is_test INTEGER DEFAULT 0,
            is_migration INTEGER DEFAULT 0,
            last_scanned TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
        CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(symbol_name);
        CREATE INDEX IF NOT EXISTS idx_symbols_type ON symbols(symbol_type);

        CREATE UNIQUE INDEX IF NOT EXISTS idx_symbols_unique
            ON symbols(file_path, symbol_name, parent);
    """)

    # Migration: add is_test/is_migration columns if missing (for existing DBs)
    try:
        conn.execute("SELECT is_test FROM symbols LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE symbols ADD COLUMN is_test INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE symbols ADD COLUMN is_migration INTEGER DEFAULT 0")
    try:
        conn.execute("SELECT is_test FROM files LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE files ADD COLUMN is_test INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE files ADD COLUMN is_migration INTEGER DEFAULT 0")

    # Migration: add description_manual flag
    try:
        conn.execute("SELECT description_manual FROM symbols LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE symbols ADD COLUMN description_manual INTEGER DEFAULT 0")

    return conn


def classify_file(file_path: str) -> tuple[bool, bool]:
    """Determine if a file is a test file and/or a migration file."""
    is_test = any(p.search(file_path) for p in TEST_PATH_PATTERNS)
    is_migration = any(p.search(file_path) for p in MIGRATION_PATH_PATTERNS)
    return is_test, is_migration


def upsert_file_symbols(conn: sqlite3.Connection, file_path: str,
                        symbols: list[dict], language: str, mtime: float):
    """Replace all symbols for a file atomically."""
    is_test, is_migration = classify_file(file_path)

    # Preserve manual descriptions (set via glossary_describe)
    manual_descs = {}
    for row in conn.execute(
        "SELECT symbol_name, parent, description FROM symbols "
        "WHERE file_path = ? AND description_manual = 1",
        (file_path,)
    ):
        key = (row[0], row[1])
        manual_descs[key] = row[2]

    # Wrap delete+re-insert in an explicit transaction so a crash between
    # them doesn't leave the file with zero symbols until the next scan.
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM symbols WHERE file_path = ?", (file_path,))

        for sym in symbols:
            key = (sym["name"], sym.get("parent"))
            if key in manual_descs:
                desc = manual_descs[key]
                desc_manual = 1
            else:
                desc = sym.get("description")
                desc_manual = 0
            conn.execute(
                """INSERT OR IGNORE INTO symbols
                   (file_path, symbol_name, symbol_type, signature, parent,
                    line_number, description, description_manual,
                    is_test, is_migration)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (file_path, sym["name"], sym["type"], sym.get("signature"),
                 sym.get("parent"), sym.get("line"), desc, desc_manual,
                 int(is_test), int(is_migration))
            )

        conn.execute(
            """INSERT INTO files (file_path, mtime, language, symbol_count,
                                  is_test, is_migration, last_scanned)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(file_path) DO UPDATE SET
                   mtime=excluded.mtime,
                   language=excluded.language,
                   symbol_count=excluded.symbol_count,
                   is_test=excluded.is_test,
                   is_migration=excluded.is_migration,
                   last_scanned=excluded.last_scanned""",
            (file_path, mtime, language, len(symbols),
             int(is_test), int(is_migration))
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def remove_file(conn: sqlite3.Connection, file_path: str):
    """Remove a deleted file from the database."""
    conn.execute("DELETE FROM symbols WHERE file_path = ?", (file_path,))
    conn.execute("DELETE FROM files WHERE file_path = ?", (file_path,))
    conn.commit()


def should_scan(conn: sqlite3.Connection, file_path: str, mtime: float) -> bool:
    """Check if file needs rescanning based on mtime, with debounce.

    Skips rescanning if the file was scanned within DEBOUNCE_SECONDS,
    which prevents redundant scans when an editor writes multiple times
    in rapid succession.
    """
    row = conn.execute(
        "SELECT mtime, last_scanned FROM files WHERE file_path = ?",
        (file_path,)
    ).fetchone()
    if row is None:
        return True
    if row[0] == mtime:
        return False
    # mtime changed — check debounce before rescanning
    if row[1]:
        try:
            last_dt = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
            if elapsed < DEBOUNCE_SECONDS:
                return False
        except (ValueError, TypeError):
            pass
    return True


# ---------------------------------------------------------------------------
# Python parser (ast)
# ---------------------------------------------------------------------------

def _extract_docstring(node) -> str | None:
    """Extract the first line of a docstring from an AST node.

    Returns a trimmed single-line summary (max 200 chars), or None.
    Works for FunctionDef, AsyncFunctionDef, ClassDef, and Module nodes.
    """
    doc = ast.get_docstring(node)
    if not doc:
        return None
    # Take first non-empty line
    first_line = doc.strip().split("\n")[0].strip()
    if not first_line:
        return None
    if len(first_line) > 200:
        first_line = first_line[:197] + "..."
    return first_line


def parse_python(source: str, file_path: str) -> list[dict]:
    """Extract symbols from Python source using the ast module."""
    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError:
        return []

    symbols = []

    def _get_annotation(node) -> str:
        if node is None:
            return ""
        try:
            return ast.unparse(node)
        except Exception:
            return ""

    def _func_signature(node: ast.FunctionDef) -> str:
        args = []
        defaults_offset = len(node.args.args) - len(node.args.defaults)

        for i, arg in enumerate(node.args.args):
            name = arg.arg
            ann = _get_annotation(arg.annotation)
            part = f"{name}: {ann}" if ann else name
            def_idx = i - defaults_offset
            if 0 <= def_idx < len(node.args.defaults):
                try:
                    default = ast.unparse(node.args.defaults[def_idx])
                    if len(default) > 20:
                        default = "..."
                    part += f"={default}"
                except Exception:
                    pass
            args.append(part)

        if node.args.vararg:
            args.append(f"*{node.args.vararg.arg}")
        if node.args.kwonlyargs:
            if not node.args.vararg:
                args.append("*")
            for kw in node.args.kwonlyargs:
                ann = _get_annotation(kw.annotation)
                args.append(f"{kw.arg}: {ann}" if ann else kw.arg)
        if node.args.kwarg:
            args.append(f"**{node.args.kwarg.arg}")

        ret = _get_annotation(node.returns)
        sig = f"{node.name}({', '.join(args)})"
        if ret:
            sig += f" -> {ret}"
        return sig

    def _visit_class(node: ast.ClassDef, parent: str = None):
        bases = []
        for base in node.bases:
            try:
                bases.append(ast.unparse(base))
            except Exception:
                pass
        sig = f"{node.name}({', '.join(bases)})" if bases else node.name

        symbols.append({
            "name": node.name,
            "type": "class",
            "signature": sig,
            "parent": parent,
            "line": node.lineno,
            "description": _extract_docstring(node),
        })

        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Detect decorator-based symbol types
                dec_names = []
                for d in item.decorator_list:
                    if isinstance(d, ast.Name):
                        dec_names.append(d.id)
                    elif isinstance(d, ast.Attribute):
                        dec_names.append(d.attr)

                if "property" in dec_names:
                    sym_type = "property"
                elif "staticmethod" in dec_names:
                    sym_type = "staticmethod"
                elif "classmethod" in dec_names:
                    sym_type = "classmethod"
                else:
                    sym_type = "method"

                symbols.append({
                    "name": item.name,
                    "type": sym_type,
                    "signature": _func_signature(item),
                    "parent": node.name,
                    "line": item.lineno,
                    "description": _extract_docstring(item),
                })
            elif isinstance(item, ast.ClassDef):
                _visit_class(item, parent=node.name)
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        _visit_assignment(target.id, item, parent=node.name)
            elif isinstance(item, ast.AnnAssign):
                if isinstance(item.target, ast.Name):
                    name = item.target.id
                    ann = _get_annotation(item.annotation)
                    sym_type = "const" if name.isupper() else "var"
                    sig = f"{name}: {ann}"
                    if item.value:
                        try:
                            val = ast.unparse(item.value)
                            if len(val) <= 30:
                                sig += f" = {val}"
                        except Exception:
                            pass
                    symbols.append({
                        "name": name,
                        "type": sym_type,
                        "signature": sig,
                        "parent": node.name,
                        "line": item.lineno,
                    })

    def _visit_assignment(name: str, node, parent: str = None):
        sym_type = "const" if name.isupper() else "var"
        sig = name
        try:
            value_str = ast.unparse(node.value)
            if len(value_str) <= 40:
                sig = f"{name} = {value_str}"
            else:
                sig = f"{name} = ..."
        except Exception:
            pass

        symbols.append({
            "name": name,
            "type": sym_type,
            "signature": sig,
            "parent": parent,
            "line": node.lineno,
        })

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append({
                "name": node.name,
                "type": "fn",
                "signature": _func_signature(node),
                "parent": None,
                "line": node.lineno,
                "description": _extract_docstring(node),
            })
        elif isinstance(node, ast.ClassDef):
            _visit_class(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    _visit_assignment(target.id, node)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                name = node.target.id
                ann = _get_annotation(node.annotation)
                sym_type = "const" if name.isupper() else "var"
                sig = f"{name}: {ann}"
                if node.value:
                    try:
                        val = ast.unparse(node.value)
                        if len(val) <= 30:
                            sig += f" = {val}"
                    except Exception:
                        pass
                symbols.append({
                    "name": name,
                    "type": sym_type,
                    "signature": sig,
                    "parent": None,
                    "line": node.lineno,
                })

    return symbols


# ---------------------------------------------------------------------------
# JavaScript / TypeScript parser (regex)
# ---------------------------------------------------------------------------

# Top-level declarations
_JS_TOP_PATTERNS = [
    # function declarations
    (re.compile(
        r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)(?:\s*:\s*(\S+))?",
        re.MULTILINE
    ), "fn", None),
    # class declarations
    (re.compile(
        r"^(?:export\s+)?(?:abstract\s+)?class\s+(\w+)(?:\s+extends\s+(\w+))?(?:\s+implements\s+[\w,\s]+)?",
        re.MULTILINE
    ), "class", None),
    # arrow functions and const assignments
    (re.compile(
        r"^(?:export\s+)?(?:const|let|var)\s+(\w+)(?:\s*:\s*\S+)?\s*=\s*(?:async\s+)?\([^)]*\)\s*(?::\s*\S+\s*)?=>",
        re.MULTILINE
    ), "fn", None),
    # const/let/var (non-arrow)
    (re.compile(
        r"^(?:export\s+)?(?:const|let|var)\s+(\w+)(?:\s*:\s*(\S+))?\s*=\s*(?!.*=>)",
        re.MULTILINE
    ), "var", None),
    # interface
    (re.compile(
        r"^(?:export\s+)?interface\s+(\w+)",
        re.MULTILINE
    ), "interface", None),
    # type alias
    (re.compile(
        r"^(?:export\s+)?type\s+(\w+)\s*(?:<[^>]*>)?\s*=",
        re.MULTILINE
    ), "type", None),
    # enum
    (re.compile(
        r"^(?:export\s+)?(?:const\s+)?enum\s+(\w+)",
        re.MULTILINE
    ), "enum", None),
]

# Class method patterns (indented, inside class body)
_JS_METHOD_PATTERN = re.compile(
    r"^[ \t]+(?:(?:public|private|protected|static|readonly|abstract|async|override)\s+)*"
    r"(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)(?:\s*:\s*([^\s{]+))?",
    re.MULTILINE
)

# Skip these "method" names — they're keywords, not real methods
_JS_METHOD_SKIP = {"if", "for", "while", "switch", "catch", "return", "throw",
                   "new", "delete", "typeof", "import", "export", "from",
                   "const", "let", "var", "class", "function", "else"}


_JSDOC_PATTERN = re.compile(r"/\*\*([\s\S]*?)\*/")


def _extract_jsdoc(source: str, decl_start: int) -> str | None:
    """Extract first line of a JSDoc comment immediately before a declaration.

    Looks backwards from *decl_start* for a /** ... */ block, skipping only
    whitespace between the closing */ and the declaration.
    Returns a trimmed single-line summary (max 200 chars), or None.
    """
    # Look at the 500 chars before the declaration for a JSDoc block
    search_start = max(0, decl_start - 500)
    region = source[search_start:decl_start]
    # Find the last JSDoc block in the region
    matches = list(_JSDOC_PATTERN.finditer(region))
    if not matches:
        return None
    last = matches[-1]
    # Ensure only whitespace between end of JSDoc and declaration
    between = region[last.end():]
    if between.strip():
        return None
    body = last.group(1)
    # Strip leading * from each line, take first non-empty line
    lines = []
    for line in body.split("\n"):
        cleaned = line.strip().lstrip("*").strip()
        if cleaned and not cleaned.startswith("@"):
            lines.append(cleaned)
    if not lines:
        return None
    first_line = lines[0]
    if len(first_line) > 200:
        first_line = first_line[:197] + "..."
    return first_line


def parse_javascript(source: str, file_path: str) -> list[dict]:
    """Extract symbols from JS/TS source using regex patterns."""
    symbols = []
    seen = set()

    # --- Top-level symbols ---
    for pattern, sym_type, _ in _JS_TOP_PATTERNS:
        for match in pattern.finditer(source):
            name = match.group(1)
            if name in seen:
                continue
            seen.add(name)

            line_num = source[:match.start()].count("\n") + 1
            full_match = match.group(0).strip()
            if len(full_match) > 80:
                full_match = full_match[:77] + "..."

            actual_type = sym_type
            if sym_type == "var" and name.isupper():
                actual_type = "const"

            symbols.append({
                "name": name,
                "type": actual_type,
                "signature": full_match if sym_type == "fn" else name,
                "parent": None,
                "line": line_num,
                "description": _extract_jsdoc(source, match.start()),
            })

    # --- Class methods ---
    # Find class bodies and extract methods
    class_pattern = re.compile(
        r"^(?:export\s+)?(?:abstract\s+)?class\s+(\w+)[^{]*\{",
        re.MULTILINE
    )
    for class_match in class_pattern.finditer(source):
        class_name = class_match.group(1)
        # Find the class body (between { and matching })
        start = class_match.end()
        depth = 1
        pos = start
        while pos < len(source) and depth > 0:
            if source[pos] == "{":
                depth += 1
            elif source[pos] == "}":
                depth -= 1
            pos += 1
        class_body = source[start:pos - 1]
        class_start_line = source[:start].count("\n") + 1

        method_seen = set()
        for method_match in _JS_METHOD_PATTERN.finditer(class_body):
            method_name = method_match.group(1)
            if method_name in _JS_METHOD_SKIP or method_name in method_seen:
                continue
            method_seen.add(method_name)

            method_line = class_start_line + class_body[:method_match.start()].count("\n")
            params = method_match.group(2).strip()
            ret = method_match.group(3)
            sig = f"{method_name}({params})"
            if ret:
                sig += f" -> {ret}"
            if len(sig) > 80:
                sig = sig[:77] + "..."

            symbols.append({
                "name": method_name,
                "type": "method",
                "signature": sig,
                "parent": class_name,
                "line": method_line,
                "description": _extract_jsdoc(class_body, method_match.start()),
            })

    symbols.sort(key=lambda s: s.get("line", 0))
    return symbols


# ---------------------------------------------------------------------------
# GDScript parser (Godot 4)
# ---------------------------------------------------------------------------

def _extract_gddoc(source: str, pos: int) -> str:
    """Extract consecutive ## doc-comment lines immediately before pos."""
    lines = source[:pos].splitlines()
    doc_lines = []
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("##"):
            doc_lines.append(stripped[2:].strip())
        else:
            break
    return " ".join(reversed(doc_lines))


# Regex patterns for GDScript 4 constructs
_GD_CLASS_NAME  = re.compile(r"^class_name\s+(\w+)", re.MULTILINE)
_GD_CLASS       = re.compile(r"^([ \t]*)class\s+(\w+)\s*(?:extends\s+\w+)?\s*:", re.MULTILINE)
_GD_FUNC        = re.compile(
    r"^([ \t]*)(?:static\s+)?func\s+(\w+)\s*\(([^)]*)\)\s*(?:->\s*([\w\[\]|, ]+))?\s*:",
    re.MULTILINE,
)
_GD_VAR         = re.compile(
    r"^([ \t]*)(?:@\w+(?:\([^)]*\))?\s+)*var\s+(\w+)(?:\s*:\s*([\w\[\]|, ]+))?(?:\s*=\s*([^\n#]+))?",
    re.MULTILINE,
)
_GD_CONST       = re.compile(
    r"^([ \t]*)const\s+(\w+)(?:\s*:\s*([\w\[\]|, ]+))?\s*=\s*([^\n#]+)",
    re.MULTILINE,
)
_GD_SIGNAL      = re.compile(
    r"^([ \t]*)signal\s+(\w+)(\s*\([^)]*\))?",
    re.MULTILINE,
)
_GD_ENUM        = re.compile(r"^([ \t]*)enum\s+(\w+)\s*\{", re.MULTILINE)


def _gd_indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _gd_extract_block(source_lines: list[str], header_line_idx: int) -> list[tuple[int, str]]:
    """Return (original_line_index, line) pairs for the indented block after header_line_idx.

    Only one nesting level is supported. The block ends when indentation
    drops back to <= the header's indentation level.
    """
    if header_line_idx >= len(source_lines) - 1:
        return []
    header_indent = _gd_indent(source_lines[header_line_idx])
    result = []
    for i in range(header_line_idx + 1, len(source_lines)):
        line = source_lines[i]
        if not line.strip():
            continue  # blank lines don't end the block
        if _gd_indent(line) <= header_indent:
            break
        result.append((i, line))
    return result


def parse_gdscript(source: str, file_path: str) -> list[dict]:
    """Extract symbols from GDScript 4 source using regex patterns."""
    symbols: list[dict] = []
    lines = source.splitlines()

    def line_of(pos: int) -> int:
        return source[:pos].count("\n") + 1

    # --- class_name (file-level, does NOT trigger block extraction) ---
    for m in _GD_CLASS_NAME.finditer(source):
        symbols.append({
            "name": m.group(1),
            "type": "class",
            "signature": m.group(1),
            "parent": None,
            "line": line_of(m.start()),
            "description": _extract_gddoc(source, m.start()),
        })

    # --- named enums ---
    for m in _GD_ENUM.finditer(source):
        indent = len(m.group(1))
        if indent == 0:
            symbols.append({
                "name": m.group(2),
                "type": "class",
                "signature": m.group(2),
                "parent": None,
                "line": line_of(m.start()),
                "description": _extract_gddoc(source, m.start()),
            })

    # --- inner classes (one level of nesting) ---
    for m in _GD_CLASS.finditer(source):
        class_name = m.group(2)
        class_line_idx = line_of(m.start()) - 1  # 0-based index into lines

        symbols.append({
            "name": class_name,
            "type": "class",
            "signature": class_name,
            "parent": None,
            "line": class_line_idx + 1,
            "description": _extract_gddoc(source, m.start()),
        })

        # Extract members of this inner class
        block = _gd_extract_block(lines, class_line_idx)
        if not block:
            continue
        block_source = "\n".join(line for _, line in block)
        first_block_line = block[0][0]  # 0-based

        # Only include symbols at the immediate class-member indent level.
        # Symbols with deeper indent are local variables inside methods — skip them.
        member_indent = _gd_indent(block[0][1])

        for fm in _GD_FUNC.finditer(block_source):
            if len(fm.group(1)) != member_indent:
                continue  # nested inside a method body
            blk_line = block_source[:fm.start()].count("\n")
            params = fm.group(3).strip()
            ret = (fm.group(4) or "").strip()
            sig = f"{fm.group(2)}({params})"
            if ret:
                sig += f" -> {ret}"
            sig = " ".join(sig.split())
            if len(sig) > 80:
                sig = sig[:77] + "..."
            symbols.append({
                "name": fm.group(2),
                "type": "method",
                "signature": sig,
                "parent": class_name,
                "line": first_block_line + blk_line + 1,
                "description": _extract_gddoc(block_source, fm.start()),
            })

        for vm in _GD_VAR.finditer(block_source):
            if len(vm.group(1)) != member_indent:
                continue  # local variable inside a method body
            blk_line = block_source[:vm.start()].count("\n")
            name = vm.group(2)
            type_hint = (vm.group(3) or "").strip()
            val = (vm.group(4) or "").strip()
            sig = name
            if type_hint:
                sig += f": {type_hint}"
            if val and len(val) <= 30:
                sig += f" = {val}"
            symbols.append({
                "name": name,
                "type": "var",
                "signature": sig,
                "parent": class_name,
                "line": first_block_line + blk_line + 1,
                "description": _extract_gddoc(block_source, vm.start()),
            })

        for cm in _GD_CONST.finditer(block_source):
            if len(cm.group(1)) != member_indent:
                continue  # nested inside a method body
            blk_line = block_source[:cm.start()].count("\n")
            val = cm.group(4).strip()
            sig = f"{cm.group(2)} = {val[:30]}"
            symbols.append({
                "name": cm.group(2),
                "type": "const",
                "signature": sig,
                "parent": class_name,
                "line": first_block_line + blk_line + 1,
                "description": _extract_gddoc(block_source, cm.start()),
            })

        for sm in _GD_SIGNAL.finditer(block_source):
            if len(sm.group(1)) != member_indent:
                continue  # nested inside a method body
            blk_line = block_source[:sm.start()].count("\n")
            args = (sm.group(3) or "").strip()
            sig = f"{sm.group(2)}{args}" if args else sm.group(2)
            symbols.append({
                "name": sm.group(2),
                "type": "signal",
                "signature": sig,
                "parent": class_name,
                "line": first_block_line + blk_line + 1,
                "description": _extract_gddoc(block_source, sm.start()),
            })

    # --- top-level symbols (indent == 0) ---
    for m in _GD_FUNC.finditer(source):
        if len(m.group(1)) != 0:
            continue  # skip inner-class members
        params = m.group(3).strip()
        ret = (m.group(4) or "").strip()
        sig = f"{m.group(2)}({params})"
        if ret:
            sig += f" -> {ret}"
        sig = " ".join(sig.split())
        if len(sig) > 80:
            sig = sig[:77] + "..."
        symbols.append({
            "name": m.group(2),
            "type": "fn",
            "signature": sig,
            "parent": None,
            "line": line_of(m.start()),
            "description": _extract_gddoc(source, m.start()),
        })

    for m in _GD_VAR.finditer(source):
        if len(m.group(1)) != 0:
            continue
        name = m.group(2)
        type_hint = (m.group(3) or "").strip()
        val = (m.group(4) or "").strip()
        sig = name
        if type_hint:
            sig += f": {type_hint}"
        if val and len(val) <= 30:
            sig += f" = {val}"
        symbols.append({
            "name": name,
            "type": "var",
            "signature": sig,
            "parent": None,
            "line": line_of(m.start()),
            "description": _extract_gddoc(source, m.start()),
        })

    for m in _GD_CONST.finditer(source):
        if len(m.group(1)) != 0:
            continue
        val = m.group(4).strip()
        sig = f"{m.group(2)} = {val[:30]}"
        symbols.append({
            "name": m.group(2),
            "type": "const",
            "signature": sig,
            "parent": None,
            "line": line_of(m.start()),
        })

    for m in _GD_SIGNAL.finditer(source):
        if len(m.group(1)) != 0:
            continue
        args = (m.group(3) or "").strip()
        sig = f"{m.group(2)}{args}" if args else m.group(2)
        symbols.append({
            "name": m.group(2),
            "type": "signal",
            "signature": sig,
            "parent": None,
            "line": line_of(m.start()),
            "description": _extract_gddoc(source, m.start()),
        })

    symbols.sort(key=lambda s: s.get("line", 0))
    return symbols


# ---------------------------------------------------------------------------
# Tree-sitter parser (optional, multi-language)
# ---------------------------------------------------------------------------

_TREESITTER_AVAILABLE = False
_TS_LANGUAGES = {}

try:
    import tree_sitter
    _TREESITTER_AVAILABLE = True
except ImportError:
    pass


def _get_ts_language(lang_name: str):
    """Try to load a tree-sitter language grammar."""
    if lang_name in _TS_LANGUAGES:
        return _TS_LANGUAGES[lang_name]

    try:
        if lang_name == "python":
            import tree_sitter_python as tsp
            language = tree_sitter.Language(tsp.language())
        elif lang_name == "javascript":
            import tree_sitter_javascript as tsjs
            language = tree_sitter.Language(tsjs.language())
        elif lang_name == "typescript":
            import tree_sitter_typescript as tsts
            language = tree_sitter.Language(tsts.language_typescript())
        elif lang_name == "go":
            import tree_sitter_go as tsgo
            language = tree_sitter.Language(tsgo.language())
        elif lang_name == "rust":
            import tree_sitter_rust as tsrs
            language = tree_sitter.Language(tsrs.language())
        elif lang_name == "java":
            import tree_sitter_java as tsj
            language = tree_sitter.Language(tsj.language())
        elif lang_name == "c":
            import tree_sitter_c as tsc
            language = tree_sitter.Language(tsc.language())
        elif lang_name == "cpp":
            import tree_sitter_cpp as tscpp
            language = tree_sitter.Language(tscpp.language())
        else:
            _TS_LANGUAGES[lang_name] = None
            return None

        _TS_LANGUAGES[lang_name] = language
        return language
    except ImportError:
        _TS_LANGUAGES[lang_name] = None
        return None


_TS_EXT_MAP = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp",
    ".cxx": "cpp", ".hpp": "cpp",
}

_TS_SYMBOL_QUERIES = {
    "python": """
        (function_definition name: (identifier) @fn)
        (class_definition name: (identifier) @class)
        (assignment left: (identifier) @var)
    """,
    "javascript": """
        (function_declaration name: (identifier) @fn)
        (class_declaration name: (identifier) @class)
        (variable_declarator name: (identifier) @var)
        (arrow_function) @arrow
    """,
    "typescript": """
        (function_declaration name: (identifier) @fn)
        (class_declaration name: (identifier) @class)
        (variable_declarator name: (identifier) @var)
        (interface_declaration name: (type_identifier) @interface)
        (type_alias_declaration name: (type_identifier) @type)
        (enum_declaration name: (identifier) @enum)
    """,
    "go": """
        (function_declaration name: (identifier) @fn)
        (method_declaration name: (field_identifier) @method)
        (type_declaration (type_spec name: (type_identifier) @type))
    """,
    "rust": """
        (function_item name: (identifier) @fn)
        (struct_item name: (type_identifier) @class)
        (enum_item name: (type_identifier) @enum)
        (impl_item type: (type_identifier) @impl)
        (trait_item name: (type_identifier) @interface)
        (const_item name: (identifier) @const)
    """,
    "java": """
        (method_declaration name: (identifier) @fn)
        (class_declaration name: (identifier) @class)
        (interface_declaration name: (identifier) @interface)
        (enum_declaration name: (identifier) @enum)
        (field_declaration declarator: (variable_declarator name: (identifier) @var))
        (constructor_declaration name: (identifier) @fn)
    """,
    "c": """
        (function_definition declarator: (function_declarator declarator: (identifier) @fn))
        (struct_specifier name: (type_identifier) @class)
        (enum_specifier name: (type_identifier) @enum)
        (declaration declarator: (init_declarator declarator: (identifier) @var))
        (type_definition declarator: (type_identifier) @type)
    """,
    "cpp": """
        (function_definition declarator: (function_declarator declarator: (qualified_identifier name: (identifier) @fn)))
        (function_definition declarator: (function_declarator declarator: (identifier) @fn))
        (class_specifier name: (type_identifier) @class)
        (struct_specifier name: (type_identifier) @class)
        (enum_specifier name: (type_identifier) @enum)
        (field_declaration declarator: (field_identifier) @var)
        (namespace_definition name: (identifier) @type)
    """,
}


def parse_treesitter(source: str, file_path: str, lang_name: str) -> list[dict] | None:
    """Parse using tree-sitter. Returns None if language not available."""
    if not _TREESITTER_AVAILABLE:
        return None

    language = _get_ts_language(lang_name)
    if language is None:
        return None

    parser = tree_sitter.Parser(language)
    tree = parser.parse(source.encode("utf-8"))

    query_str = _TS_SYMBOL_QUERIES.get(lang_name)
    if not query_str:
        return None

    try:
        query = language.query(query_str)
    except Exception:
        return None

    symbols = []
    seen = set()  # (name, parent) tuples to allow same-name symbols under different parents
    captures = query.captures(tree.root_node)

    # tree-sitter 0.23+ returns dict {capture_name: [nodes]}
    if isinstance(captures, dict):
        items = []
        for capture_name, nodes in captures.items():
            for node in nodes:
                items.append((node, capture_name))
        items.sort(key=lambda x: x[0].start_point[0])
    else:
        items = captures

    for node, capture_name in items:
        name = node.text.decode("utf-8") if hasattr(node.text, "decode") else str(node.text)
        # Determine parent from tree-sitter node hierarchy
        parent = None
        p = node.parent
        while p:
            if p.type in ("class_definition", "class_declaration", "class_specifier",
                          "struct_specifier", "impl_item", "trait_item",
                          "interface_declaration", "enum_declaration"):
                for child in p.children:
                    if child.type in ("identifier", "type_identifier"):
                        parent = child.text.decode("utf-8") if hasattr(child.text, "decode") else str(child.text)
                        break
                break
            p = p.parent

        key = (name, parent)
        if not name or key in seen:
            continue
        seen.add(key)

        sym_type_map = {
            "fn": "fn", "class": "class", "method": "method",
            "var": "var", "const": "const", "interface": "interface",
            "type": "type", "enum": "enum", "impl": "class",
            "arrow": "fn",
        }
        sym_type = sym_type_map.get(capture_name, "var")
        # If we found a parent, methods/fields should be typed accordingly
        if parent and sym_type == "fn":
            sym_type = "method"

        symbols.append({
            "name": name,
            "type": sym_type,
            "signature": name,
            "parent": parent,
            "line": node.start_point[0] + 1,
        })

    return symbols


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def parse_file(file_path: str, project_root: str) -> tuple[list[dict], str] | None:
    """Parse a file and return (symbols, language). Returns None if unsupported."""
    ext = os.path.splitext(file_path)[1].lower()

    abs_path = os.path.join(project_root, file_path) if not os.path.isabs(file_path) else file_path
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except (OSError, IOError):
        return None

    lang = LANGUAGE_MAP.get(ext)

    if ext == ".py":
        return parse_python(source, file_path), "python"
    elif ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        return parse_javascript(source, file_path), lang or "javascript"
    elif ext == ".gd":
        return parse_gdscript(source, file_path), "gdscript"

    ts_lang = _TS_EXT_MAP.get(ext)
    if ts_lang:
        result = parse_treesitter(source, file_path, ts_lang)
        if result is not None:
            return result, ts_lang

    return None


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def discover_files(project_root: str) -> list[str]:
    """Walk the project and find all parseable source files."""
    files = []
    supported_exts = set(LANGUAGE_MAP.keys()) | set(_TS_EXT_MAP.keys())

    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS
        ]

        for filename in filenames:
            ext = os.path.splitext(filename)[1].lower()
            if ext not in supported_exts:
                continue
            if any(p.match(filename) for p in SKIP_FILE_PATTERNS):
                continue

            rel_path = os.path.relpath(
                os.path.join(dirpath, filename), project_root
            ).replace("\\", "/")
            files.append(rel_path)

    return files


# ---------------------------------------------------------------------------
# .gitignore management
# ---------------------------------------------------------------------------

def ensure_gitignore(project_root: str):
    """Add .claude/glossary.db to .gitignore if not already present."""
    gitignore_path = os.path.join(project_root, ".gitignore")
    entry = ".claude/glossary.db"

    if os.path.exists(gitignore_path):
        with open(gitignore_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if entry in content:
            return
        # Append with a newline separator
        if not content.endswith("\n"):
            content += "\n"
        content += f"\n# Glossary database (regenerable)\n{entry}\n"
        with open(gitignore_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Added {entry} to .gitignore")
    else:
        # Only create .gitignore if the project uses git
        if os.path.isdir(os.path.join(project_root, ".git")):
            with open(gitignore_path, "w", encoding="utf-8") as f:
                f.write(f"# Glossary database (regenerable)\n{entry}\n")
            print(f"Created .gitignore with {entry}")


# ---------------------------------------------------------------------------
# Scan orchestration
# ---------------------------------------------------------------------------

def scan_file(conn: sqlite3.Connection, file_path: str, project_root: str,
              force: bool = False) -> bool:
    """Scan a single file. Returns True if scanned, False if skipped."""
    abs_path = os.path.join(project_root, file_path)

    if not os.path.exists(abs_path):
        remove_file(conn, file_path)
        return True

    mtime = os.path.getmtime(abs_path)

    if not force and not should_scan(conn, file_path, mtime):
        return False

    result = parse_file(file_path, project_root)
    if result is None:
        return False

    symbols, language = result
    upsert_file_symbols(conn, file_path, symbols, language, mtime)
    return True


def full_scan(conn: sqlite3.Connection, project_root: str):
    """Scan all files in the project."""
    files = discover_files(project_root)
    scanned = 0
    for f in files:
        if scan_file(conn, f, project_root, force=True):
            scanned += 1

    # Clean up files that no longer exist
    db_files = {row[0] for row in conn.execute("SELECT file_path FROM files").fetchall()}
    project_files = set(files)
    for old_file in db_files - project_files:
        remove_file(conn, old_file)

    total_symbols = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    print(f"Scanned {scanned} files, {total_symbols} symbols total")


# ---------------------------------------------------------------------------
# Hook stdin parser
# ---------------------------------------------------------------------------

def parse_hook_stdin() -> str | None:
    """Extract file_path from hook's JSON stdin."""
    try:
        data = json.load(sys.stdin)
        tool_input = data.get("tool_input", {})
        file_path = tool_input.get("file_path")
        if file_path is None:
            print(
                f"glossary_scanner: no file_path in tool_input. "
                f"Keys received: {list(data.keys())}, "
                f"tool_input keys: {list(tool_input.keys()) if isinstance(tool_input, dict) else 'not a dict'}",
                file=sys.stderr,
            )
        return file_path
    except json.JSONDecodeError as e:
        print(f"glossary_scanner: failed to parse hook stdin — {e}", file=sys.stderr)
        return None
    except AttributeError as e:
        print(f"glossary_scanner: unexpected AttributeError parsing hook stdin — {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Glossary Scanner")
    parser.add_argument("--project-root", help="Project root directory (auto-detected if omitted)")
    parser.add_argument("--full", action="store_true", help="Full project scan")
    parser.add_argument("--init", action="store_true", help="Initialize: full scan + .gitignore")
    parser.add_argument("--file", help="Scan a specific file (relative to project root)")
    parser.add_argument("--stdin", action="store_true", help="Read file path from hook JSON stdin")

    args = parser.parse_args()

    project_root = os.path.abspath(args.project_root) if args.project_root else find_project_root()
    db_path = get_db_path(project_root)
    conn = init_db(db_path)

    try:
        if args.init:
            print(f"Initializing glossary for: {project_root}")
            full_scan(conn, project_root)
            ensure_gitignore(project_root)
            print("Done. Hook setup instructions: see references/setup.md")
        elif args.full:
            full_scan(conn, project_root)
        elif args.stdin:
            file_path = parse_hook_stdin()
            if file_path:
                rel = os.path.relpath(file_path, project_root).replace("\\", "/")
                scanned = scan_file(conn, rel, project_root)
                if scanned:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM symbols WHERE file_path = ?", (rel,)
                    ).fetchone()[0]
                    print(f"Updated {rel}: {count} symbols")
            else:
                print("No file_path in stdin", file=sys.stderr)
        elif args.file:
            rel = args.file.replace("\\", "/")
            scanned = scan_file(conn, rel, project_root, force=True)
            if scanned:
                count = conn.execute(
                    "SELECT COUNT(*) FROM symbols WHERE file_path = ?", (rel,)
                ).fetchone()[0]
                print(f"Updated {rel}: {count} symbols")
            else:
                print(f"Skipped {rel} (unsupported or unchanged)")
        else:
            parser.print_help()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
