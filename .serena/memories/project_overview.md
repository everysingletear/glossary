# Glossary - Symbol Registry Skill for Claude Code

## Purpose
MCP server + CLI + hook-based scanner that maintains a persistent SQLite symbol registry for codebases. Prevents naming collisions, restores knowledge after context compaction, provides token-efficient symbol queries (~50 tokens vs ~2000 for file read).

## Tech Stack
- Python 3.14, stdlib only for CLI/scanner
- FastMCP (mcp>=1.0.0,<2.0.0) for MCP server
- SQLite with WAL mode for concurrent reads
- Python ast module for Python parsing, regex for JS/TS
- Optional tree-sitter for Go, Rust, Java, C, C++

## Structure
```
glossary/
├── SKILL.md              # Skill definition for Claude Code
├── scripts/
│   ├── glossary_server.py   # MCP server (9 tools via FastMCP)
│   ├── glossary_query.py    # CLI fallback (8 commands)
│   ├── glossary_scanner.py  # Hook-based + full scanner
│   ├── glossary_common.py   # Shared constants, formatters, helpers
│   └── requirements.txt     # mcp>=1.0.0,<2.0.0
├── references/
│   └── setup.md             # Installation/configuration guide
└── evals/
    └── evals.json           # Test cases
```

## Architecture
- **Scanner** (glossary_scanner.py): Deterministic parser, runs as PostToolUse hook on Edit/Write, ~100ms per file
- **Server** (glossary_server.py): FastMCP async server, 9 tools, unified db_lock for all operations
- **CLI** (glossary_query.py): Direct SQLite access, mirrors MCP tools
- **Common** (glossary_common.py): Shared constants, formatters, SQL helpers, path utilities

## Database
Located at `.claude/glossary.db`. Tables: `symbols` (name, type, signature, parent, line, description, is_test, is_migration), `files` (mtime, language, symbol_count, last_scanned). UNIQUE constraint on (file_path, symbol_name, parent).
