"""Keep new report artifacts with their project while retaining original historical locations."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from . import data_refs, execution_inputs, paths, projects


def _legacy(report_id: str) -> Path:
    if re.fullmatch(r"run_[A-Za-z0-9][A-Za-z0-9_-]*", report_id):
        return paths.local() / "runs" / report_id.removeprefix("run_") / "test-report"
    if re.fullmatch(r"(?:workflow|recording)_[a-f0-9]{64}", report_id):
        return paths.local() / "task-reports" / report_id
    raise ValueError("测试报告标识无效")


def _index(report_id: str) -> Path:
    _legacy(report_id)
    return paths.local() / "task-report-locations" / (report_id + ".json")


def folder(report_id: str) -> Path:
    legacy = _legacy(report_id)
    index = _index(report_id)
    if index.is_file():
        location = json.loads(index.read_bytes())
        project_id = location.get("project_id", "")
        if not isinstance(project_id, str) or not projects.ID.fullmatch(project_id):
            raise ValueError("项目报告位置索引无效")
        selected = data_refs.resolve(location.get("folder"))
        expected = paths.projects_dir() / project_id / "reports" / report_id
        if selected != expected or not selected.resolve().is_relative_to(paths.workspace().resolve()):
            raise ValueError("项目报告路径不属于当前数据目录")
        return selected
    if not legacy.resolve().is_relative_to(paths.local().resolve()):
        raise ValueError("测试报告路径不属于当前工作区")
    return legacy


def bind(report_id: str, project_id: str) -> Path:
    """Bind before the first write; a retained legacy directory is never relocated or rehashed."""
    selected = folder(report_id)
    if _index(report_id).is_file() or selected.exists():
        return selected
    target = projects.path(project_id) / "reports" / report_id
    if not target.resolve().is_relative_to(paths.workspace().resolve()):
        raise ValueError("项目报告路径不属于当前数据目录")
    execution_inputs.write_json(_index(report_id), {"project_id": project_id, "folder": data_refs.store(target)})
    return target


def remove_run(run_id: str) -> None:
    """Remove a run's project report alongside its existing execution deletion journal."""
    report_id = "run_" + run_id
    selected = folder(report_id)
    index = _index(report_id)
    if not index.is_file():
        return
    if selected.is_symlink():
        raise ValueError("项目报告目录不能是外部链接")
    if selected.exists():
        shutil.rmtree(selected)
    index.unlink(missing_ok=True)
