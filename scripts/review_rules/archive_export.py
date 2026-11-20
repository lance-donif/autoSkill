"""Detect public ZIP exports opened in truncating mode before delivery."""
import ast
import symtable
from pathlib import Path

SOURCE = "skills/autoskill/app/autoskill/share.py"


def _imports(body):
    names = {}
    for node in body:
        if isinstance(node, ast.Import):
            names.update((item.asname or item.name, item.name) for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            names.update((item.asname or item.name, f"{module}.{item.name}") for item in node.names)
    return names


def _qualified(node, names):
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{names.get(node.value.id)}.{node.attr}"
    return None


def _output_path(node, aliases, names):
    if isinstance(node, ast.Name):
        return aliases.get(node.id)
    if isinstance(node, ast.IfExp) and isinstance(node.test, ast.Name) and node.test.id == "out":
        return _output_path(node.body, aliases, names)  # Explicit output path supplied.
    if isinstance(node, ast.Call):
        if _qualified(node.func, names) in {"builtins.str", "pathlib.Path"} and len(node.args) == 1 and not node.keywords:
            return _output_path(node.args[0], aliases, names)
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"expanduser", "resolve"} and not node.args and not node.keywords:
            return _output_path(node.func.value, aliases, names)
    return None


def _zip_target(node, aliases, names):
    if not isinstance(node, ast.Call) or _qualified(node.func, names) != "zipfile.ZipFile":
        return None
    keywords = {item.arg: item.value for item in node.keywords}
    target = node.args[0] if node.args else keywords.get("file")
    mode = node.args[1] if len(node.args) > 1 else keywords.get("mode")
    if isinstance(mode, ast.Constant) and mode.value == "w":
        return _output_path(target, aliases, names)
    return None


def _assignment(value, targets, aliases, names, writes):
    if (path := _zip_target(value, aliases, names)) is not None:
        writes.append((value.lineno, path))
    assigned_path = _output_path(value, aliases, names)
    for target in targets:
        if isinstance(target, ast.Name):
            aliases[target.id] = assigned_path


def _branch_paths(node, aliases, names):
    # A conditional rebind creates a new identity, shared only by writes and
    # receipts that both see that value after the conditional.
    blocks = (node.body, node.orelse)
    if isinstance(node.test, ast.Constant):
        blocks = (node.body if node.test.value else node.orelse,)
    elif isinstance(node.test, ast.Name) and node.test.id == "out" and aliases.get("out") == "out":
        blocks = (node.body,)
    branches = []
    for statements in blocks:
        branch = aliases.copy()
        if _scan(statements, branch, names, []) is None:
            branches.append(branch)
    for name in set().union(*branches):
        options = {branch.get(name) for branch in branches}
        aliases[name] = options.pop() if len(options) == 1 else (node, name)
    return bool(branches)


def _receipt(node, aliases, names):
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "file":
                return _output_path(value, aliases, names)
    return None


def _scan(body, aliases, names, writes):
    for node in body:
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.Expr)):
            targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
            _assignment(node.value, targets, aliases, names, writes)
        elif isinstance(node, ast.With):
            for item in node.items:
                _assignment(item.context_expr, [item.optional_vars], aliases, names, writes)
            if (lines := _scan(node.body, aliases, names, writes)) is not None:
                return lines
        elif isinstance(node, ast.If):
            if not _branch_paths(node, aliases, names):
                return []
        elif isinstance(node, ast.Return):
            receipt = _receipt(node.value, aliases, names)
            return [line for line, path in writes if path == receipt]
        elif isinstance(node, ast.Raise):
            return []
        elif isinstance(node, (ast.Try, ast.For, ast.While, ast.Match)):
            for bound in ast.walk(node):
                if isinstance(bound, ast.Name) and isinstance(bound.ctx, ast.Store):
                    aliases.pop(bound.id, None)
    return None


def run(root: Path) -> list[dict]:
    source = (root / SOURCE).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=SOURCE)
    scope = symtable.symtable(source, SOURCE, "exec")
    module_names = {"str": "builtins.str", **_imports(tree.body)}
    for symbol in scope.get_symbols():
        if symbol.is_assigned():
            module_names.pop(symbol.get_name(), None)
    functions = {(child.get_name(), child.get_lineno()): child for child in scope.get_children()}
    findings = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name not in {"pack", "pack_full"}:
            continue
        local = functions[(node.name, node.lineno)]
        if "out" not in local.get_parameters():
            continue
        names = {name: value for name, value in module_names.items() if name not in local.get_locals()}
        names.update((name, value) for name, value in _imports(node.body).items() if not local.lookup(name).is_assigned())
        lines = _scan(node.body, {"out": "out"}, names, []) or []
        findings.extend({"rule": "archive_export", "severity": "error", "path": SOURCE, "line": line,
                         "message": f"{node.name} 在交付前用 w 模式截断回执指向的正式 ZIP，同名重试可能损坏已有交付。"}
                        for line in lines)
    return findings
