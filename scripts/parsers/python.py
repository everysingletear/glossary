"""Python parser — uses ast module for accurate symbol extraction."""
import ast

EXTENSIONS = [".py"]
LANGUAGE = "python"


def _extract_docstring(node) -> str | None:
    """Extract the first line of a docstring from an AST node.

    Returns a trimmed single-line summary (max 200 chars), or None.
    Works for FunctionDef, AsyncFunctionDef, ClassDef, and Module nodes.
    """
    doc = ast.get_docstring(node)
    if not doc:
        return None
    # Take first non-empty line
    first_line = doc.strip().split("\n")[0].strip()
    if not first_line:
        return None
    if len(first_line) > 200:
        first_line = first_line[:197] + "..."
    return first_line


def parse_python(source: str, file_path: str) -> list[dict]:
    """Extract symbols from Python source using the ast module."""
    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError:
        return []

    symbols = []

    def _get_annotation(node) -> str:
        if node is None:
            return ""
        try:
            return ast.unparse(node)
        except Exception:
            return ""

    def _func_signature(node: ast.FunctionDef) -> str:
        args = []
        defaults_offset = len(node.args.args) - len(node.args.defaults)

        for i, arg in enumerate(node.args.args):
            name = arg.arg
            ann = _get_annotation(arg.annotation)
            part = f"{name}: {ann}" if ann else name
            def_idx = i - defaults_offset
            if 0 <= def_idx < len(node.args.defaults):
                try:
                    default = ast.unparse(node.args.defaults[def_idx])
                    if len(default) > 20:
                        default = "..."
                    part += f"={default}"
                except Exception:
                    pass
            args.append(part)

        if node.args.vararg:
            args.append(f"*{node.args.vararg.arg}")
        if node.args.kwonlyargs:
            if not node.args.vararg:
                args.append("*")
            for kw in node.args.kwonlyargs:
                ann = _get_annotation(kw.annotation)
                args.append(f"{kw.arg}: {ann}" if ann else kw.arg)
        if node.args.kwarg:
            args.append(f"**{node.args.kwarg.arg}")

        ret = _get_annotation(node.returns)
        sig = f"{node.name}({', '.join(args)})"
        if ret:
            sig += f" -> {ret}"
        return sig

    def _visit_class(node: ast.ClassDef, parent: str = None):
        bases = []
        for base in node.bases:
            try:
                bases.append(ast.unparse(base))
            except Exception:
                pass
        sig = f"{node.name}({', '.join(bases)})" if bases else node.name

        symbols.append({
            "name": node.name,
            "type": "class",
            "signature": sig,
            "parent": parent,
            "line": node.lineno,
            "description": _extract_docstring(node),
        })

        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Detect decorator-based symbol types
                dec_names = []
                for d in item.decorator_list:
                    if isinstance(d, ast.Name):
                        dec_names.append(d.id)
                    elif isinstance(d, ast.Attribute):
                        dec_names.append(d.attr)

                if "property" in dec_names:
                    sym_type = "property"
                elif "staticmethod" in dec_names:
                    sym_type = "staticmethod"
                elif "classmethod" in dec_names:
                    sym_type = "classmethod"
                else:
                    sym_type = "method"

                symbols.append({
                    "name": item.name,
                    "type": sym_type,
                    "signature": _func_signature(item),
                    "parent": node.name,
                    "line": item.lineno,
                    "description": _extract_docstring(item),
                })
            elif isinstance(item, ast.ClassDef):
                _visit_class(item, parent=node.name)
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        _visit_assignment(target.id, item, parent=node.name)
            elif isinstance(item, ast.AnnAssign):
                if isinstance(item.target, ast.Name):
                    name = item.target.id
                    ann = _get_annotation(item.annotation)
                    sym_type = "const" if name.isupper() else "var"
                    sig = f"{name}: {ann}"
                    if item.value:
                        try:
                            val = ast.unparse(item.value)
                            if len(val) <= 30:
                                sig += f" = {val}"
                        except Exception:
                            pass
                    symbols.append({
                        "name": name,
                        "type": sym_type,
                        "signature": sig,
                        "parent": node.name,
                        "line": item.lineno,
                    })

    def _visit_assignment(name: str, node, parent: str = None):
        sym_type = "const" if name.isupper() else "var"
        sig = name
        try:
            value_str = ast.unparse(node.value)
            if len(value_str) <= 40:
                sig = f"{name} = {value_str}"
            else:
                sig = f"{name} = ..."
        except Exception:
            pass

        symbols.append({
            "name": name,
            "type": sym_type,
            "signature": sig,
            "parent": parent,
            "line": node.lineno,
        })

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append({
                "name": node.name,
                "type": "fn",
                "signature": _func_signature(node),
                "parent": None,
                "line": node.lineno,
                "description": _extract_docstring(node),
            })
        elif isinstance(node, ast.ClassDef):
            _visit_class(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    _visit_assignment(target.id, node)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                name = node.target.id
                ann = _get_annotation(node.annotation)
                sym_type = "const" if name.isupper() else "var"
                sig = f"{name}: {ann}"
                if node.value:
                    try:
                        val = ast.unparse(node.value)
                        if len(val) <= 30:
                            sig += f" = {val}"
                    except Exception:
                        pass
                symbols.append({
                    "name": name,
                    "type": sym_type,
                    "signature": sig,
                    "parent": None,
                    "line": node.lineno,
                })

    return symbols


def parse(source: str, file_path: str) -> list[dict]:
    """Parse Python source and return symbol list."""
    return parse_python(source, file_path)
