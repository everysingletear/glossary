# Glossary Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all issues identified in dual-framework review (mcp-builder + skill-creator) — server hardening, SKILL.md improvements, and eval rewrite.

**Architecture:** Three independent workstreams: (1) MCP server fixes — pagination total_count, duplicates pagination, _get_lock safety, connection leak; (2) SKILL.md improvements — examples, negative triggers, tighter description; (3) Eval rewrite — self-contained fixtures, missing scenarios.

**Tech Stack:** Python 3.14, FastMCP, SQLite, pytest (for evals structure)

---

## File Structure

| File | Responsibility | Changes |
|------|---------------|---------|
| `scripts/glossary_server.py` | MCP server | Add total_count to pagination, add pagination to duplicates, fix _get_lock, fix connection leak in glossary_init |
| `scripts/glossary_common.py` | Shared helpers | Add `count_matching()` helper, add stderr warning in `find_project_root` |
| `scripts/glossary_query.py` | CLI fallback | Add pagination params to cmd_search/cmd_type/cmd_full for consistency |
| `SKILL.md` | Skill definition | Add example output, negative triggers, tighten description |
| `references/setup.md` | Setup guide | Add troubleshooting section |
| `evals/evals.json` | Test cases | Rewrite with self-contained setup, add missing scenarios |

---

## Task 1: Add `total_count` to Paginated Server Tools

Currently `glossary_search`, `glossary_type`, `glossary_full` detect "more results" by checking `len(rows) == limit` — the LLM doesn't know how many total results exist.

**Files:**
- Modify: `scripts/glossary_server.py:154-219` (glossary_search)
- Modify: `scripts/glossary_server.py:277-340` (glossary_type)
- Modify: `scripts/glossary_server.py:566-646` (glossary_full)

- [ ] **Step 1: Add COUNT query to `glossary_search`**

Inside the `_query` closure at `glossary_search` (line ~199), add a total count query alongside the main query. Update the output header to show `"Found N of TOTAL symbols"` instead of `"Found N symbols"`. When `total > limit`, show exact total and next offset.

```python
def _query():
    total = conn.execute(
        """SELECT COUNT(*) FROM symbols
           WHERE symbol_name LIKE ? ESCAPE '\\'""",
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
```

Update the output formatting after the lock block:

```python
async with lock:
    rows, total = await asyncio.to_thread(_query)

if not rows:
    return f"No symbols matching '{pattern}'"

out = StringIO()
suffix = f" (offset {offset})" if offset > 0 else ""
out.write(f"Found {len(rows)} of {total} symbols matching '{pattern}'{suffix}:\n")
# ... existing group_by_file formatting ...
if len(rows) == limit and total > offset + limit:
    out.write(f"\n(Showing {limit} of {total}; pass offset={offset + limit} for more)\n")
```

- [ ] **Step 2: Apply same pattern to `glossary_type`**

Same approach — add `COUNT(*)` with `WHERE symbol_type = ?`, return `(rows, total)`, update header to `"All {type} symbols ({len(rows)} of {total}{suffix}):"`.

- [ ] **Step 3: Apply same pattern to `glossary_full`**

Add `SELECT COUNT(*) FROM symbols` (no WHERE needed), update header to `"# Full Glossary ({len(rows)} of {total} symbols in {file_count} files{suffix})"`.

- [ ] **Step 4: Verify compilation**

Run: `python -m py_compile scripts/glossary_server.py`
Expected: No output (success)

- [ ] **Step 5: Commit**

```bash
git add scripts/glossary_server.py
git commit -m "feat: add total_count to paginated tool responses

LLM now sees exact total for better pagination decisions."
```

---

## Task 2: Add Pagination to `glossary_duplicates`

The only listing tool without `limit`/`offset`. Unbounded output for projects with many duplicates.

**Files:**
- Modify: `scripts/glossary_server.py:343-423` (glossary_duplicates)

- [ ] **Step 1: Add `limit` and `offset` params**

Add to function signature after `verbose`:

```python
limit: Annotated[int, Field(
    ge=1, le=500,
    description="Maximum number of duplicate groups to return.",
)] = 50,
offset: Annotated[int, Field(
    ge=0,
    description="Number of duplicate groups to skip (for pagination).",
)] = 0,
```

- [ ] **Step 2: Apply LIMIT/OFFSET to the aggregate query**

Add `LIMIT ? OFFSET ?` to the aggregate query and pass `(limit, offset)` as params. Also add a total count:

```python
def _query():
    total = conn.execute(
        # NOTE: {where} is from _DUP_WHERE dict (static constants, not user input) — safe f-string
        f"""SELECT COUNT(*) FROM (
            SELECT symbol_name, symbol_type
            FROM symbols WHERE {where}
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
    # ... details query unchanged ...
    return agg_rows, details, total
```

- [ ] **Step 3: Update output formatting**

```python
async with lock:
    agg_rows, details, total = await asyncio.to_thread(_query)

# ... existing header ...
out.write(f"Found {len(agg_rows)} of {total} duplicate symbol names{suffix}:\n\n")
# ... existing formatting ...
if len(agg_rows) == limit and total > offset + limit:
    out.write(f"\n(Showing {limit} of {total} groups; pass offset={offset + limit} for more)\n")
```

- [ ] **Step 4: Verify compilation**

Run: `python -m py_compile scripts/glossary_server.py`
Expected: No output (success)

- [ ] **Step 5: Commit**

```bash
git add scripts/glossary_server.py
git commit -m "feat: add pagination to glossary_duplicates

Consistent with other listing tools — prevents unbounded output."
```

---

## Task 3: Fix `_get_lock` Safety and Connection Leak

Two bugs: `_get_lock` silently creates useless new Lock on lifespan failure; `glossary_init` leaks old DB connection.

**Files:**
- Modify: `scripts/glossary_server.py:129-131` (_get_lock)
- Modify: `scripts/glossary_server.py:766-779` (glossary_init reconnection block)

- [ ] **Step 1: Fix `_get_lock` to raise on missing lock**

Replace the fallback `or asyncio.Lock()` with an explicit error:

```python
def _get_lock(ctx: Context) -> asyncio.Lock:
    """Return the database lock from lifespan state."""
    lock = ctx.request_context.lifespan_state.get("db_lock")
    if lock is None:
        raise RuntimeError("DB lock not initialized — server lifespan may have failed")
    return lock
```

- [ ] **Step 2: Fix connection leak in `glossary_init`**

In the reconnection block (around line 769), close the old connection under the lock before replacing:

```python
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
    if ctx:
        async with lock:
            old_conn = ctx.request_context.lifespan_state.get("db")
            ctx.request_context.lifespan_state["db"] = new_conn
            if old_conn:
                old_conn.close()
    else:
        new_conn.close()
```

Note: the reconnect block now acquires the lock separately from the scanner run, so reads aren't blocked during the scan — only during the connection swap.

- [ ] **Step 3: Add comment explaining `check_same_thread=False` safety**

At lifespan line ~91:

```python
# check_same_thread=False is safe because asyncio.Lock serializes all
# access — only one asyncio.to_thread call touches the connection at a time.
conn = sqlite3.connect(db_path, check_same_thread=False)
```

- [ ] **Step 4: Verify compilation**

Run: `python -m py_compile scripts/glossary_server.py`
Expected: No output (success)

- [ ] **Step 5: Commit**

```bash
git add scripts/glossary_server.py
git commit -m "fix: _get_lock raises on missing lock, close leaked connection in glossary_init

_get_lock previously created a useless new Lock silently.
glossary_init now properly closes old connection under lock."
```

---

## Task 4: Add Warning to `find_project_root` Fallback

Silent CWD fallback can create DB in unexpected locations.

**Files:**
- Modify: `scripts/glossary_common.py:37-51` (find_project_root)

- [ ] **Step 1: Add stderr warning on fallback**

```python
import sys

def find_project_root(start: str | None = None) -> str:
    """Walk up from *start* directory to find the project root.

    Looks for common project markers (.git, pyproject.toml, package.json, ...).
    Falls back to *start* (or CWD) if no marker is found.
    """
    current = os.path.abspath(start or os.getcwd())
    while True:
        for marker in PROJECT_MARKERS:
            if os.path.exists(os.path.join(current, marker)):
                return current
        parent = os.path.dirname(current)
        if parent == current:
            fallback = os.path.abspath(start or os.getcwd())
            print(
                f"Warning: No project marker found. Using {fallback} as project root.",
                file=sys.stderr,
            )
            return fallback
        current = parent
```

- [ ] **Step 2: Verify compilation**

Run: `python -m py_compile scripts/glossary_common.py`
Expected: No output (success)

- [ ] **Step 3: Commit**

```bash
git add scripts/glossary_common.py
git commit -m "fix: warn on stderr when project root falls back to CWD

Helps diagnose DB created in wrong directory."
```

---

## Task 5: Add CLI Pagination Consistency

CLI fallback lacks pagination that server has. Add `--limit` and `--offset` to main commands.

**Files:**
- Modify: `scripts/glossary_query.py` (cmd_search, cmd_type, cmd_full, argparse setup)

- [ ] **Step 1: Add `--limit` and `--offset` to argparse**

In the `main()` function's argparse setup, add global optional args:

```python
parser.add_argument("--limit", type=int, default=None,
                    help="Maximum results (default: 100 for search/type, 5000 for full)")
parser.add_argument("--offset", type=int, default=0,
                    help="Skip N results for pagination")
```

- [ ] **Step 2: Apply to `cmd_search`**

Add `limit` and `offset` params, apply `LIMIT ? OFFSET ?` to the SQL query. Use default limit=100 if not specified. Add pagination hint to output when `len(rows) == limit`.

- [ ] **Step 3: Apply to `cmd_type`**

Same pattern — default limit=200 matching server.

- [ ] **Step 4: Apply to `cmd_full`**

Same pattern — default limit=5000 matching server.

- [ ] **Step 5: Verify compilation**

Run: `python -m py_compile scripts/glossary_query.py`
Expected: No output (success)

- [ ] **Step 6: Commit**

```bash
git add scripts/glossary_query.py
git commit -m "feat: add --limit/--offset pagination to CLI commands

Consistent with MCP server pagination behavior."
```

---

## Task 6: Improve SKILL.md — Example Output, Negative Triggers, Tighter Description

**Files:**
- Modify: `SKILL.md`

- [ ] **Step 1: Shorten frontmatter description**

Replace the wall-of-text description with a concise 2-sentence version. Move trigger phrases into the body:

```yaml
description: Generate and query a persistent glossary of all declared symbols in the codebase. Use proactively before naming new symbols, after context compaction, at session start, or when the user asks about existing functions, classes, variables, or naming collisions. Do NOT use when the user needs to understand HOW code works — the glossary only tracks WHAT exists.
```

- [ ] **Step 2: Add concrete example output**

After the `glossary_search` tool description, add:

```markdown
**Example:**
```
> glossary_search("process_*")

Found 3 of 3 symbols matching 'process_*':
**backend/app/pipeline.py**
  fn  process_batch(items: list[dict], batch_size: int = 100)
  fn  process_single(item: dict) -> Result

**backend/app/worker.py**
  fn  process_queue(queue_name: str, max_retries: int = 3)
```
```

- [ ] **Step 3: Add negative triggers section**

After "## When to query", add:

```markdown
## When NOT to use the glossary

- **Understanding implementation logic** — the glossary shows WHAT exists, not HOW it works. Use source reading or Serena's `find_symbol` with `include_body=True` for implementation details.
- **Exploring code structure/architecture** — for directory trees, module dependencies, or architectural overview, use Serena's `get_symbols_overview` or directory exploration tools.
- **Finding specific code patterns** — for regex searches in source code, use `search_for_pattern` (Serena) or `Grep`. The glossary only indexes symbol names and signatures, not code bodies.
```

- [ ] **Step 4: Narrow "code structure" trigger**

In the description, replace "asks about the code structure" with "asks about what symbols, functions, or classes exist in the codebase".

- [ ] **Step 5: Verify SKILL.md is still under 500 lines**

Run: `wc -l SKILL.md`
Expected: Under 150 lines (currently 118, adding ~30 lines)

- [ ] **Step 6: Commit**

```bash
git add SKILL.md
git commit -m "docs: add example output, negative triggers, tighten SKILL.md description

Reduces false positive triggering and helps LLM understand tool output format."
```

---

## Task 7: Add Troubleshooting to setup.md

**Files:**
- Modify: `references/setup.md`

- [ ] **Step 1: Add troubleshooting section at the end**

```markdown
## Troubleshooting

### MCP server not starting
- **`ModuleNotFoundError: No module named 'mcp'`** — The MCP package was installed into a different Python than the one running the server. Check: `python -c "import mcp; print(mcp.__file__)"` using the same interpreter from `settings.json`.
- **Server starts but no tools appear** — Verify the path in `settings.json` points to `glossary_server.py`, not the skill directory. Restart Claude Code after changing MCP settings.

### Hook doesn't fire
- **Matcher typo** — Must be exactly `Edit|Write` (case-sensitive). Check `.claude/settings.json` for typos.
- **Wrong script path** — Hook runs from project root CWD. Use absolute path to `glossary_scanner.py`.
- **No file_path in stdin** — The hook reads `file_path` from the tool's JSON input. If using a custom tool not listed in the matcher, its input may not contain `file_path`.

### Database seems stale
- **After git operations** — Run `glossary_init` after branch switches, merges, or rebases.
- **After file deletion** — The hook only fires on Edit/Write. Deleted files leave orphan entries until the next `glossary_init`.
- **Check last scan time** — `glossary_stats` shows when each file was last scanned.

### Database not found
- **No `.claude/` directory** — Run `glossary_init` or `glossary_scanner.py --init` from the project root.
- **Wrong project root** — The server detects project root from CWD using markers (.git, pyproject.toml, etc.). If no marker found, it falls back to CWD with a warning.
```

- [ ] **Step 2: Commit**

```bash
git add references/setup.md
git commit -m "docs: add troubleshooting section to setup guide

Covers common issues: MCP startup, hook failures, stale DB, missing DB."
```

---

## Task 8: Rewrite Evals — Self-Contained + Missing Scenarios

Current evals depend on `g:/python_projects/reMind` existing. Need self-contained setup and more scenarios.

**Files:**
- Modify: `evals/evals.json`

- [ ] **Step 1: Design self-contained eval approach**

Each eval should:
1. Work against the glossary project itself (it's a Python project with known symbols)
2. Not depend on any external project
3. Reference symbols that exist in `scripts/*.py`

- [ ] **Step 2: Rewrite evals.json**

```json
{
  "skill_name": "glossary",
  "evals": [
    {
      "id": 0,
      "name": "check-before-naming",
      "prompt": "I want to add a new function called escape_like to the glossary project at g:/python_projects/skills_creator/glossary. It will handle escaping SQL patterns. Before I start, check if there's anything with a similar name already.",
      "expected_output": "Should find the existing escape_like function in scripts/glossary_common.py and warn about the naming conflict",
      "files": [],
      "assertions": [
        {
          "name": "found-existing-escape_like",
          "type": "output_contains",
          "check": "The output mentions that escape_like already exists"
        },
        {
          "name": "identified-file-location",
          "type": "output_contains",
          "check": "The output identifies glossary_common.py as the file containing escape_like"
        },
        {
          "name": "suggested-alternative",
          "type": "output_contains",
          "check": "The output suggests an alternative name or warns about the conflict"
        },
        {
          "name": "token-efficiency",
          "type": "qualitative",
          "check": "Used glossary_search instead of reading source files"
        }
      ]
    },
    {
      "id": 1,
      "name": "session-start-orientation",
      "prompt": "I'm starting a new session working on the glossary skill at g:/python_projects/skills_creator/glossary. What symbols exist in this project? Show me all functions and classes across the codebase so I can orient myself.",
      "expected_output": "Should provide a structured overview of glossary symbols organized by module without reading every source file",
      "files": [],
      "assertions": [
        {
          "name": "covered-server",
          "type": "output_contains",
          "check": "The output lists glossary_server.py symbols (glossary_search, glossary_init, etc.)"
        },
        {
          "name": "covered-common",
          "type": "output_contains",
          "check": "The output mentions glossary_common.py symbols (find_project_root, escape_like, etc.)"
        },
        {
          "name": "covered-scanner",
          "type": "output_contains",
          "check": "The output mentions glossary_scanner.py"
        },
        {
          "name": "structured-output",
          "type": "qualitative",
          "check": "Output is organized by module/file, not a random dump"
        }
      ]
    },
    {
      "id": 2,
      "name": "find-duplicates",
      "prompt": "Check the glossary project at g:/python_projects/skills_creator/glossary for duplicate symbol names — any functions or classes that share the same name across different files.",
      "expected_output": "Should use glossary_duplicates tool and report any symbols with the same name in multiple files",
      "files": [],
      "assertions": [
        {
          "name": "used-duplicates-tool",
          "type": "qualitative",
          "check": "Used glossary_duplicates MCP tool (not manual file reading)"
        },
        {
          "name": "reported-results",
          "type": "output_contains",
          "check": "The output either lists specific duplicates or confirms no duplicates found"
        },
        {
          "name": "actionable-output",
          "type": "qualitative",
          "check": "If duplicates found, explains which files and suggests whether they're intentional"
        }
      ]
    },
    {
      "id": 3,
      "name": "post-compaction-recovery",
      "prompt": "My context was just compacted and I lost track of the glossary project at g:/python_projects/skills_creator/glossary. I was working on the MCP server — what tools does it expose and what helper functions does it use?",
      "expected_output": "Should query glossary to restore knowledge of server tools and helpers without reading full source",
      "files": [],
      "assertions": [
        {
          "name": "found-mcp-tools",
          "type": "output_contains",
          "check": "Lists MCP tool functions: glossary_search, glossary_file, glossary_type, etc."
        },
        {
          "name": "found-helpers",
          "type": "output_contains",
          "check": "Mentions helper functions: _get_db, _get_lock, _handle_error"
        },
        {
          "name": "token-efficiency",
          "type": "qualitative",
          "check": "Used glossary tools to restore knowledge, not full file reads"
        }
      ]
    },
    {
      "id": 4,
      "name": "partial-path-file-lookup",
      "prompt": "Show me all the symbols in the common module of the glossary project at g:/python_projects/skills_creator/glossary. I think the file is called something like common.py.",
      "expected_output": "Should use glossary_file with partial path 'common.py' and show all symbols in glossary_common.py",
      "files": [],
      "assertions": [
        {
          "name": "matched-partial-path",
          "type": "output_contains",
          "check": "Found and displayed symbols from glossary_common.py despite partial path input"
        },
        {
          "name": "showed-key-symbols",
          "type": "output_contains",
          "check": "Lists find_project_root, escape_like, format_symbol, group_by_file"
        },
        {
          "name": "used-glossary-file",
          "type": "qualitative",
          "check": "Used glossary_file tool, not source file reading"
        }
      ]
    },
    {
      "id": 5,
      "name": "negative-should-not-trigger",
      "prompt": "Explain how the glossary_search function works internally — I want to understand the SQL query it builds and how wildcard patterns are converted to LIKE syntax. The project is at g:/python_projects/skills_creator/glossary.",
      "expected_output": "Should read the actual source code of glossary_search, not just query the glossary. The user wants implementation details, not a symbol listing.",
      "files": [],
      "assertions": [
        {
          "name": "read-source-code",
          "type": "qualitative",
          "check": "Read or referenced actual source code of glossary_search function body, not just symbol listing"
        },
        {
          "name": "explained-sql-logic",
          "type": "output_contains",
          "check": "Explains the LIKE pattern conversion, escape_like usage, or SQL query construction"
        },
        {
          "name": "not-just-glossary-dump",
          "type": "qualitative",
          "check": "Did NOT rely solely on glossary tools — actually read implementation details"
        }
      ]
    }
  ]
}
```

- [ ] **Step 3: Commit**

```bash
git add evals/evals.json
git commit -m "test: rewrite evals as self-contained + add missing scenarios

Evals now test against glossary project itself (no external deps).
Added: duplicates, post-compaction, partial path, negative trigger."
```

---

## Execution Order

Tasks are independent and can run in parallel:
- **Group A (server):** Tasks 1, 2, 3 (sequential — all modify glossary_server.py)
- **Group B (common):** Task 4 (independent)
- **Group C (CLI):** Task 5 (independent)
- **Group D (docs):** Tasks 6, 7 (independent of each other and code tasks)
- **Group E (evals):** Task 8 (independent)

Recommended: Run Groups A+B+C as one subagent batch (code changes), Groups D+E as another (docs/evals).
