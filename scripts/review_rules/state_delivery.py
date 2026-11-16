"""Check prose, current/history verdicts and stable Excel delivery in an isolated process."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def run(root: Path) -> list[dict]:
    with tempfile.TemporaryDirectory(prefix="autoskill-state-delivery-") as directory:
        environment = {**os.environ, "AUTOSKILL_WORKSPACE": directory,
                       "AUTOSKILL_CASE_SKILL": str(Path(directory) / "sheet"), "AUTOSKILL_SESSION": "state-delivery-rule",
                       "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_HEADED": "0", "TMPDIR": directory, "PYTHONDONTWRITEBYTECODE": "1",
                       "PYTHONPATH": os.pathsep.join(str(root / part) for part in ("skills/autoskill/app", "tests"))}
        try:
            result = subprocess.run([sys.executable, "-m", "unittest", "test_state_delivery_contracts"],
                                    cwd=root, env=environment, capture_output=True, text=True, timeout=30, check=False)
        except subprocess.TimeoutExpired:
            message = "状态与交付契约检查超过 30 秒；隔离测试未完成。"
        else:
            if result.returncode == 0:
                return []
            message = "状态与交付契约失败：\n" + (result.stdout + result.stderr)[-6000:]
    return [{"rule": "state_delivery", "severity": "error", "path": "tests/test_state_delivery_contracts.py",
             "line": 1, "message": message}]
