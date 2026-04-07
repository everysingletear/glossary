# Glossary Setup

## MCP Server Configuration

Add to your project's `.claude/settings.json` (or global `~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "glossary_mcp": {
      "command": "python",
      "args": ["<SKILL_PATH>/scripts/glossary_server.py"]
    }
  }
}
```

Replace `<SKILL_PATH>` with the absolute path to the glossary skill directory.

**Windows path note:** In JSON, backslashes must be escaped. Use forward slashes instead — they work on Windows too:
```json
"args": ["C:/Users/you/.claude/skills/glossary/scripts/glossary_server.py"]
```

The server auto-detects the project root from CWD (Claude Code runs MCP servers from the project directory).

### What the MCP server provides

9 tools available as native MCP calls — no Bash needed:
- `glossary_search` — find symbols by name
- `glossary_file` — symbols in a specific file
- `glossary_type` — all symbols of a type
- `glossary_duplicates` — naming collisions
- `glossary_stats` — quick overview
- `glossary_recent` — recently changed symbols
- `glossary_full` — complete dump
- `glossary_describe` — add descriptions
- `glossary_init` — initialize/rebuild database

## Hook Configuration (automatic updates)

The hook runs the scanner after every Edit/Write, keeping the database current with zero token cost.

Add to the same `.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "command": "python \"<SKILL_PATH>/scripts/glossary_scanner.py\" --stdin"
      }
    ]
  }
}
```

### Extended matcher (including Serena tools)

For comprehensive coverage when using Serena for code edits:
```json
{
  "matcher": "Edit|Write|mcp__plugin_serena_serena__replace_symbol_body|mcp__plugin_serena_serena__rename_symbol|mcp__plugin_serena_serena__insert_after_symbol|mcp__plugin_serena_serena__insert_before_symbol"
}
```

### How the hook works

1. Every time Claude uses Edit or Write, the hook fires
2. The hook receives the tool input as JSON on stdin (includes `file_path`)
3. `glossary_scanner.py` parses just that file and updates the database
4. Takes ~100ms, uses zero LLM tokens

## Dependencies

Install the MCP package before running the server:

```bash
pip install -r "<SKILL_PATH>/scripts/requirements.txt"
# or directly:
pip install "mcp>=1.0.0,<2.0.0"
```

**Use the same Python interpreter that will run the MCP server.** If you manage multiple environments, prefer installing via:
```bash
# With uv (recommended):
uv pip install "mcp>=1.0.0,<2.0.0"

# Or point pip at a specific interpreter:
/path/to/your/python -m pip install "mcp>=1.0.0,<2.0.0"
```

If you see `ModuleNotFoundError: No module named 'mcp'` when the server starts, the MCP package was installed into a different Python than the one running the server. Check with `python -c "import mcp; print(mcp.__file__)"` from the same interpreter.

## First-time initialization

The MCP server auto-initializes when the database doesn't exist. You can also initialize manually:

```bash
python "<SKILL_PATH>/scripts/glossary_scanner.py" --init
```

Or call the `glossary_init` MCP tool.

## Optional: tree-sitter for more languages

By default, the scanner handles Python (via `ast`) and JS/TS (via regex). For other languages:

```bash
pip install tree-sitter
pip install tree-sitter-go tree-sitter-rust tree-sitter-java tree-sitter-c tree-sitter-cpp
```

## CLI fallback (no MCP)

If the MCP server is not configured, you can query the glossary directly via `glossary_query.py`:

```bash
python "<SKILL_PATH>/scripts/glossary_query.py" --search "process_*"
python "<SKILL_PATH>/scripts/glossary_query.py" --file auth.py
python "<SKILL_PATH>/scripts/glossary_query.py" --stats
python "<SKILL_PATH>/scripts/glossary_query.py" --duplicates
```

Run without arguments for full usage help. This is a direct SQLite CLI — useful for debugging the database or environments where MCP is unavailable.

## Database location

- Stored at `<project-root>/.claude/glossary.db`
- Auto-added to `.gitignore` during initialization
- Regenerable — safe to delete and re-init

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
