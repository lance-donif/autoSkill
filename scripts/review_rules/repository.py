"""Check local module closure, package versions and CI test/dependency references."""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path


def finding(path: str, line: int, message: str) -> dict:
    return {"rule": "repository", "severity": "error", "path": path, "line": line, "message": message}


def _module_exists(app: Path, module: str) -> bool:
    path = app.joinpath(*module.split("."))
    return path.with_suffix(".py").is_file() or path.is_dir()


def _python_imports(root: Path) -> list[dict]:
    app = root / "skills/autoskill/app"
    findings = []
    for file in sorted((app / "autoskill").rglob("*.py")):
        relative = file.relative_to(root).as_posix()
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=relative)
        package = list(file.relative_to(app).parts[:-1])
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names if alias.name.startswith("autoskill.")]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = package[:len(package) - node.level + 1]
                    module = ".".join([*base, *node.module.split(".")]) if node.module else ".".join(base)
                else:
                    module = node.module or ""
                if not module.startswith("autoskill"):
                    continue
                modules = [module]
                target = app.joinpath(*module.split("."))
                if target.is_dir():
                    modules.extend(module + "." + alias.name for alias in node.names
                                   if alias.name != "*" and not _package_attribute(target, alias.name))
            else:
                continue
            findings.extend(finding(relative, node.lineno, "本地 Python 导入目标缺失：" + module)
                            for module in modules if not _module_exists(app, module))
    return findings


def _package_attribute(folder: Path, name: str) -> bool:
    init = folder / "__init__.py"
    if not init.is_file():
        return False
    tree = ast.parse(init.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            return True
        if isinstance(node, (ast.Import, ast.ImportFrom)) and any((alias.asname or alias.name.split(".")[0]) == name for alias in node.names):
            return True
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return True
    return False


def _javascript_imports(root: Path) -> list[dict]:
    findings = []
    expression = re.compile(r"(?:\bfrom\s*|\bimport\s*\(?\s*)['\"](\.{1,2}/[^'\"]+)['\"]")
    for file in sorted((root / "plugin").glob("*")):
        if file.suffix not in {".mjs", ".js"}:
            continue
        source = file.read_text(encoding="utf-8")
        for match in expression.finditer(source):
            target = (file.parent / match[1]).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                findings.append(finding(file.relative_to(root).as_posix(), source.count("\n", 0, match.start()) + 1,
                                        "宿主 ES 模块导入目标缺失或越界：" + match[1]))
    return findings


def _versions_and_exports(root: Path) -> list[dict]:
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    path = "skills/autoskill/app/autoskill/__init__.py"
    tree = ast.parse((root / path).read_text(encoding="utf-8"))
    versions = [node for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)]
    findings = []
    if len(versions) != 1 or ast.literal_eval(versions[0].value) != package.get("version"):
        findings.append(finding(path, versions[0].lineno if versions else 1, "Python 与插件包的发布版本不一致"))
    for name, value in package.get("exports", {}).items():
        if isinstance(value, str) and not (root / value).is_file():
            findings.append(finding("package.json", 1, "包导出目标缺失：" + name + " → " + value))
    return findings


def _workflow_references(root: Path) -> list[dict]:
    findings = []
    path_pattern = re.compile(r"(?<![\w./-])((?:scripts|tests)/[\w./*-]+\.(?:py|mjs))")
    for file in sorted((root / ".github/workflows").glob("*.yml")):
        source = file.read_text(encoding="utf-8")
        findings.extend(finding(file.relative_to(root).as_posix(), source.count("\n", 0, match.start()) + 1,
                                "CI 引用的脚本或测试缺失：" + match[1])
                        for match in path_pattern.finditer(source) if not list(root.glob(match[1])))
        # Python heredocs select test filenames without the tests/ prefix.
        findings.extend(finding(file.relative_to(root).as_posix(), source.count("\n", 0, match.start()) + 1,
                                "CI 选择的测试文件缺失：" + match[1])
                        for match in re.finditer(r"['\"](test_[\w-]+\.py)['\"]", source) if not (root / "tests" / match[1]).is_file())
    return findings



def _validation_selections(root: Path) -> list[dict]:
    script = root / "scripts/validate_review.py"
    if not script.is_file():
        return []
    tree = ast.parse(script.read_text(encoding="utf-8"))
    tables = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in {"PYTHON_SUITES", "NODE_SUITES"} for target in node.targets):
            tables[node.targets[0].id] = ast.literal_eval(node.value)
    python_suites = tables.get("PYTHON_SUITES", {})
    node_suites = tables.get("NODE_SUITES", {})
    findings = []
    for file in sorted((root / ".github/workflows").glob("*.yml")):
        for line, source in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
            if "scripts/validate_review.py" not in source:
                continue
            for suite in re.findall(r"--suite\s+([\w-]+)", source):
                if suite not in {*python_suites, *node_suites, "lint", "ruff", "biome", "review-rules"}:
                    findings.append(finding(file.relative_to(root).as_posix(), line, "CI 选择了未知验证范围：" + suite))
                selections = ["tests/" + pattern for pattern in python_suites.get(suite, [])] + node_suites.get(suite, [])
                findings.extend(finding(file.relative_to(root).as_posix(), line, "CI 验证范围引用缺失测试：" + selected)
                                for selected in selections if not list(root.glob(selected)))
    return findings


def _dependency_pins(root: Path) -> list[dict]:
    path = ".github/workflows/lint.yml"
    source = (root / path).read_text(encoding="utf-8")
    match = re.search(r"^\s*names\s*=\s*(\{[^\n]+\})", source, re.MULTILINE)
    if match is None:
        return []
    names = ast.literal_eval(match[1])
    lines = "\n".join(file.read_text(encoding="utf-8") for file in (root / "requirements.txt", root / "requirements-linux.txt")).splitlines()
    findings = []
    for name in sorted(names):
        pins = [line for line in lines if re.match(re.escape(name) + r"(?:\[[^]]+\])?==[^\s;]+(?:\s*;.*)?$", line)]
        if len(pins) != 1:
            findings.append(finding(path, source.count("\n", 0, match.start()) + 1, "CI 契约依赖需要唯一项目版本锁定：" + name))
    return findings


def run(root: Path) -> list[dict]:
    root = root.resolve()
    return [*_python_imports(root), *_javascript_imports(root), *_versions_and_exports(root),
            *_workflow_references(root), *_validation_selections(root), *_dependency_pins(root)]
