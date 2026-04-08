#!/usr/bin/env python3
"""
Glossary Scanner — deterministic symbol extraction from source files.

Parses code files using modular language-specific parsers (scripts/parsers/)
and stores symbols in SQLite.

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
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

from glossary_common import find_project_root, get_db_path
from parsers import parse as registry_parse, SUPPORTED_EXTENSIONS

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

# Debounce: skip if last scan of this file was < N seconds ago
DEBOUNCE_SECONDS = 3


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


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
    """Check if file needs rescanning based on mtime, with debounce."""
    row = conn.execute(
        "SELECT mtime, last_scanned FROM files WHERE file_path = ?",
        (file_path,)
    ).fetchone()
    if row is None:
        return True
    if row[0] == mtime:
        return False
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

    return registry_parse(source, file_path, ext)


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def discover_files(project_root: str) -> list[str]:
    """Walk the project and find all parseable source files."""
    files = []

    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS
        ]

        for filename in filenames:
            ext = os.path.splitext(filename)[1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
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
        if not content.endswith("\n"):
            content += "\n"
        content += f"\n# Glossary database (regenerable)\n{entry}\n"
        with open(gitignore_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Added {entry} to .gitignore")
    else:
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
