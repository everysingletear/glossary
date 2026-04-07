# Task Completion Checklist

1. Run py_compile on all modified files
2. Verify no SQL injection patterns (all queries use ? params or _DUP_WHERE whitelist)
3. Check escape_like() used for all LIKE patterns
4. Ensure server and CLI produce consistent output for same queries
5. Test with `python scripts/glossary_query.py stats` to verify DB access works
