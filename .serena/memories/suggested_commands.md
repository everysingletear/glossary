# Suggested Commands

## Running
```bash
# MCP server (normally started by Claude Code via settings.json)
python scripts/glossary_server.py

# CLI queries
python scripts/glossary_query.py search <pattern>
python scripts/glossary_query.py file <path>
python scripts/glossary_query.py type <fn|class|method|var|const|interface|type|enum>
python scripts/glossary_query.py duplicates
python scripts/glossary_query.py stats
python scripts/glossary_query.py recent
python scripts/glossary_query.py full
python scripts/glossary_query.py describe <file:symbol>

# Scanner
python scripts/glossary_scanner.py --init    # First-time setup
python scripts/glossary_scanner.py --full    # Rescan all files
python scripts/glossary_scanner.py --file <path>  # Single file scan
```

## Testing
```bash
python -m py_compile scripts/glossary_server.py
python -m py_compile scripts/glossary_scanner.py
python -m py_compile scripts/glossary_query.py
python -m py_compile scripts/glossary_common.py
```

## System (Windows with Git Bash)
```bash
git status
git diff
git log --oneline -10
```
