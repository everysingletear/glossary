#!/usr/bin/env python3
"""
Glossary Query — token-efficient interface to the symbol database.

Outputs compact, scannable symbol listings organized by file.
Designed to be called by the LLM via Bash — output is minimal to save tokens.

Auto-detects project root and database location. No need to pass --db
in most cases.

Usage:
    python glossary_query.py --search "process_*"
    python glossary_query.py --file src/auth.py
    python glossary_query.py --type class
    python glossary_query.py --duplicates
    python glossary_query.py --stats
    python glossary_query.py --recent
    python glossary_query.py --full
    python glossary_query.py --full --verbose
    python glossary_query.py --describe "file:symbol" "Description"
"""

import argparse
import os
import sqlite3
import subprocess
import sys

from glossary_common import (
    _DUP_WHERE,
    escape_like,
    find_project_root,
    format_file_group,
    format_symbol,
    get_db_path,
    group_by_file,
    split_target,
)


# ---------------------------------------------------------------------------
# DB auto-detection and connection
# ---------------------------------------------------------------------------

def find_db(explicit_path: str = None) -> str:
    """Find the glossary database. Auto-detects if no explicit path given."""
    if explicit_path:
        return explicit_path
    return get_db_path()


def connect(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        project_root = find_project_root()
        scanner_path = os.path.join(os.path.dirname(__file__), "glossary_scanner.py")

        if os.path.exists(scanner_path):
            print("Glossary database not found. Initializing full scan...", file=sys.stderr)
            result = subprocess.run(
                [sys.executable, scanner_path, "--project-root", project_root, "--init"],
                capture_output=True, text=True
            )
            if result.stdout:
                print(result.stdout, file=sys.stderr)
            if result.returncode != 0:
                print(f"Scanner failed: {result.stderr}", file=sys.stderr)
                sys.exit(1)
        else:
            print(f"Error: database not found at {db_path}", file=sys.stderr)
            print(f"Scanner not found at {scanner_path}", file=sys.stderr)
            sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_search(conn: sqlite3.Connection, pattern: str, verbose: bool,
               limit: int = None, offset: int = 0):
    """Search symbols by name with wildcard support."""
    if limit is None:
        limit = 100
    sql_pattern = escape_like(pattern).replace("*", "%")
    total = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE symbol_name LIKE ? ESCAPE '\\'",
        (sql_pattern,),
    ).fetchone()[0]
    rows = conn.execute(
        """SELECT * FROM symbols
           WHERE symbol_name LIKE ? ESCAPE '\\'
           ORDER BY file_path, line_number
           LIMIT ? OFFSET ?""",
        (sql_pattern, limit, offset)
    ).fetchall()

    if not rows:
        print(f"No symbols matching '{pattern}'")
        return

    suffix = f" (offset {offset})" if offset > 0 else ""
    print(f"Found {len(rows)} of {total} symbols matching '{pattern}'{suffix}:\n")
    for fp, syms in sorted(group_by_file(rows).items()):
        print(format_file_group(fp, syms, verbose))

    if len(rows) == limit:
        print(f"\n(showing {limit} results from offset {offset}"
              f" — use --offset {offset + limit} for next page)")


def cmd_file(conn: sqlite3.Connection, file_path: str, verbose: bool):
    """Show all symbols in a specific file."""
    file_path = file_path.replace("\\", "/")
    escaped = escape_like(file_path)

    rows = conn.execute(
        """SELECT * FROM symbols
           WHERE file_path = ? OR file_path LIKE ? ESCAPE '\\'
           ORDER BY file_path, line_number""",
        (file_path, f"%/{escaped}")
    ).fetchall()

    if not rows:
        print(f"No symbols found in '{file_path}'")
        return

    for fp, syms in group_by_file(rows).items():
        print(format_file_group(fp, syms, verbose))


def cmd_type(conn: sqlite3.Connection, sym_type: str, verbose: bool,
             limit: int = None, offset: int = 0):
    """Show all symbols of a given type."""
    if limit is None:
        limit = 200
    total = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE symbol_type = ?",
        (sym_type,),
    ).fetchone()[0]
    rows = conn.execute(
        """SELECT * FROM symbols
           WHERE symbol_type = ?
           ORDER BY file_path, line_number
           LIMIT ? OFFSET ?""",
        (sym_type, limit, offset)
    ).fetchall()

    if not rows:
        print(f"No symbols of type '{sym_type}'")
        return

    suffix = f" (offset {offset})" if offset > 0 else ""
    print(f"All {sym_type} symbols ({len(rows)} of {total}{suffix}):\n")
    for fp, syms in sorted(group_by_file(rows).items()):
        print(format_file_group(fp, syms, verbose))

    if len(rows) == limit:
        print(f"\n(showing {limit} results from offset {offset}"
              f" — use --offset {offset + limit} for next page)")


def cmd_duplicates(conn: sqlite3.Connection, verbose: bool, exclude_tests: bool,
                   exclude_migrations: bool, limit: int = 50, offset: int = 0):
    """Find symbols with the same name in different files."""
    include_tests = not exclude_tests
    include_migrations = not exclude_migrations
    where = _DUP_WHERE[(include_tests, include_migrations)]

    # NOTE: {where} is from _DUP_WHERE dict (static constants, not user input) — safe f-string
    total = conn.execute(
        f"""SELECT COUNT(*) FROM (
            SELECT symbol_name, symbol_type
            FROM symbols WHERE {where}
            GROUP BY symbol_name, symbol_type
            HAVING COUNT(DISTINCT file_path) > 1
        )"""
    ).fetchone()[0]
    rows = conn.execute(
        f"""SELECT symbol_name, symbol_type, COUNT(DISTINCT file_path) as file_count
           FROM symbols
           WHERE {where}
           GROUP BY symbol_name, symbol_type
           HAVING file_count > 1
           ORDER BY file_count DESC, symbol_name
           LIMIT ? OFFSET ?""",
        (limit, offset),
    ).fetchall()

    filters = []
    if exclude_tests:
        filters.append("tests")
    if exclude_migrations:
        filters.append("migrations")
    filter_suffix = f" (excluding {', '.join(filters)})" if filters else ""
    offset_suffix = f" (offset {offset})" if offset > 0 else ""

    if not rows:
        print(f"No duplicate symbol names found{filter_suffix}.")
        return

    print(f"Found {len(rows)} of {total} duplicate symbol names{filter_suffix}{offset_suffix}:\n")

    for r in rows:
        print(f"### `{r['symbol_name']}` ({r['symbol_type']}) — in {r['file_count']} files")
        locations = conn.execute(
            f"""SELECT file_path, signature, line_number FROM symbols
               WHERE symbol_name = ? AND symbol_type = ? AND {where}
               ORDER BY file_path""",
            (r["symbol_name"], r["symbol_type"]),
        ).fetchall()
        for loc in locations:
            sig = loc["signature"] or r["symbol_name"]
            line_info = f" (L{loc['line_number']})" if verbose and loc["line_number"] else ""
            print(f"  - {loc['file_path']}: `{sig}`{line_info}")
        print()

    if len(rows) == limit and total > offset + limit:
        print(f"(showing {limit} of {total} groups"
              f" — use --offset {offset + limit} for next page)")


def cmd_stats(conn: sqlite3.Connection):
    """Show summary statistics."""
    file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    sym_count = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    described = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE description IS NOT NULL"
    ).fetchone()[0]
    test_files = conn.execute("SELECT COUNT(*) FROM files WHERE is_test = 1").fetchone()[0]
    migration_files = conn.execute("SELECT COUNT(*) FROM files WHERE is_migration = 1").fetchone()[0]

    type_counts = conn.execute(
        """SELECT symbol_type, COUNT(*) as cnt
           FROM symbols GROUP BY symbol_type ORDER BY cnt DESC"""
    ).fetchall()

    lang_counts = conn.execute(
        """SELECT language, COUNT(*) as cnt, SUM(symbol_count) as syms
           FROM files GROUP BY language ORDER BY cnt DESC"""
    ).fetchall()

    print(f"Glossary: {sym_count} symbols in {file_count} files")
    if described > 0:
        print(f"Described: {described}/{sym_count}")
    if test_files > 0:
        print(f"Test files: {test_files}")
    if migration_files > 0:
        print(f"Migration files: {migration_files}")
    print()

    print("By type:")
    for r in type_counts:
        print(f"  {r['symbol_type']:12s} {r['cnt']}")
    print()

    print("By language:")
    for r in lang_counts:
        lang = r["language"] or "unknown"
        print(f"  {lang:12s} {r['cnt']} files, {r['syms']} symbols")


def cmd_recent(conn: sqlite3.Connection, verbose: bool, limit: int = 5):
    """Show symbols from the most recently scanned files."""
    recent_files = conn.execute(
        "SELECT file_path FROM files ORDER BY last_scanned DESC LIMIT ?",
        (limit,),
    ).fetchall()

    if not recent_files:
        print("No recently scanned symbols.")
        return

    fps = [r["file_path"] for r in recent_files]
    placeholders = ",".join("?" * len(fps))
    rows = conn.execute(
        f"""SELECT s.*, f.last_scanned FROM symbols s
           JOIN files f ON s.file_path = f.file_path
           WHERE s.file_path IN ({placeholders})
           ORDER BY f.last_scanned DESC, s.line_number""",
        fps,
    ).fetchall()

    if not rows:
        print("No recently scanned symbols.")
        return

    by_file = group_by_file(rows)
    print(f"Last {len(by_file)} scanned file(s):\n")
    for fp, syms in by_file.items():
        print(format_file_group(fp, syms, verbose))


def cmd_full(conn: sqlite3.Connection, verbose: bool,
             limit: int = None, offset: int = 0):
    """Dump the full glossary."""
    if limit is None:
        limit = 5000
    total = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    rows = conn.execute(
        """SELECT * FROM symbols ORDER BY file_path, line_number
           LIMIT ? OFFSET ?""",
        (limit, offset)
    ).fetchall()

    if not rows:
        print("Glossary is empty. Run scanner --init to populate.")
        return

    by_file = group_by_file(rows)
    file_count = len(by_file)
    suffix = f" (offset {offset})" if offset > 0 else ""
    print(f"# Full Glossary ({len(rows)} of {total} symbols in {file_count} files{suffix})\n")

    if not verbose and total > 200:
        print("(Compact mode: top-level symbols only; pass --verbose for methods)\n")
        for fp in sorted(by_file.keys()):
            syms = by_file[fp]
            top_level = [s for s in syms if not s["parent"]]
            parts = [f"{s['symbol_name']}({s['symbol_type']})" for s in top_level]
            print(f"**{fp}** — {', '.join(parts)}")
    else:
        for fp in sorted(by_file.keys()):
            print(format_file_group(fp, by_file[fp], verbose=verbose))

    if len(rows) == limit:
        print(f"\n(showing {limit} results from offset {offset}"
              f" — use --offset {offset + limit} for next page)")


def cmd_describe(conn: sqlite3.Connection, target: str, description: str):
    """Add a description to a symbol."""
    file_part, symbol_name = split_target(target)

    if file_part is not None:
        file_path = file_part.replace("\\", "/")
        escaped = escape_like(file_path)
        updated = conn.execute(
            """UPDATE symbols SET description = ?, description_manual = 1
               WHERE (file_path = ? OR file_path LIKE ? ESCAPE '\\') AND symbol_name = ?""",
            (description, file_path, f"%/{escaped}", symbol_name)
        ).rowcount
    else:
        updated = conn.execute(
            """UPDATE symbols SET description = ?, description_manual = 1
               WHERE symbol_name = ?""",
            (description, symbol_name)
        ).rowcount

    conn.commit()

    if updated:
        print(f"Updated description for '{target}' ({updated} row(s))")
    else:
        print(f"Symbol '{target}' not found")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Glossary Query")
    parser.add_argument("--db", help="Path to glossary.db (auto-detected if omitted)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show line numbers and full signatures")
    parser.add_argument("--exclude-tests", action="store_true", default=True,
                        help="Exclude test files from --duplicates (default: on)")
    parser.add_argument("--include-tests", action="store_true",
                        help="Include test files in --duplicates")
    parser.add_argument("--exclude-migrations", action="store_true", default=True,
                        help="Exclude migration files from --duplicates (default: on)")
    parser.add_argument("--include-migrations", action="store_true",
                        help="Include migration files in --duplicates")
    parser.add_argument("--limit", type=int, default=None,
                        help="Maximum results (default: 100 for search/type, 5000 for full)")
    parser.add_argument("--offset", type=int, default=0,
                        help="Skip N results for pagination")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--search", "-s", help="Search symbols by name (* wildcards)")
    group.add_argument("--file", "-f", help="Show symbols in a file")
    group.add_argument("--type", "-t", help="Show all symbols of a type")
    group.add_argument("--duplicates", "-d", action="store_true", help="Find duplicate names")
    group.add_argument("--stats", action="store_true", help="Summary statistics")
    group.add_argument("--recent", "-r", action="store_true", help="Recently changed symbols")
    group.add_argument("--full", action="store_true", help="Full glossary dump")
    group.add_argument("--describe", nargs=2, metavar=("TARGET", "DESC"),
                       help="Add description: 'file:symbol' 'text'")

    args = parser.parse_args()

    db_path = find_db(args.db)
    conn = connect(db_path)

    try:
        if args.search:
            cmd_search(conn, args.search, args.verbose, args.limit, args.offset)
        elif args.file:
            cmd_file(conn, args.file, args.verbose)
        elif args.type:
            cmd_type(conn, args.type, args.verbose, args.limit, args.offset)
        elif args.duplicates:
            exclude_tests = args.exclude_tests and not args.include_tests
            exclude_migrations = args.exclude_migrations and not args.include_migrations
            dup_limit = args.limit if args.limit is not None else 50
            cmd_duplicates(conn, args.verbose, exclude_tests, exclude_migrations,
                           dup_limit, args.offset)
        elif args.stats:
            cmd_stats(conn)
        elif args.recent:
            cmd_recent(conn, args.verbose)
        elif args.full:
            cmd_full(conn, args.verbose, args.limit, args.offset)
        elif args.describe:
            cmd_describe(conn, args.describe[0], args.describe[1])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
