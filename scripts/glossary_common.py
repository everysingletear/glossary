"""
Glossary Common — shared constants, helpers, and formatters used by
the server, CLI, and scanner.

Extracted to avoid duplication across modules (DRY).
"""

import os
import sqlite3


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_RELATIVE_PATH = os.path.join(".claude", "glossary.db")

PROJECT_MARKERS = (
    ".git", "pyproject.toml", "package.json", "Cargo.toml",
    "go.mod", "pom.xml", ".claude",
)

# Pre-defined static WHERE clauses for duplicate queries.
# Keys: (include_tests, include_migrations)
# Whitelist avoids f-string SQL injection risk.
_DUP_WHERE: dict[tuple[bool, bool], str] = {
    (False, False): "parent IS NULL AND is_test = 0 AND is_migration = 0",
    (True,  False): "parent IS NULL AND is_migration = 0",
    (False, True):  "parent IS NULL AND is_test = 0",
    (True,  True):  "parent IS NULL",
}


# ---------------------------------------------------------------------------
# Project root auto-detection
# ---------------------------------------------------------------------------

def find_project_root(start: str | None = None) -> str:
    """Walk up from *start* directory to find the project root.

    Looks for common project markers (.git, pyproject.toml, package.json, …).
    Falls back to *start* (or CWD) if no marker is found.
    """
    current = os.path.abspath(start or os.getcwd())
    while True:
        for marker in PROJECT_MARKERS:
            if os.path.exists(os.path.join(current, marker)):
                return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.abspath(start or os.getcwd())
        current = parent


def get_db_path(project_root: str | None = None) -> str:
    """Return absolute path to the glossary database."""
    root = project_root or find_project_root()
    return os.path.join(root, DB_RELATIVE_PATH)


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def format_symbol(sym: sqlite3.Row, verbose: bool = False, indent: int = 0) -> str:
    """Format a single symbol as a compact line."""
    prefix = "  " * indent + "- "
    sig = sym["signature"] or sym["symbol_name"]
    line = f"{prefix}`{sig}` — {sym['symbol_type']}"
    if sym["description"]:
        line += f" — {sym['description']}"
    if verbose and sym["line_number"]:
        line += f"  (L{sym['line_number']})"
    return line


def format_file_group(file_path: str, symbols: list[sqlite3.Row],
                      verbose: bool = False) -> str:
    """Format all symbols in a file as a markdown section."""
    lines = [f"\n## {file_path} ({len(symbols)} symbols)"]
    top_level = [s for s in symbols if not s["parent"]]
    children: dict[str, list] = {}
    for s in symbols:
        if s["parent"]:
            children.setdefault(s["parent"], []).append(s)
    for sym in top_level:
        lines.append(format_symbol(sym, verbose))
        if sym["symbol_name"] in children:
            for child in children[sym["symbol_name"]]:
                lines.append(format_symbol(child, verbose, indent=1))
    return "\n".join(lines)


def group_by_file(rows: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    """Group symbol rows by file_path, preserving insertion order."""
    by_file: dict[str, list] = {}
    for r in rows:
        by_file.setdefault(r["file_path"], []).append(r)
    return by_file


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def escape_like(value: str) -> str:
    r"""Escape SQL LIKE special characters: \, %, _."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def split_target(target: str) -> tuple[str | None, str]:
    """Split 'file_path:symbol_name' respecting Windows drive letters.

    Returns (file_part, symbol_name).  If no file part, returns (None, target).
    """
    last_colon = target.rfind(":")
    # Avoid splitting on a Windows drive letter colon (e.g. C:\)
    if last_colon > 0 and not (last_colon == 1 and target[0].isalpha()):
        return target[:last_colon], target[last_colon + 1:]
    return None, target
