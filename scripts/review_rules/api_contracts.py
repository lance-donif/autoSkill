"""Check concrete credential-template and API verdict contracts in an isolated interpreter."""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROBE = r'''
import json
from autoskill.core import store
from autoskill import runner
findings = []
for value in ("token={{token}}", "token={{token}}; csrf=${ENV:CSRF}"):
    if store.credential_errors({"headers": {"Cookie": value}}):
        findings.append(["api_cookie_references", "core/store.py", "credential_errors",
                         "合法 Cookie 名称加完整变量/环境引用被误报为明文凭据：" + value])
if not store.credential_errors({"headers": {"Cookie": "token={{token}}; other=literal-fixture-value"}}):
    findings.append(["api_cookie_literals", "core/store.py", "credential_errors",
                     "Cookie 的一个合法引用不能掩盖另一项明文凭据"])
for status in (200, 201, 202):
    case = {"kind": "api", "assertions": {"status": status}, "extensions": {}}
    outcome = {"status": "Pass", "assertions": [{"passed": True, "expected": status, "actual": status}]}
    if runner.verification(case, outcome)["business_status"] != "Unverified":
        findings.append(["api_status_is_not_business_verification", "runner.py", "verification",
                         f"HTTP {status} 状态单独通过时被标记为业务已验证"])
action = {"id": "DELETE", "kind": "api", "lifecycle": "active", "assertions": {"status": 201},
          "extensions": {"postconditions": ["EMPTY"]}}
check = {"id": "EMPTY", "kind": "api", "lifecycle": "active", "assertions": {"status": 200, "json_equals": {"/item": None}},
         "extensions": {"depends_on": ["DELETE"]}}
try:
    plan = runner.build_plan(["DELETE"], {"DELETE": action, "EMPTY": check})
except (ValueError, AttributeError) as error:
    findings.append(["api_explicit_postcondition", "runner.py", "build_plan", "合法删除后查空关系不能执行：" + str(error)])
else:
    if plan != [("DELETE", False), ("EMPTY", False)]:
        findings.append(["api_explicit_postcondition", "runner.py", "build_plan", "单选动作没有按顺序执行后置，或后置被误标为前置"])
for assertions, extensions in (({"status": 200}, {"depends_on": ["DELETE"]}),
                               ({"status": 200, "json_equals": {"/item": None}}, {}),
                               ({"status": 200, "json_equals": {"/item": None}}, {"depends_on": ["DELETE"], "oracle": {"source": "observation"}})):
    altered = {**check, "assertions": assertions, "extensions": extensions}
    try:
        runner.build_plan(["DELETE"], {"DELETE": action, "EMPTY": altered})
    except (ValueError, AttributeError):
        continue
    findings.append(["api_postcondition_requires_independent_dependency", "runner.py", "validate_postconditions",
                     "后置允许缺少独立检查、依赖关系或仅有录制观察"])
print(json.dumps(findings, ensure_ascii=False))
'''


def _line(file: Path, function: str) -> int:
    tree = ast.parse(file.read_text(encoding="utf-8"))
    return next((node.lineno for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == function), 1)


def run(root: Path) -> list[dict]:
    with tempfile.TemporaryDirectory(prefix="autoskill-api-contracts-") as folder:
        environment = {**os.environ, "PYTHONPATH": str(root / "skills/autoskill/app"), "PYTHONDONTWRITEBYTECODE": "1",
                       "AUTOSKILL_WORKSPACE": folder, "AUTOSKILL_BRIDGE": "off", "TMPDIR": folder}
        result = subprocess.run([sys.executable, "-c", PROBE], cwd=root, env=environment, text=True,
                                capture_output=True, check=False, timeout=30)
    if result.returncode:
        return [{"rule": "api_contract_probe", "severity": "error", "path": "scripts/review_rules/api_contracts.py", "line": 1,
                 "message": "API 契约探针未完成：" + result.stderr.strip()[-500:]}]
    findings = []
    for rule, relative, function, message in json.loads(result.stdout):
        path = "skills/autoskill/app/autoskill/" + relative
        findings.append({"rule": rule, "severity": "error", "path": path, "line": _line(root / path, function), "message": message})
    return findings
