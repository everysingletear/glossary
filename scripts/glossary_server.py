#!/usr/bin/env python3
"""
Glossary MCP Server — exposes glossary operations as native MCP tools.

LLM calls these tools directly, no Bash needed. The scanner hook
keeps the database updated automatically; this server handles queries
and descriptions.

Run:
    python glossary_server.py
    # or via Claude Code settings.json mcpServers config
"""

import asyncio
import os
import sqlite3
import subprocess
import sys
from contextlib import asynccontextmanager
from io import StringIO
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP, Context
from pydantic import Field

from glossary_common import (
    DB_RELATIVE_PATH,
    _DUP_WHERE,
    escape_like,
    find_project_root,
    format_file_group,
    format_symbol,
    group_by_file,
    split_target,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCANNER_FILENAME = "glossary_scanner.py"

SymbolType = Literal["fn", "class", "method", "var", "const", "interface", "type", "enum"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_db_path() -> str:
    return os.path.join(find_project_root(), DB_RELATIVE_PATH)


def _get_scanner_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), SCANNER_FILENAME)


def _handle_error(e: Exception) -> str:
    """Consistent error formatting with actionable messages."""
    if isinstance(e, FileNotFoundError):
        return str(e)
    if isinstance(e, sqlite3.OperationalError):
        return (
            f"Error: Database operation failed — {e}. "
            "The database may be corrupted; try glossary_init to rebuild."
        )
    return f"Error: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Lifespan: open DB connection once, reuse across tool calls
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Module-level state (replaces lifespan_context — avoids MCP runtime API churn)
# ---------------------------------------------------------------------------

_db_conn: sqlite3.Connection | None = None
_db_lock: asyncio.Lock | None = None


@asynccontextmanager
async def glossary_lifespan(server: FastMCP):
    """Initialize DB connection on startup; auto-init if DB doesn't exist."""
    global _db_conn, _db_lock

    db_path = _get_db_path()

    if not os.path.exists(db_path):
        scanner = _get_scanner_path()
        root = find_project_root()
        if os.path.exists(scanner):
            await asyncio.to_thread(
                subprocess.run,
                [sys.executable, scanner, "--project-root", root, "--init"],
                capture_output=True, text=True,
            )

    if os.path.exists(db_path):
        # check_same_thread=False is safe: _db_lock serializes all access.
        _db_conn = sqlite3.connect(db_path, check_same_thread=False)
        _db_conn.row_factory = sqlite3.Row
        _db_conn.execute("PRAGMA journal_mode=WAL")
        _db_conn.execute("PRAGMA synchronous=NORMAL")

    _db_lock = asyncio.Lock()

    try:
        yield {}
    finally:
        if _db_conn:
            _db_conn.close()
            _db_conn = None


def _get_db(ctx: Context) -> sqlite3.Connection:
    """Return the module-level DB connection, opening it lazily if needed."""
    global _db_conn
    if _db_conn is None:
        db_path = _get_db_path()
        if not os.path.exists(db_path):
            raise FileNotFoundError(
                f"Glossary database not found at {db_path}. "
                "Use the glossary_init tool to create it, "
                "or check that you're running from inside a project directory."
            )
        _db_conn = sqlite3.connect(db_path, check_same_thread=False)
        _db_conn.row_factory = sqlite3.Row
        _db_conn.execute("PRAGMA journal_mode=WAL")
        _db_conn.execute("PRAGMA synchronous=NORMAL")
    return _db_conn


def _get_lock(ctx: Context) -> asyncio.Lock:
    """Return the module-level DB lock, creating it lazily if needed."""
    global _db_lock
    if _db_lock is None:
        _db_lock = asyncio.Lock()
    return _db_lock


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "glossary_mcp",
    instructions=(
        "Glossary: a persistent symbol registry for the codebase. "
        "Use glossary_search before naming new symbols to avoid collisions. "
        "Use glossary_stats or glossary_full at session start to orient yourself. "
        "Use glossary_file to see what's in a file without reading source code."
    ),
    lifespan=glossary_lifespan,
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    name="glossary_search",
    annotations={
        "title": "Search Symbols",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def glossary_search(
    pattern: Annotated[str, Field(
        description="Symbol name pattern. Use * as wildcard (e.g. 'process_*', '*Logger*', 'get_*').",
        min_length=1,
    )],
    verbose: Annotated[bool, Field(
        description="Include line numbers in output.",
    )] = False,
    limit: Annotated[int, Field(
        ge=1, le=1000,
        description="Maximum number of results to return.",
    )] = 100,
    offset: Annotated[int, Field(
        ge=0,
        description="Number of results to skip (for pagination).",
    )] = 0,
    ctx: Context = None,
) -> str:
    """Search symbols by name with wildcard support.

    Use * wildcards to match symbol names (e.g. 'process_*', '*Logger*', 'get_*_by_id').
    Always search before creating new functions or classes to check for naming conflicts.

    Returns matching symbols grouped by file with type and signature.
    """
    try:
        conn = _get_db(ctx)
        sql_pattern = escape_like(pattern).replace("*", "%")
        lock = _get_lock(ctx)

        def _query() -> tuple[list[sqlite3.Row], int]:
            total = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE symbol_name LIKE ? ESCAPE '\\'",
                (sql_pattern,),
            ).fetchone()[0]
            rows = conn.execute(
                """SELECT * FROM symbols
                   WHERE symbol_name LIKE ? ESCAPE '\\'
                   ORDER BY file_path, line_number
                   LIMIT ? OFFSET ?""",
                (sql_pattern, limit, offset),
            ).fetchall()
            return rows, total

        async with lock:
            rows, total = await asyncio.to_thread(_query)

        if not rows:
            return f"No symbols matching '{pattern}'"

        out = StringIO()
        suffix = f" (offset {offset})" if offset > 0 else ""
        out.write(f"Found {len(rows)} of {total} symbols matching '{pattern}'{suffix}:\n")
        for fp, syms in sorted(group_by_file(rows).items()):
            out.write(format_file_group(fp, syms, verbose))
            out.write("\n")
        if len(rows) == limit and total > offset + limit:
            out.write(f"\n(Showing {limit} of {total}; pass offset={offset + limit} for more)\n")
        return out.getvalue()
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="glossary_file",
    annotations={
        "title": "Symbols In File",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def glossary_file(
    file_path: Annotated[str, Field(
        description="File path or partial name (e.g. 'auth.py' matches 'backend/app/auth.py').",
        min_length=1,
    )],
    verbose: Annotated[bool, Field(
        description="Include line numbers in output.",
    )] = False,
    ctx: Context = None,
) -> str:
    """Show all symbols declared in a specific file.

    Accepts partial paths (e.g. 'auth.py' matches 'backend/app/auth.py').
    When multiple files match, all are shown.
    Much cheaper than reading the source — use this to see what exists
    before reading implementation details.
    """
    try:
        conn = _get_db(ctx)
        normalized = file_path.replace("\\", "/")
        lock = _get_lock(ctx)

        def _query() -> list[sqlite3.Row]:
            escaped = escape_like(normalized)
            return conn.execute(
                "SELECT * FROM symbols WHERE file_path = ? OR file_path LIKE ? ESCAPE '\\' ORDER BY file_path, line_number",
                (normalized, f"%/{escaped}"),
            ).fetchall()

        async with lock:
            rows = await asyncio.to_thread(_query)

        if not rows:
            return f"No symbols found in '{file_path}'. Check the path or run glossary_init."

        by_file = group_by_file(rows)
        out = StringIO()
        for fp, syms in by_file.items():
            out.write(format_file_group(fp, syms, verbose))
            out.write("\n")
        return out.getvalue()
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="glossary_type",
    annotations={
        "title": "Symbols By Type",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def glossary_type(
    symbol_type: Annotated[SymbolType, Field(
        description="Symbol type: fn, class, method, var, const, interface, type, or enum.",
    )],
    verbose: Annotated[bool, Field(
        description="Include line numbers in output.",
    )] = False,
    limit: Annotated[int, Field(
        ge=1, le=1000,
        description="Maximum number of results to return.",
    )] = 200,
    offset: Annotated[int, Field(
        ge=0,
        description="Number of results to skip (for pagination).",
    )] = 0,
    ctx: Context = None,
) -> str:
    """Show all symbols of a given type across the project.

    Valid types: fn, class, method, var, const, interface, type, enum.
    """
    try:
        conn = _get_db(ctx)
        lock = _get_lock(ctx)

        def _query() -> tuple[list[sqlite3.Row], int]:
            total = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE symbol_type = ?",
                (symbol_type,),
            ).fetchone()[0]
            rows = conn.execute(
                """SELECT * FROM symbols
                   WHERE symbol_type = ?
                   ORDER BY file_path, line_number
                   LIMIT ? OFFSET ?""",
                (symbol_type, limit, offset),
            ).fetchall()
            return rows, total

        async with lock:
            rows, total = await asyncio.to_thread(_query)

        if not rows:
            return (
                f"No symbols of type '{symbol_type}'. "
                "Valid types: fn, class, method, var, const, interface, type, enum."
            )

        out = StringIO()
        suffix = f" (offset {offset})" if offset > 0 else ""
        out.write(f"All {symbol_type} symbols ({len(rows)} of {total}{suffix}):\n")
        for fp, syms in sorted(group_by_file(rows).items()):
            out.write(format_file_group(fp, syms, verbose))
            out.write("\n")
        if len(rows) == limit and total > offset + limit:
            out.write(f"\n(Showing {limit} of {total}; pass offset={offset + limit} for more)\n")
        return out.getvalue()
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="glossary_duplicates",
    annotations={
        "title": "Find Duplicate Names",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def glossary_duplicates(
    include_tests: Annotated[bool, Field(
        description="Include symbols from test files (excluded by default).",
    )] = False,
    include_migrations: Annotated[bool, Field(
        description="Include symbols from migration files (excluded by default).",
    )] = False,
    verbose: Annotated[bool, Field(
        description="Include line numbers for each location.",
    )] = False,
    limit: Annotated[int, Field(
        ge=1, le=500,
        description="Maximum number of duplicate groups to return.",
    )] = 50,
    offset: Annotated[int, Field(
        ge=0,
        description="Number of duplicate groups to skip (for pagination).",
    )] = 0,
    ctx: Context = None,
) -> str:
    """Find symbols with the same name AND type in different files.

    Tests and migrations are excluded by default because they naturally
    repeat names (setup, teardown, upgrade, downgrade). Use include_tests
    or include_migrations to see those too.
    """
    try:
        conn = _get_db(ctx)
        where = _DUP_WHERE[(include_tests, include_migrations)]
        lock = _get_lock(ctx)

        def _query():
            total = conn.execute(
                f"""SELECT COUNT(*) FROM (
                       SELECT 1 FROM symbols WHERE {where}
                       GROUP BY symbol_name, symbol_type
                       HAVING COUNT(DISTINCT file_path) > 1
                   )"""
            ).fetchone()[0]
            agg_rows = conn.execute(
                f"""SELECT symbol_name, symbol_type, COUNT(DISTINCT file_path) as file_count
                   FROM symbols WHERE {where}
                   GROUP BY symbol_name, symbol_type
                   HAVING file_count > 1
                   ORDER BY file_count DESC, symbol_name
                   LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()

            details = {}
            for r in agg_rows:
                locations = conn.execute(
                    f"""SELECT file_path, signature, line_number
                        FROM symbols
                        WHERE symbol_name = ? AND symbol_type = ? AND {where}
                        ORDER BY file_path""",
                    (r["symbol_name"], r["symbol_type"]),
                ).fetchall()
                details[(r["symbol_name"], r["symbol_type"])] = locations

            return agg_rows, details, total

        async with lock:
            agg_rows, details, total = await asyncio.to_thread(_query)

        if not agg_rows:
            return "No duplicate symbol names found."

        out = StringIO()
        filters = []
        if not include_tests:
            filters.append("tests")
        if not include_migrations:
            filters.append("migrations")
        filter_suffix = f" (excluding {', '.join(filters)})" if filters else ""
        offset_suffix = f", offset {offset}" if offset > 0 else ""
        out.write(f"Found {len(agg_rows)} of {total} duplicate symbol names{filter_suffix}{offset_suffix}:\n\n")

        for r in agg_rows:
            out.write(f"### `{r['symbol_name']}` ({r['symbol_type']}) — in {r['file_count']} files\n")
            for loc in details[(r["symbol_name"], r["symbol_type"])]:
                sig = loc["signature"] or r["symbol_name"]
                line_info = f" (L{loc['line_number']})" if verbose and loc["line_number"] else ""
                out.write(f"  - {loc['file_path']}: `{sig}`{line_info}\n")
            out.write("\n")

        if len(agg_rows) == limit and total > offset + limit:
            out.write(f"(Showing {limit} of {total} groups; pass offset={offset + limit} for more)\n")

        return out.getvalue()
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="glossary_stats",
    annotations={
        "title": "Glossary Stats",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def glossary_stats(ctx: Context = None) -> str:
    """Quick overview — file count, symbol count by type and language.

    Cheapest query available. Use at session start or after context compaction
    to understand the project scope before diving into specific files.
    Includes staleness indicator so you know when the database was last updated.
    """
    try:
        conn = _get_db(ctx)
        lock = _get_lock(ctx)

        def _query():
            file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            sym_count = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            described = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE description IS NOT NULL"
            ).fetchone()[0]
            test_files = conn.execute(
                "SELECT COUNT(*) FROM files WHERE is_test = 1"
            ).fetchone()[0]
            migration_files = conn.execute(
                "SELECT COUNT(*) FROM files WHERE is_migration = 1"
            ).fetchone()[0]
            last_scan = conn.execute(
                "SELECT MAX(last_scanned) FROM files"
            ).fetchone()[0]
            type_counts = conn.execute(
                """SELECT symbol_type, COUNT(*) as cnt FROM symbols
                   GROUP BY symbol_type ORDER BY cnt DESC"""
            ).fetchall()
            lang_counts = conn.execute(
                """SELECT language, COUNT(*) as cnt, SUM(symbol_count) as syms
                   FROM files GROUP BY language ORDER BY cnt DESC"""
            ).fetchall()
            return (file_count, sym_count, described, test_files,
                    migration_files, last_scan, type_counts, lang_counts)

        async with lock:
            (file_count, sym_count, described, test_files,
             migration_files, last_scan, type_counts, lang_counts) = await asyncio.to_thread(_query)

        out = StringIO()
        out.write(f"Glossary: {sym_count} symbols in {file_count} files\n")
        if last_scan:
            out.write(f"Last updated: {last_scan}\n")
        else:
            out.write("Last updated: never (run glossary_init)\n")
        if described > 0:
            out.write(f"Described: {described}/{sym_count}\n")
        if test_files > 0:
            out.write(f"Test files: {test_files}\n")
        if migration_files > 0:
            out.write(f"Migration files: {migration_files}\n")
        out.write("\nBy type:\n")
        for r in type_counts:
            out.write(f"  {r['symbol_type']:12s} {r['cnt']}\n")
        out.write("\nBy language:\n")
        for r in lang_counts:
            lang = r["language"] or "unknown"
            out.write(f"  {lang:12s} {r['cnt']} files, {r['syms']} symbols\n")
        return out.getvalue()
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="glossary_recent",
    annotations={
        "title": "Recently Changed Symbols",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def glossary_recent(
    limit: Annotated[int, Field(
        ge=1, le=50,
        description="Number of recently scanned files to show (default 5). Returns all symbols from each file.",
    )] = 5,
    verbose: Annotated[bool, Field(
        description="Include line numbers in output.",
    )] = False,
    ctx: Context = None,
) -> str:
    """Show symbols from the most recently scanned files.

    Useful when resuming work mid-session to see what was touched last,
    or to verify that the hook-based scanner is keeping the database current.
    Returns complete files (not a symbol-count cutoff) so you always see
    full context.
    """
    try:
        conn = _get_db(ctx)
        lock = _get_lock(ctx)

        def _query() -> list[sqlite3.Row]:
            recent_files = conn.execute(
                "SELECT file_path FROM files ORDER BY last_scanned DESC LIMIT ?",
                (limit,),
            ).fetchall()
            if not recent_files:
                return []
            fps = [r["file_path"] for r in recent_files]
            placeholders = ",".join("?" * len(fps))
            return conn.execute(
                f"""SELECT s.*, f.last_scanned FROM symbols s
                   JOIN files f ON s.file_path = f.file_path
                   WHERE s.file_path IN ({placeholders})
                   ORDER BY f.last_scanned DESC, s.line_number""",
                fps,
            ).fetchall()

        async with lock:
            rows = await asyncio.to_thread(_query)

        if not rows:
            return "No recently scanned symbols."

        out = StringIO()
        by_file = group_by_file(rows)
        out.write(f"Last {len(by_file)} scanned file(s):\n")
        for fp, syms in by_file.items():
            out.write(format_file_group(fp, syms, verbose))
            out.write("\n")
        return out.getvalue()
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="glossary_full",
    annotations={
        "title": "Full Glossary Dump",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def glossary_full(
    verbose: Annotated[bool, Field(
        description=(
            "Show full signatures and line numbers. "
            "Also bypasses compact format on large projects — use to force full output."
        ),
    )] = False,
    limit: Annotated[int, Field(
        ge=1, le=10000,
        description="Maximum number of symbols to return (default 5000).",
    )] = 5000,
    offset: Annotated[int, Field(
        ge=0,
        description="Number of symbols to skip for pagination.",
    )] = 0,
    ctx: Context = None,
) -> str:
    """Full glossary dump — every symbol in the project.

    For large projects (200+ symbols), auto-switches to compact format
    showing top-level symbols as name(type) per file (methods are hidden
    to save tokens). Pass verbose=True to force full signatures, line numbers,
    and all methods regardless of project size.
    Use limit/offset for pagination on very large projects (5000+ symbols).
    """
    try:
        conn = _get_db(ctx)
        lock = _get_lock(ctx)

        def _query() -> tuple[list[sqlite3.Row], int]:
            total = conn.execute(
                "SELECT COUNT(*) FROM symbols",
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM symbols ORDER BY file_path, line_number LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return rows, total

        async with lock:
            rows, total = await asyncio.to_thread(_query)

        if not rows:
            return "Glossary is empty. Use glossary_init to populate."

        by_file = group_by_file(rows)
        file_count = len(by_file)
        sorted_files = sorted(by_file.keys())

        out = StringIO()
        suffix = f" (offset {offset})" if offset > 0 else ""
        out.write(f"# Full Glossary ({len(rows)} of {total} symbols in {file_count} files{suffix})\n\n")
        if len(rows) == limit and total > offset + limit:
            out.write(f"(Showing {limit} of {total}; pass offset={offset + limit} for more)\n\n")

        if not verbose and total > 200:
            out.write("(Compact mode: top-level symbols only; pass verbose=True for methods)\n\n")
            for i, fp in enumerate(sorted_files):
                if ctx and i % 20 == 0:
                    await ctx.report_progress(i, file_count)
                syms = by_file[fp]
                top_level = [s for s in syms if not s["parent"]]
                parts = [f"{s['symbol_name']}({s['symbol_type']})" for s in top_level]
                out.write(f"**{fp}** — {', '.join(parts)}\n")
        else:
            for i, fp in enumerate(sorted_files):
                if ctx and i % 20 == 0:
                    await ctx.report_progress(i, file_count)
                out.write(format_file_group(fp, by_file[fp], verbose=verbose))
                out.write("\n")

        return out.getvalue()
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="glossary_describe",
    annotations={
        "title": "Add Symbol Description",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def glossary_describe(
    target: Annotated[str, Field(
        description=(
            "Symbol to describe. Format: 'file_path:symbol_name' or just 'symbol_name'. "
            "Windows paths are handled correctly (C:\\path\\file.py:func_name works)."
        ),
        min_length=1,
    )],
    description: Annotated[str, Field(
        description="One-line description of what this symbol does.",
        min_length=1,
        max_length=200,
    )],
    ctx: Context = None,
) -> str:
    """Set a 1-line description on a symbol.

    Target format: 'file_path:symbol_name' or just 'symbol_name'.
    Only describe ambiguous names (run, handle, process) — clear names
    like get_user_by_id don't need descriptions.
    Calling again with a different description replaces the previous one.
    """
    try:
        conn = _get_db(ctx)
        lock = _get_lock(ctx)
        file_part, symbol_name = split_target(target)

        def _update() -> tuple[int, list[str]]:
            if file_part is not None:
                file_norm = file_part.replace("\\", "/")
                file_escaped = escape_like(file_norm)
                affected = conn.execute(
                    "SELECT file_path FROM symbols WHERE (file_path = ? OR file_path LIKE ? ESCAPE '\\') AND symbol_name = ?",
                    (file_norm, f"%/{file_escaped}", symbol_name),
                ).fetchall()
                if not affected:
                    return 0, []
                updated = conn.execute(
                    "UPDATE symbols SET description = ?, description_manual = 1 WHERE (file_path = ? OR file_path LIKE ? ESCAPE '\\') AND symbol_name = ?",
                    (description, file_norm, f"%/{file_escaped}", symbol_name),
                ).rowcount
                conn.commit()
                return updated, [r["file_path"] for r in affected]
            else:
                updated = conn.execute(
                    "UPDATE symbols SET description = ?, description_manual = 1 WHERE symbol_name = ?",
                    (description, symbol_name),
                ).rowcount
                conn.commit()
                return updated, []

        async with lock:
            updated, paths = await asyncio.to_thread(_update)

        if updated:
            if paths:
                return f"Updated description for '{symbol_name}' in: {', '.join(paths)}"
            return f"Updated description for '{target}' ({updated} row(s))"
        return f"Symbol '{target}' not found. Check the name with glossary_search."
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="glossary_init",
    annotations={
        "title": "Initialize Glossary",
        "readOnlyHint": False,
        "destructiveHint": True,   # rebuilds all symbol rows (descriptions are preserved)
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def glossary_init(ctx: Context = None) -> str:
    """Initialize or rebuild the glossary database.

    Scans all source files in the project, creates .claude/glossary.db,
    and adds it to .gitignore. Run once per project, or after branch
    switches and major refactors. Safe to re-run — existing descriptions
    are preserved across rebuilds.
    """
    try:
        scanner = _get_scanner_path()
        root = find_project_root()

        if not os.path.exists(scanner):
            return f"Error: Scanner not found at {scanner}."

        if ctx:
            await ctx.report_progress(0, 3)

        lock = _get_lock(ctx)

        def _run_scanner():
            return subprocess.run(
                [sys.executable, scanner, "--project-root", root, "--init"],
                capture_output=True, text=True,
            )

        async with lock:
            result = await asyncio.to_thread(_run_scanner)

        if result.returncode != 0:
            return f"Error: Scanner failed — {result.stderr.strip()}"

        if ctx:
            await ctx.report_progress(2, 3)

        # Reconnect: the scanner recreated the DB, so the old connection is stale.
        db_path = os.path.join(root, DB_RELATIVE_PATH)
        if os.path.exists(db_path):
            new_conn = sqlite3.connect(db_path, check_same_thread=False)
            new_conn.row_factory = sqlite3.Row
            new_conn.execute("PRAGMA journal_mode=WAL")
            new_conn.execute("PRAGMA synchronous=NORMAL")
            async with lock:
                global _db_conn
                old_conn = _db_conn
                _db_conn = new_conn
                if old_conn:
                    old_conn.close()

        if ctx:
            await ctx.report_progress(3, 3)

        return result.stdout.strip() or "Initialization complete."
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="glossary_enrich",
    annotations={
        "title": "Enrich Undescribed Symbols",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def glossary_enrich(
    file_path: Annotated[str | None, Field(
        description="Limit to a specific file (partial path OK). Omit to scan all files.",
    )] = None,
    symbol_type: Annotated[SymbolType | None, Field(
        description="Limit to a specific symbol type (fn, class, method, etc.).",
    )] = None,
    limit: Annotated[int, Field(
        ge=1, le=50,
        description="Max symbols to return per call (default 20). Keep small for focused descriptions.",
    )] = 20,
    context_lines: Annotated[int, Field(
        ge=5, le=50,
        description="Lines of source to include after each symbol definition (default 15).",
    )] = 15,
    ctx: Context = None,
) -> str:
    """Return undescribed symbols with source context for LLM description generation.

    Finds symbols that have no description (neither from docstrings nor manual),
    reads source lines around each symbol, and returns them formatted for the LLM
    to generate descriptions. After generating, save them with glossary_describe_batch.

    Workflow: glossary_enrich → LLM generates descriptions → glossary_describe_batch.
    """
    try:
        conn = _get_db(ctx)
        lock = _get_lock(ctx)
        root = find_project_root()

        def _query() -> list[sqlite3.Row]:
            conditions = ["description IS NULL"]
            params: list = []
            if file_path:
                normalized = file_path.replace("\\", "/")
                escaped = escape_like(normalized)
                conditions.append(
                    "(file_path = ? OR file_path LIKE ? ESCAPE '\\')"
                )
                params.extend([normalized, f"%/{escaped}"])
            if symbol_type:
                conditions.append("symbol_type = ?")
                params.append(symbol_type)

            where = " AND ".join(conditions)
            params.append(limit)
            return conn.execute(
                f"""SELECT file_path, symbol_name, symbol_type, signature,
                           parent, line_number
                    FROM symbols WHERE {where}
                    ORDER BY file_path, line_number
                    LIMIT ?""",
                params,
            ).fetchall()

        async with lock:
            rows = await asyncio.to_thread(_query)

        if not rows:
            scope = f" in '{file_path}'" if file_path else ""
            return f"All symbols{scope} already have descriptions."

        # Read source context for each symbol
        out = StringIO()
        out.write(f"# {len(rows)} symbols need descriptions\n\n")
        out.write("Generate a 1-line description (max 100 tokens) for each symbol.\n")
        out.write("Then call `glossary_describe_batch` with the results.\n\n")

        file_cache: dict[str, list[str]] = {}
        for row in rows:
            fp = row["file_path"]
            if fp not in file_cache:
                abs_path = os.path.join(root, fp)
                try:
                    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                        file_cache[fp] = f.readlines()
                except OSError:
                    file_cache[fp] = []

            lines = file_cache[fp]
            start = max(0, (row["line_number"] or 1) - 1)
            end = min(len(lines), start + context_lines)
            snippet = "".join(lines[start:end]).rstrip()

            parent_info = f" (in {row['parent']})" if row["parent"] else ""
            out.write(f"## `{row['symbol_name']}` — {row['symbol_type']}{parent_info}\n")
            out.write(f"**File:** {fp}:{row['line_number']}\n")
            out.write(f"```\n{snippet}\n```\n\n")

        return out.getvalue()
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="glossary_describe_batch",
    annotations={
        "title": "Batch Describe Symbols",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def glossary_describe_batch(
    descriptions: Annotated[str, Field(
        description=(
            "JSON array of objects: [{\"target\": \"file:symbol\", \"description\": \"...\"}]. "
            "Target format: 'file_path:symbol_name' or just 'symbol_name'."
        ),
        min_length=2,
    )],
    ctx: Context = None,
) -> str:
    """Save multiple symbol descriptions in one call.

    Accepts a JSON array of {target, description} objects.
    Each description is marked as manual (survives rescans).
    Use after glossary_enrich to save LLM-generated descriptions.
    """
    try:
        import json as _json
        items = _json.loads(descriptions)
        if not isinstance(items, list):
            return "Error: expected a JSON array of {target, description} objects."

        conn = _get_db(ctx)
        lock = _get_lock(ctx)

        def _batch_update() -> tuple[int, int]:
            updated = 0
            skipped = 0
            for item in items:
                target = item.get("target", "")
                desc = item.get("description", "")
                if not target or not desc:
                    skipped += 1
                    continue

                file_part, symbol_name = split_target(target)
                if file_part is not None:
                    file_norm = file_part.replace("\\", "/")
                    file_escaped = escape_like(file_norm)
                    cnt = conn.execute(
                        "UPDATE symbols SET description = ?, description_manual = 1 "
                        "WHERE (file_path = ? OR file_path LIKE ? ESCAPE '\\') "
                        "AND symbol_name = ?",
                        (desc, file_norm, f"%/{file_escaped}", symbol_name),
                    ).rowcount
                else:
                    cnt = conn.execute(
                        "UPDATE symbols SET description = ?, description_manual = 1 "
                        "WHERE symbol_name = ?",
                        (desc, symbol_name),
                    ).rowcount
                if cnt:
                    updated += cnt
                else:
                    skipped += 1
            conn.commit()
            return updated, skipped

        async with lock:
            updated, skipped = await asyncio.to_thread(_batch_update)

        result = f"Updated {updated} symbol(s)."
        if skipped:
            result += f" Skipped {skipped} (not found or empty)."
        return result
    except Exception as e:
        return _handle_error(e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
