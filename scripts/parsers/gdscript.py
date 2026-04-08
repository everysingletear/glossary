"""GDScript parser — regex + indentation based. No tree-sitter grammar available."""
import re

EXTENSIONS = [".gd"]
LANGUAGE = "gdscript"


def _extract_gddoc(source: str, pos: int) -> str:
    """Extract consecutive ## doc-comment lines immediately before pos."""
    lines = source[:pos].splitlines()
    doc_lines = []
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("##"):
            doc_lines.append(stripped[2:].strip())
        else:
            break
    return " ".join(reversed(doc_lines))


# Regex patterns for GDScript 4 constructs
_GD_CLASS_NAME  = re.compile(r"^class_name\s+(\w+)", re.MULTILINE)
_GD_CLASS       = re.compile(r"^([ \t]*)class\s+(\w+)\s*(?:extends\s+\w+)?\s*:", re.MULTILINE)
_GD_FUNC        = re.compile(
    r"^([ \t]*)(?:static\s+)?func\s+(\w+)\s*\(([^)]*)\)\s*(?:->\s*([\w\[\]|, ]+))?\s*:",
    re.MULTILINE,
)
_GD_VAR         = re.compile(
    r"^([ \t]*)(?:@\w+(?:\([^)]*\))?\s+)*var\s+(\w+)(?:\s*:\s*([\w\[\]|, ]+))?(?:\s*=\s*([^\n#]+))?",
    re.MULTILINE,
)
_GD_CONST       = re.compile(
    r"^([ \t]*)const\s+(\w+)(?:\s*:\s*([\w\[\]|, ]+))?\s*=\s*([^\n#]+)",
    re.MULTILINE,
)
_GD_SIGNAL      = re.compile(
    r"^([ \t]*)signal\s+(\w+)(\s*\([^)]*\))?",
    re.MULTILINE,
)
_GD_ENUM        = re.compile(r"^([ \t]*)enum\s+(\w+)\s*\{", re.MULTILINE)


def _gd_indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _gd_extract_block(source_lines: list[str], header_line_idx: int) -> list[tuple[int, str]]:
    """Return (original_line_index, line) pairs for the indented block after header_line_idx.

    Only one nesting level is supported. The block ends when indentation
    drops back to <= the header's indentation level.
    """
    if header_line_idx >= len(source_lines) - 1:
        return []
    header_indent = _gd_indent(source_lines[header_line_idx])
    result = []
    for i in range(header_line_idx + 1, len(source_lines)):
        line = source_lines[i]
        if not line.strip():
            continue  # blank lines don't end the block
        if _gd_indent(line) <= header_indent:
            break
        result.append((i, line))
    return result


def parse_gdscript(source: str, file_path: str) -> list[dict]:
    """Extract symbols from GDScript 4 source using regex patterns."""
    symbols: list[dict] = []
    lines = source.splitlines()

    def line_of(pos: int) -> int:
        return source[:pos].count("\n") + 1

    # --- class_name (file-level, does NOT trigger block extraction) ---
    for m in _GD_CLASS_NAME.finditer(source):
        symbols.append({
            "name": m.group(1),
            "type": "class",
            "signature": m.group(1),
            "parent": None,
            "line": line_of(m.start()),
            "description": _extract_gddoc(source, m.start()),
        })

    # --- named enums ---
    for m in _GD_ENUM.finditer(source):
        indent = len(m.group(1))
        if indent == 0:
            symbols.append({
                "name": m.group(2),
                "type": "enum",
                "signature": m.group(2),
                "parent": None,
                "line": line_of(m.start()),
                "description": _extract_gddoc(source, m.start()),
            })

    # --- inner classes (one level of nesting) ---
    for m in _GD_CLASS.finditer(source):
        class_name = m.group(2)
        class_line_idx = line_of(m.start()) - 1  # 0-based index into lines

        symbols.append({
            "name": class_name,
            "type": "class",
            "signature": class_name,
            "parent": None,
            "line": class_line_idx + 1,
            "description": _extract_gddoc(source, m.start()),
        })

        # Extract members of this inner class
        block = _gd_extract_block(lines, class_line_idx)
        if not block:
            continue
        block_source = "\n".join(line for _, line in block)
        first_block_line = block[0][0]  # 0-based

        # Only include symbols at the immediate class-member indent level.
        # Symbols with deeper indent are local variables inside methods — skip them.
        member_indent = _gd_indent(block[0][1])

        for fm in _GD_FUNC.finditer(block_source):
            if len(fm.group(1)) != member_indent:
                continue  # nested inside a method body
            blk_line = block_source[:fm.start()].count("\n")
            params = fm.group(3).strip()
            ret = (fm.group(4) or "").strip()
            sig = f"{fm.group(2)}({params})"
            if ret:
                sig += f" -> {ret}"
            sig = " ".join(sig.split())
            if len(sig) > 80:
                sig = sig[:77] + "..."
            symbols.append({
                "name": fm.group(2),
                "type": "method",
                "signature": sig,
                "parent": class_name,
                "line": first_block_line + blk_line + 1,
                "description": _extract_gddoc(block_source, fm.start()),
            })

        for vm in _GD_VAR.finditer(block_source):
            if len(vm.group(1)) != member_indent:
                continue  # local variable inside a method body
            blk_line = block_source[:vm.start()].count("\n")
            name = vm.group(2)
            type_hint = (vm.group(3) or "").strip()
            val = (vm.group(4) or "").strip()
            sig = name
            if type_hint:
                sig += f": {type_hint}"
            if val and len(val) <= 30:
                sig += f" = {val}"
            symbols.append({
                "name": name,
                "type": "var",
                "signature": sig,
                "parent": class_name,
                "line": first_block_line + blk_line + 1,
                "description": _extract_gddoc(block_source, vm.start()),
            })

        for cm in _GD_CONST.finditer(block_source):
            if len(cm.group(1)) != member_indent:
                continue  # nested inside a method body
            blk_line = block_source[:cm.start()].count("\n")
            val = cm.group(4).strip()
            sig = f"{cm.group(2)} = {val[:30]}"
            symbols.append({
                "name": cm.group(2),
                "type": "const",
                "signature": sig,
                "parent": class_name,
                "line": first_block_line + blk_line + 1,
                "description": _extract_gddoc(block_source, cm.start()),
            })

        for sm in _GD_SIGNAL.finditer(block_source):
            if len(sm.group(1)) != member_indent:
                continue  # nested inside a method body
            blk_line = block_source[:sm.start()].count("\n")
            args = (sm.group(3) or "").strip()
            sig = f"{sm.group(2)}{args}" if args else sm.group(2)
            symbols.append({
                "name": sm.group(2),
                "type": "signal",
                "signature": sig,
                "parent": class_name,
                "line": first_block_line + blk_line + 1,
                "description": _extract_gddoc(block_source, sm.start()),
            })

    # --- top-level symbols (indent == 0) ---
    for m in _GD_FUNC.finditer(source):
        if len(m.group(1)) != 0:
            continue  # skip inner-class members
        params = m.group(3).strip()
        ret = (m.group(4) or "").strip()
        sig = f"{m.group(2)}({params})"
        if ret:
            sig += f" -> {ret}"
        sig = " ".join(sig.split())
        if len(sig) > 80:
            sig = sig[:77] + "..."
        symbols.append({
            "name": m.group(2),
            "type": "fn",
            "signature": sig,
            "parent": None,
            "line": line_of(m.start()),
            "description": _extract_gddoc(source, m.start()),
        })

    for m in _GD_VAR.finditer(source):
        if len(m.group(1)) != 0:
            continue
        name = m.group(2)
        type_hint = (m.group(3) or "").strip()
        val = (m.group(4) or "").strip()
        sig = name
        if type_hint:
            sig += f": {type_hint}"
        if val and len(val) <= 30:
            sig += f" = {val}"
        symbols.append({
            "name": name,
            "type": "var",
            "signature": sig,
            "parent": None,
            "line": line_of(m.start()),
            "description": _extract_gddoc(source, m.start()),
        })

    for m in _GD_CONST.finditer(source):
        if len(m.group(1)) != 0:
            continue
        val = m.group(4).strip()
        sig = f"{m.group(2)} = {val[:30]}"
        symbols.append({
            "name": m.group(2),
            "type": "const",
            "signature": sig,
            "parent": None,
            "line": line_of(m.start()),
        })

    for m in _GD_SIGNAL.finditer(source):
        if len(m.group(1)) != 0:
            continue
        args = (m.group(3) or "").strip()
        sig = f"{m.group(2)}{args}" if args else m.group(2)
        symbols.append({
            "name": m.group(2),
            "type": "signal",
            "signature": sig,
            "parent": None,
            "line": line_of(m.start()),
            "description": _extract_gddoc(source, m.start()),
        })

    symbols.sort(key=lambda s: s.get("line", 0))
    return symbols


def parse(source: str, file_path: str) -> list[dict]:
    """Parse GDScript source and return symbol list."""
    return parse_gdscript(source, file_path)
