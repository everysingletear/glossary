---
name: glossary
description: Generate and query a persistent glossary of all declared symbols in the codebase. Make sure to use this skill whenever you need to check what functions, classes, or variables exist — before creating new symbols, after context compaction, at session start, when resuming work, or whenever the user asks about existing code. Use it instead of reading source files when you only need to know WHAT exists (not HOW it works). A single glossary search costs ~50 tokens vs ~2000 for reading a file. Always prefer glossary_search over Grep or file reads for symbol name lookups.
---

# Glossary

A persistent, token-efficient registry of every declared symbol in the codebase.

## Why this exists

Your context degrades during compaction and vanishes between sessions. Without a persistent record, you will:
- Create functions or variables with names that already exist elsewhere
- Refer to symbols by wrong or outdated names from compressed context
- Waste thousands of tokens re-scanning files to figure out what exists

The glossary solves this with a SQLite database that survives sessions. You query only what you need — a single search returns ~50 tokens instead of a full file scan at ~2000 tokens.

## Architecture

```
Hook (PostToolUse: Edit/Write)          You (LLM)
  │                                        │
  │  glossary_scanner.py --stdin           │  MCP tools: glossary_search, etc.
  │  deterministic parsing, 0 tokens       │  native tool calls, no Bash needed
  │                                        │
  └──────────► .claude/glossary.db ◄───────┘
```

Two components:
- **Scanner** (hook) — runs after every code edit. Uses Python `ast`, regex for JS/TS, optional tree-sitter for other languages. Deterministic — zero LLM tokens, ~100ms per file.
- **MCP Server** — exposes query operations as native MCP tools. You call them like any other tool — no Bash, no script paths, no permission issues.

## MCP Tools

All tools are available as native MCP calls via the `glossary_mcp` server. No `--db` flags, no file paths — just call the tool.

### `glossary_search(pattern, verbose?, limit?, offset?)`
Search symbols by name. Supports `*` wildcards.
**When to use:** Before creating any new function, class, or variable — check for naming conflicts first. A 50-token search prevents a costly rename later. Supports `limit`/`offset` pagination — if results hit the limit, pass `offset=limit` for the next page.

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

### `glossary_file(file_path, verbose?)`
Show all symbols in a specific file. Accepts partial paths (`auth.py` matches `backend/app/auth.py`).
**When to use:** When you're about to modify a file and want to know what's there without reading source. ~100 tokens vs ~2000 for a full file read.

### `glossary_type(symbol_type, verbose?, limit?, offset?)`
Show all symbols of a given type: `fn`, `class`, `method`, `var`, `const`, `interface`, `type`, `enum`.
**When to use:** When you want a cross-project inventory of a specific symbol kind — all classes before designing inheritance, all constants before adding a new one. Supports `limit`/`offset` pagination for large codebases.

### `glossary_duplicates(include_tests?, include_migrations?, verbose?, limit?, offset?)`
Find symbols with the same name across different files. Tests and migrations excluded by default. Supports `limit`/`offset` pagination for projects with many duplicates.
**When to use:** The #1 source of confusion during context compaction. If `process_data` exists in two modules, after compaction you might call the wrong one.

### `glossary_stats()`
Quick overview — file count, symbol count by type and language. Cheapest query possible.
**When to use:** At session start or after context compaction to understand project scope before diving into specific files.

### `glossary_recent(limit?, verbose?)`
Show symbols from the most recently scanned files.
**When to use:** After edits to verify the scanner is tracking changes.

### `glossary_full(verbose?, limit?, offset?)`
Full glossary dump. For large projects (200+ symbols), auto-switches to compact format: `name(type)` per file, showing only top-level symbols (methods under classes are hidden to save tokens). Pass `verbose=True` to force full signatures, line numbers, and all methods regardless of project size. Supports `limit`/`offset` pagination for very large projects (5000+ symbols).
**When to use:** Session start for small-to-medium projects. For large projects, prefer `glossary_stats` first, then `glossary_file` for specific areas.

### `glossary_describe(target, description)`
Set a 1-line description on a symbol. Format: `file_path:symbol_name` or just `symbol_name`. Windows absolute paths work correctly (`C:\path\file.py:func_name`). Calling again replaces the previous description.
**When to use:** Only for ambiguous names (`run`, `handle`, `process`). Don't describe obvious names.

### `glossary_init()`
Initialize or rebuild the database. Scans all source files, creates `.claude/glossary.db`, adds to `.gitignore`.
**When to use:** Once per project, or after branch switches / major refactors.

### `glossary_enrich(file_path?, symbol_type?, limit?, context_lines?)`
Returns undescribed symbols with source code context. The scanner extracts docstrings/JSDoc automatically — this tool covers the rest. You read the source snippets, generate 1-line descriptions, and save them with `glossary_describe_batch`.
**When to use:** After `glossary_init` on a new project, or when you notice symbols without descriptions.

### `glossary_describe_batch(descriptions)`
Save multiple descriptions in one call. Takes a JSON array: `[{"target": "file:symbol", "description": "..."}]`.
**When to use:** After `glossary_enrich` — to save LLM-generated descriptions.

## Enrichment workflow

After `glossary_init`, many symbols may lack descriptions (the scanner only extracts docstrings/JSDoc). Fill them in:

1. Call `glossary_enrich()` — returns up to 20 undescribed symbols with source context
2. Read the source snippets and generate a 1-line description for each (max 100 tokens)
3. Call `glossary_describe_batch` with the JSON array of descriptions
4. Repeat until `glossary_enrich` returns "All symbols already have descriptions"

**When working with files:** if you edit or read code and understand a symbol that has no description, add one immediately via `glossary_describe`. Don't wait for a batch — enrich as you go.

## When to query

### Before creating any new symbol
Always search first to avoid collisions. This is the most important use case.

### After context compaction
When your context has been compressed, query the files you're working with to restore knowledge.

### At session start
Orient yourself. For small projects, `glossary_full`. For large ones, `glossary_stats` first, then `glossary_file` for specific areas.

### Instead of reading source files
When you only need to know **what exists** (not **how it works**), the glossary is 10-100x cheaper than reading source.

## When NOT to use the glossary

- **Understanding implementation logic** — the glossary shows WHAT exists, not HOW it works. Use source reading or Serena's `find_symbol` with `include_body=True` for implementation details.
- **Exploring code structure/architecture** — for directory trees, module dependencies, or architectural overview, use Serena's `get_symbols_overview` or directory exploration tools.
- **Finding specific code patterns** — for regex searches in source code, use `search_for_pattern` (Serena) or `Grep`. The glossary only indexes symbol names and signatures, not code bodies.

## Setup

The plugin auto-configures the MCP server. For manual setup, see the README.

## Language support

| Language | Parser | Coverage |
|----------|--------|----------|
| Python | `ast` module | Full — functions, classes, methods, variables, constants, type annotations |
| JS/TS | Regex patterns | Good — top-level declarations, class methods, interfaces, types, enums |
| Go, Rust | tree-sitter (optional) | Full if `pip install tree-sitter tree-sitter-go tree-sitter-rust` |
| Java, C/C++ | tree-sitter (optional) | Full if `pip install tree-sitter tree-sitter-java tree-sitter-c tree-sitter-cpp` |

## Limitations

**Stale data after file deletion.** The hook fires on Edit/Write, not on file deletion. If a file is deleted outside of Claude Code (e.g., via shell), its symbols remain in the database until the next full scan. Run `glossary_init` after deleting files, or rely on `glossary_stats` staleness timestamp to detect drift.

**Stale data after git operations.** Branch switches, merges, and rebases change files outside of Edit/Write hooks. Run `glossary_init` after any significant git operation that changes file structure.

**Debounce window.** The scanner skips rescanning a file if it was scanned within the last 3 seconds. This means very rapid successive edits may leave the database briefly behind the latest file state.

**Symbols inside functions.** Only module/class level declarations are indexed. Variables declared inside function bodies are not captured (too noisy, rarely needed for naming collision checks).

**Dynamic symbols.** Dynamically generated symbols (e.g., `setattr`, `__all__` re-exports, decorator-generated attributes) are not indexed — static analysis cannot resolve runtime symbol creation.
