"""Check real HTTP lifecycle semantics with isolated local data and fixed oracles."""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TEST_FILE = "tests/test_review_api_flows.py"


def _line(root: Path, name: str) -> int:
    tree = ast.parse((root / TEST_FILE).read_text(encoding="utf-8"))
    return next((node.lineno for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name), 1)


def run(root: Path) -> list[dict]:
    """Execute fixed synthetic contracts; never use a real gateway or the user's data root."""
    root = root.resolve()
    with tempfile.TemporaryDirectory(prefix="autoskill-api-rule-") as temporary:
        report = Path(temporary) / "result.json"
        environment = {**os.environ, "TMPDIR": temporary, "PYTHONDONTWRITEBYTECODE": "1", "AUTOSKILL_WORKSPACE": str(Path(temporary) / "workspace")}
        command = [sys.executable, str(root / TEST_FILE), "--json-output", str(report)]
        try:
            completed = subprocess.run(command, cwd=temporary, env=environment, capture_output=True, text=True, check=False, timeout=90)
        except subprocess.TimeoutExpired:
            return [{"rule": "api_flows", "severity": "error", "path": TEST_FILE, "line": 1,
                     "message": "隔离HTTP契约验证超时，未取得完整执行证据；不能视作通过"}]
        if not report.is_file():
            return [{"rule": "api_flows", "severity": "error", "path": TEST_FILE, "line": 1,
                     "message": "接口审查验证器未生成结果：" + completed.stderr[-1500:]}]
        outcome = json.loads(report.read_text(encoding="utf-8"))
        if destination := os.environ.get("AUTOSKILL_REVIEW_API_RESULT"):
            Path(destination).write_text(json.dumps(outcome, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    findings = []
    for failure in [*outcome["failures"], *outcome["errors"]]:
        name = failure["test"].rsplit(".", 1)[-1]
        findings.append({"rule": "api_flows", "severity": "error", "path": TEST_FILE, "line": _line(root, name),
                         "message": name + "：" + "\n".join(failure["traceback"].strip().splitlines()[-8:])})
    findings.extend({"rule": "api_flows", "severity": "error", "path": TEST_FILE, "line": _line(root, skipped["test"].rsplit(".", 1)[-1]),
                     "message": "本地接口契约未执行：" + skipped["reason"]} for skipped in outcome["skipped"])
    if not findings and (completed.returncode or not outcome["ok"] or not outcome["tests_run"]):
        findings.append({"rule": "api_flows", "severity": "error", "path": TEST_FILE, "line": 1,
                         "message": "验证器没有完成全部接口契约，不能仅凭空finding判定通过"})
    return findings
