# Glossary

A persistent symbol registry for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Indexes all declared symbols (functions, classes, variables) into SQLite. Queries via MCP tools cost ~50 tokens instead of ~2000 for reading source files.

## Why

Claude's context degrades during compaction and vanishes between sessions. Without a persistent record, the LLM will:
- Create functions with names that already exist elsewhere
- Refer to symbols by wrong or outdated names
- Waste thousands of tokens re-scanning files to figure out what exists

Glossary keeps a SQLite database that survives sessions. One search returns ~50 tokens instead of a full file read at ~2000 tokens.

## Token savings

Measured on a 167-file TypeScript project (703 symbols, 812KB source):

| Operation | Glossary | Reading source | Savings |
|-----------|----------|---------------|---------|
| What's in a file? | 255 chars | 10,298 chars | **40x** |
| Project overview | 290 chars | 5,000+ chars | **17x** |
| All project symbols | 20,777 chars | 812,349 chars | **39x** |
| Search `*Node*` (87 hits) | 6,277 chars | 50,000+ chars | **8x** |

## Installation

### As a Claude Code plugin (recommended)

```bash
# Install globally
claude plugin add everysingletear/glossary

# Or add to a specific project's .claude/settings.json
```

### Manual setup

1. Clone the repository:
```bash
git clone https://github.com/everysingletear/glossary.git
```

2. Install dependencies:
```bash
pip install mcp pydantic
```

3. Add MCP server to your project's `.mcp.json`:
```json
{
  "mcpServers": {
    "glossary_mcp": {
      "command": "python",
      "args": ["/path/to/glossary/scripts/glossary_server.py"]
    }
  }
}
```

4. (Optional) Add the auto-update hook to `~/.claude/settings.json`:
```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "python /path/to/glossary/scripts/glossary_scanner.py --stdin"
          }
        ]
      }
    ]
  }
}
```

## How it works

```
Hook (PostToolUse: Edit/Write)          LLM
  |                                      |
  |  glossary_scanner.py --stdin         |  MCP tools: glossary_search, etc.
  |  deterministic parsing, 0 tokens     |  native tool calls, no Bash needed
  |                                      |
  +----------> .claude/glossary.db <-----+
```

Two components:
- **Scanner** (hook) — runs after every code edit. Uses Python `ast`, regex for JS/TS, optional tree-sitter for other languages. Deterministic — zero LLM tokens, ~100ms per file.
- **MCP Server** — exposes query tools as native MCP calls. No Bash, no script paths, no permission prompts.

## MCP Tools

### `glossary_search(pattern, verbose?, limit?, offset?)`

Search symbols by name with `*` wildcards.

```
> glossary_search("process_*")

Found 3 symbols matching 'process_*':

## backend/app/pipeline.py (2 symbols)
- `process_batch(items: list[dict], batch_size: int = 100)` — fn
- `process_single(item: dict) -> Result` — fn

## backend/app/worker.py (1 symbols)
- `process_queue(queue_name: str, max_retries: int = 3)` — fn
```

**When to use:** Before creating any new function, class, or variable — check for naming conflicts first.

### `glossary_file(file_path, verbose?)`

Show all symbols in a file. Accepts partial paths (`auth.py` matches `backend/app/auth.py`).

**When to use:** When you need to know what's in a file without reading source. 40x cheaper.

### `glossary_type(symbol_type, verbose?, limit?, offset?)`

Show all symbols of a type: `fn`, `class`, `method`, `var`, `const`, `interface`, `type`, `enum`.

### `glossary_duplicates(include_tests?, include_migrations?, verbose?, limit?, offset?)`

Find symbols with the same name across different files. Tests and migrations excluded by default.

### `glossary_stats()`

Cheapest query — file count, symbol count by type and language. Use at session start.

### `glossary_recent(limit?, verbose?)`

Show symbols from the most recently scanned files.

### `glossary_full(verbose?, limit?, offset?)`

Full glossary dump. Auto-switches to compact format for large projects (200+ symbols). Pass `verbose=True` for full output.

### `glossary_describe(target, description)`

Set a 1-line description on a symbol. Format: `file_path:symbol_name` or just `symbol_name`.

### `glossary_init()`

Initialize or rebuild the database. Run once per project, or after branch switches.

## CLI usage

All operations are also available via command-line:

```bash
python scripts/glossary_query.py --search "process_*"
python scripts/glossary_query.py --file src/auth.py
python scripts/glossary_query.py --type class
python scripts/glossary_query.py --duplicates
python scripts/glossary_query.py --stats
python scripts/glossary_query.py --recent
python scripts/glossary_query.py --full
python scripts/glossary_query.py --describe "file:symbol" "Description"
```

Scanner commands:

```bash
python scripts/glossary_scanner.py --init          # First-time setup
python scripts/glossary_scanner.py --full           # Rescan everything
python scripts/glossary_scanner.py --file src/foo.py  # Scan one file
```

## Language support

| Language | Parser | Coverage |
|----------|--------|----------|
| Python | `ast` module | Full — functions, classes, methods, variables, constants, type annotations |
| JavaScript / TypeScript | Regex patterns | Good — top-level declarations, class methods, interfaces, types, enums |
| Go | tree-sitter (optional) | Full with `pip install tree-sitter tree-sitter-go` |
| Rust | tree-sitter (optional) | Full with `pip install tree-sitter tree-sitter-rust` |
| Java | tree-sitter (optional) | Full with `pip install tree-sitter tree-sitter-java` |
| C/C++ | tree-sitter (optional) | Full with `pip install tree-sitter tree-sitter-c tree-sitter-cpp` |

## When to use vs alternatives

| Need | Tool | Why |
|------|------|-----|
| "What functions exist in file X?" | **Glossary** `glossary_file` | 40x cheaper than reading source |
| "Is there a function named Y?" | **Glossary** `glossary_search` | Instant SQLite lookup |
| "Show all classes in the project" | **Glossary** `glossary_type class` | Cross-project inventory |
| "How does function X work?" | Serena / Read source | Glossary shows WHAT exists, not HOW |
| "Find regex pattern in code" | Grep / Serena | Glossary only indexes names, not bodies |
| "What calls function X?" | Serena `find_referencing_symbols` | Glossary doesn't track references |

## Limitations

- **Stale after file deletion.** The hook fires on Edit/Write, not deletion. Run `glossary_init` after deleting files.
- **Stale after git operations.** Branch switches change files outside hooks. Run `glossary_init` after major git operations.
- **No function bodies.** Only names, signatures, and types are indexed. Use source reading for implementation details.
- **Large files (>500KB) skipped.** Generated code and bundles are excluded.
- **No dynamic symbols.** `setattr`, `__all__` re-exports, decorator-generated attributes are not indexed.

## Requirements

- Python >= 3.10
- `mcp` >= 1.0.0 (for MCP server)
- `pydantic` (dependency of mcp)

## License

MIT
