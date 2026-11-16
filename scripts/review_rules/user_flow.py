"""Find statically resolved execution/report mutations reachable from public status readers."""
from __future__ import annotations

import ast
from collections import deque
from pathlib import Path

PACKAGE = Path("skills/autoskill/app/autoskill")
READERS = {
    "autoskill.plan.show", "autoskill.plan_execution.describe", "autoskill.plan_execution.previous_deliveries",
    "autoskill.case_plans.get", "autoskill.case_plans.previous_deliveries", "autoskill.task_reports.get",
    "autoskill.task_reports.peek", "autoskill.task_state.report", "autoskill.workflow.status",
}
MUTATIONS = {
    "autoskill.runner.prepare", "autoskill.runner.enqueue", "autoskill.runner.create", "autoskill.runner.execute",
    "autoskill.runner.spawn", "autoskill.native_run.create", "autoskill.native_run.execute",
    "autoskill.task_reports._render", "autoskill.task_reports.for_design", "autoskill.task_reports.for_run",
    "autoskill.task_reports.for_recording", "autoskill.task_reports.for_workflow", "autoskill.task_reports.retry",
    "autoskill.task_reports.prepare_workflow", "autoskill.task_reports.finish_workflow_report",
    "autoskill.workflow.next_task", "autoskill.workflow.submit", "autoskill.workflow.reconcile_pending",
    "autoskill.candidate_publication.publish", "autoskill.candidate_publication.complete_run",
}


def _imports(nodes, module: str, package_file: bool = False) -> dict[str, str]:
    aliases = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".")[0]] = item.name if item.asname else item.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            parent = module.split(".") if package_file else module.split(".")[:-1]
            prefix = parent[:len(parent) - node.level + 1] if node.level else []
            prefix += node.module.split(".") if node.module else []
            for item in node.names:
                if item.name != "*":
                    aliases[item.asname or item.name] = ".".join([*prefix, item.name])
    return aliases


def _name(node: ast.AST, aliases: dict[str, str], module: str, local: set[str]) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.insert(0, node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    if node.id in aliases:
        return ".".join([aliases[node.id], *parts])
    if not parts and node.id in local:
        return module + "." + node.id
    return None


def _body_nodes(function):
    pending = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield node
        pending.extend(ast.iter_child_nodes(node))


def _graph(root: Path) -> dict[str, tuple[Path, ast.AST, list[tuple[str, int]]]]:
    graph = {}
    for file in sorted((root / PACKAGE).rglob("*.py")):
        if "__pycache__" in file.parts:
            continue
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        parts = list(file.relative_to(root / PACKAGE.parent).with_suffix("").parts)
        package_file = parts[-1] == "__init__"
        module = ".".join(parts[:-1] if package_file else parts)
        functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        local = {node.name for node in functions}
        imported = _imports(tree.body, module, package_file)
        for function in functions:
            nodes = list(_body_nodes(function))
            aliases = {**imported, **_imports(nodes, module, package_file)}
            calls = [(target, node.lineno) for node in nodes
                     if isinstance(node, ast.Call) and (target := _name(node.func, aliases, module, local))]
            graph[module + "." + function.name] = (file, function, calls)
    return graph


def run(root: Path) -> list[dict]:
    """Read source only; dynamic dispatch, decorators and closures require separate review."""
    root = root.resolve()
    graph = _graph(root)
    findings = []
    reported = set()
    for entry in sorted(READERS & graph.keys()):
        queue = deque([(entry, [entry])])
        visited = set()
        while queue:
            name, chain = queue.popleft()
            if name in visited or name not in graph:
                continue
            visited.add(name)
            file, _function, calls = graph[name]
            for target, line in calls:
                if target in MUTATIONS:
                    location = (file, line, target)
                    if location not in reported:
                        reported.add(location)
                        findings.append({"rule": "user_flow.read_only_status", "severity": "error",
                                         "path": file.relative_to(root).as_posix(), "line": line,
                                         "message": "只读状态入口可到达执行或报告写操作：" + " → ".join([*chain, target])})
                elif target in graph and target not in visited:
                    queue.append((target, [*chain, target]))
    return findings
