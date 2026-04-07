# Code Style and Conventions

- Python, no type hints on existing code (don't add unless changing function)
- No docstrings convention — comments explain WHY, not WHAT
- Short focused functions, explicit over clever
- Standard library preferred over external deps
- Constants as module-level UPPER_SNAKE_CASE
- Private helpers prefixed with underscore
- SQL queries use parameterized ? placeholders (never f-strings)
- SQL LIKE patterns escaped via escape_like() helper
- Async with unified db_lock in server, sync in CLI/scanner
- Conventional commits: feat:, fix:, refactor:, docs:
