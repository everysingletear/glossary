"""Parser registry — maps file extensions to parser modules."""

from __future__ import annotations

from parsers import python as _python
from parsers import gdscript as _gdscript
from parsers import javascript as _javascript
from parsers import go as _go
from parsers import rust as _rust
from parsers import java as _java
from parsers import c as _c
from parsers import csharp as _csharp
from parsers import php as _php
from parsers import swift as _swift
from parsers import kotlin as _kotlin
from parsers import ruby as _ruby

# Explicit parser registration. Add new parsers here as they are created.
_PARSERS: list = [_python, _gdscript, _javascript, _go, _rust, _java, _c,
                  _csharp, _php, _swift, _kotlin, _ruby]

# Build extension -> module mapping
_EXT_MAP: dict[str, object] = {}
for _mod in _PARSERS:
    for _ext in _mod.EXTENSIONS:
        _EXT_MAP[_ext] = _mod

SUPPORTED_EXTENSIONS: set[str] = set(_EXT_MAP.keys())


def parse(source: str, file_path: str, ext: str) -> tuple[list[dict], str] | None:
    """Route to the correct parser by extension. Returns (symbols, language) or None."""
    module = _EXT_MAP.get(ext)
    if module is None:
        return None
    symbols = module.parse(source, file_path)
    # Support per-extension language override (e.g. JS module handles both .js and .ts)
    lang_map = getattr(module, "LANGUAGE_MAP", None)
    language = lang_map.get(ext, module.LANGUAGE) if lang_map else module.LANGUAGE
    return symbols, language
