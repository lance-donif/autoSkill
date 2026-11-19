"""Native Python, collection CLI and JMeter adapters, preserving original files and invocation arguments."""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import secrets
from . import reports
from .process import Process

SPECS = {
    "selenium-pytest": ("Python / pytest 原项目", "AUTOSKILL_NATIVE_PYTHON", "python", "*.py"),
    "selenium-unittest": ("Python / unittest 原项目", "AUTOSKILL_NATIVE_PYTHON", "python", "*.py"),
    "postman-newman": ("Postman / Newman", "AUTOSKILL_NEWMAN", "newman", "*.json"),
    "apifox": ("Apifox CLI", "AUTOSKILL_APIFOX", "apifox", "*.json"),
    "jmeter": ("Apache JMeter", "AUTOSKILL_JMETER", "jmeter", "*.jmx"),
}


def _npm_command(executable: str, package: str, entry: str) -> list[str]:
    if Path(executable).suffix.lower() not in (".cmd", ".bat"):
        return [executable]
    root = Path(executable).parent
    candidates = (root / "node_modules" / package / entry, root.parent / package / entry)
    script = next((file for file in candidates if file.is_file()), None)
    node = shutil.which("node")
    if script and node:
        return [node, str(script)]
    raise ValueError(f"无法定位 {package} 的 Node 入口，请检查 CLI 安装与 node PATH")


def _runtime(engine: str, override: str | None = None) -> list[str]:
    _, variable, default, _ = SPECS[engine]
    executable = override or os.environ.get(variable) or (sys.executable if engine.startswith("selenium-") else shutil.which(default))
    if not executable:
        raise ValueError(f"没有找到 {default}；请安装原生工具后配置 {variable} 或 PATH（不会自动下载）")
    executable = shutil.which(executable) or executable
    if not Path(executable).is_file():
        raise ValueError(f"{variable} 指向的可执行文件不存在")
    if engine == "postman-newman":
        return _npm_command(executable, "newman", "bin/newman.js")
    if engine == "apifox":
        return _npm_command(executable, "apifox-cli", "bin/cli.js")
    if engine == "jmeter" and Path(executable).suffix.lower() in (".bat", ".cmd"):
        java = shutil.which("java")
        jar = Path(executable).parent / "ApacheJMeter.jar"
        if not java or not jar.is_file():
            raise ValueError("JMeter 需要 Java 和 bin/ApacheJMeter.jar，请检查原生安装")
        return [java, "-Djava.awt.headless=true", "-jar", str(jar)]
    return [executable]


def _entry_args(argv: list[str], engine: str) -> list[str]:
    """Replace only the launcher, retaining the original module and all test arguments."""
    first = Path(argv[0]).stem.lower()
    if engine.startswith("selenium-"):
        if first.startswith("python"):
            return argv[1:]
        if first in ("pytest", "unittest"):
            return ["-m", first, *argv[1:]]
        return argv
    launchers = {"newman", "apifox", "jmeter"}
    return argv[1:] if first in launchers else argv


def _mapped_args(argv: list[str], asset: dict, root: Path) -> list[str]:
    original_root = Path(asset.get("source_root") or asset.get("origin", {}).get("path", ""))
    mapped = []
    for value in argv:
        result = value
        path = Path(value)
        if path.is_absolute():
            with contextlib.suppress(ValueError):
                result = str(root / path.relative_to(original_root))
        mapped.append(result)
    return mapped


def _without_options(args: list[str], valued: tuple[str, ...], flags: tuple[str, ...] = ()) -> list[str]:
    """Replace reporting destinations, preserving every unrelated native option."""
    result, index = [], 0
    while index < len(args):
        value = args[index]
        if value in valued:
            index += 2
            continue
        attached = any(value.startswith(option + "=") or (len(option) == 2 and value.startswith(option) and len(value) > 2) for option in valued)
        if value not in flags and not attached:
            result.append(value)
        index += 1
    return result


def _reporters(args: list[str], required: list[str]) -> list[str]:
    selected = list(required)
    for index, value in enumerate(args):
        if value in ("--reporters", "-r") and index + 1 < len(args):
            selected.extend(args[index + 1].split(","))
        elif value.startswith("--reporters="):
            selected.extend(value.split("=", 1)[1].split(","))
    return [*_without_options(args, ("--reporters", "-r")), "--reporters", ",".join(dict.fromkeys(selected))]


class NativeAdapter:
    def __init__(self, engine: str):
        self.id = engine

    def capabilities(self) -> dict:
        return {"id": self.id, "title": SPECS[self.id][0], "family": "native", "protocol_version": 1,
                "asset_formats": [SPECS[self.id][3]], "preserves_tree": True, "conversion": False,
                "events": True, "heartbeat": True, "cancel": True, "results": "native per-case reports",
                "scope": ("执行 CLI 支持的本地集合；云端项目、私有扩展和脚本兼容性按实际 CLI 报告判定" if self.id == "apifox" else "原生运行"),
                "measurement_only_is_business_verified": False}

    def probe(self, executable: str | None = None) -> dict:
        try:
            command = _runtime(self.id, executable)
            args = ["-m", "pytest", "--version"] if self.id == "selenium-pytest" else ["--version"]
            if self.id == "jmeter":
                args = ["-j", os.devnull, "--version"]
            result = subprocess.run([*command, *args], capture_output=True, text=True, timeout=15, check=False)
            output = (result.stdout + result.stderr).strip()
            available = result.returncode == 0
            return {"id": self.id, "available": available, "command": command, "version": output[:1000] if available else "",
                    "diagnosis": "" if available else f"原生工具探测失败：{output[:1000]}"}
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            return {"id": self.id, "available": False, "version": "", "diagnosis": str(error)}

    def probe_asset(self, asset: dict) -> dict:
        entry = Path(asset["argv"][0])
        explicit = entry.is_absolute() or entry.parent != Path(".")
        launcher = entry.stem.lower()
        if explicit and (launcher.startswith("python") or launcher in ("newman", "apifox", "jmeter")):
            if not entry.is_absolute():
                entry = Path(asset["source_root"]) / asset["cwd"] / entry
            return self.probe(str(entry))
        return self.probe()

    def discover(self, root: str) -> list[dict]:
        source = Path(root)
        files = [source] if source.is_file() else sorted(source.rglob(SPECS[self.id][3]))
        return [{"path": str(file), "engine": self.id, "discovery": "文件发现；可执行性由原生工具验证"} for file in files]

    def default_entry(self, files: list[str], cwd: str) -> list[str]:
        if self.id.startswith("selenium-"):
            return ["python", "-m", "pytest"] if self.id == "selenium-pytest" else ["python", "-m", "unittest", "discover"]
        suffix = ".jmx" if self.id == "jmeter" else ".json"
        choices = [name for name in files if name.endswith(suffix)]
        if len(choices) != 1:
            raise ValueError("资产中有多个或没有入口文件，请显式提供原入口 argv")
        entry = os.path.relpath(choices[0], cwd).replace("\\", "/")
        return ["jmeter", "-n", "-t", entry] if self.id == "jmeter" else ["newman" if self.id == "postman-newman" else "apifox", "run", entry]

    def prepare(self, asset: dict, root: Path, evidence: Path, parameters: dict) -> dict:
        probe = asset.get("_runtime") or self.probe_asset(asset)
        if not probe["available"]:
            raise ValueError(probe["diagnosis"])
        args = _entry_args(_mapped_args(asset["argv"], asset, root), self.id)
        extra = parameters.get("args", [])
        if not isinstance(extra, list):
            raise ValueError("执行参数 args 必须是字符串数组")
        args += extra
        args = secrets.resolve(args)
        if not all(isinstance(value, str) for value in args):
            raise ValueError("执行参数 args 必须是字符串数组")
        cwd = (root / asset["cwd"]).resolve()
        environment = secrets.resolve(dict(parameters.get("env", {})))
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in environment.items()):
            raise ValueError("执行环境 env 必须是字符串键值")
        evidence.mkdir(parents=True, exist_ok=True)
        command, report = self._command(probe["command"], args, environment, evidence)
        timeout = parameters.get("timeout_seconds")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0):
            raise ValueError("timeout_seconds 需要是大于零的秒数")
        return {"engine": self.id, "argv": command, "cwd": str(cwd), "env": environment, "timeout": timeout,
                "evidence": str(evidence), "report": str(report), "runtime": probe, "parameters": parameters}

    def _command(self, runtime: list[str], args: list[str], environment: dict, evidence: Path) -> tuple[list[str], Path]:
        if self.id == "selenium-pytest":
            report = evidence / "junit.xml"
            return [*runtime, *_without_options(args, ("--junitxml", "--junit-xml")), f"--junitxml={report}"], report
        if self.id == "selenium-unittest":
            report = evidence / "unittest.json"
            environment.update(AUTOSKILL_UNITTEST_REPORT=str(report), AUTOSKILL_UNITTEST_ENTRY=json.dumps(args))
            return [*runtime, str(Path(__file__).with_name("unittest_runner.py"))], report
        if self.id == "apifox":
            report = evidence / "apifox.json"
            args = _reporters(_without_options(args, ("--out-dir", "--out-file")), ["cli", "json", "junit"])
            return [*runtime, *args, "--out-dir", str(evidence), "--out-file", "apifox"], report
        if self.id == "postman-newman":
            report = evidence / "report.json"
            args = _reporters(_without_options(args, ("--reporter-json-export",)), ["cli", "json"])
            return [*runtime, *args, "--reporter-json-export", str(report)], report
        report = evidence / "samples.jtl"
        args = _without_options(args, ("-l", "--logfile", "-j", "--jmeterlogfile", "-o", "--reportoutputfolder"), ("-e", "--reportatendofloadtests", "-n", "--nongui"))
        return [*runtime, *args, "-n", "-l", str(report), "-j", str(evidence / "jmeter.log"), "-e", "-o", str(evidence / "html"),
                "-Jjmeter.save.saveservice.output_format=csv", "-Jjmeter.save.saveservice.print_field_names=true",
                "-Jjmeter.save.saveservice.assertion_results_failure_message=true"], report

    def execute(self, prepared: dict, *, cancelled=None, emit=None) -> dict:
        process = Process(prepared, emit)
        try:
            return process.wait(cancelled)
        finally:
            process.cleanup()

    def collect(self, prepared: dict, raw: dict) -> dict:
        report, evidence = Path(prepared["report"]), Path(prepared["evidence"])
        cases, metrics = [], {}
        try:
            if self.id == "selenium-unittest" and (report.is_file() or report.with_suffix(".jsonl").is_file()):
                cases = reports.unittest_report(report, raw.get("reason") or ("运行已取消" if raw.get("state") == "cancelled" else ""))
            elif report.is_file():
                if self.id == "selenium-pytest":
                    cases = reports.junit(report)
                elif self.id in ("postman-newman", "apifox"):
                    cases = reports.newman(report)
                    if self.id == "apifox" and (evidence / "apifox.xml").is_file():
                        cases = [*reports.junit(evidence / "apifox.xml"), *(item for item in cases if item["status"] == "Blocked")]
                else:
                    cases, metrics = reports.jmeter(report, prepared["parameters"].get("thresholds", {}))
        except (OSError, ValueError, KeyError, TypeError, ET.ParseError) as error:
            raw = {**raw, "state": "blocked", "reason": "无法读取原生报告：" + str(error)}
        return reports.summarize(cases, raw, evidence, metrics)

    def cancel(self, prepared: dict) -> None:
        (Path(prepared["evidence"]) / "cancel").touch()

    def heartbeat(self, prepared: dict) -> dict:
        file = Path(prepared["evidence"]) / "heartbeat.json"
        return json.loads(file.read_text(encoding="utf-8")) if file.is_file() else {}

    def events(self, prepared: dict) -> list[dict]:
        file = Path(prepared["evidence"]) / "events.jsonl"
        if not file.is_file():
            return []
        entries = []
        for line in file.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(ValueError):
                entries.append(json.loads(line))
        return entries

    def cleanup(self, prepared: dict) -> None:
        """Preserve logs, reports and the frozen tree; no source file is changed."""
        (Path(prepared["evidence"]) / "process-spec.json").unlink(missing_ok=True)
