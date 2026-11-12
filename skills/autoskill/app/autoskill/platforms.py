"""Native Linux / UOS diagnostics and explicit, offline browser configuration."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

from . import paths


def system_info() -> dict:
    try:
        release = platform.freedesktop_os_release() if sys.platform == "linux" else {}
    except OSError:
        release = {}
    libc, version = platform.libc_ver()
    return {"system": sys.platform, "machine": platform.machine(), "target": paths.runtime_target(),
            "distribution": release.get("PRETTY_NAME", platform.system()),
            "distribution_id": release.get("ID", ""), "libc": libc, "libc_version": version,
            "uos": release.get("ID", "").lower() in ("uos", "deepin") or "uos" in release.get("NAME", "").lower(),
            "uos_hardware_verified": False}


def runtime_problems(info: dict | None = None) -> list[str]:
    info = info or system_info()
    problems = []
    if info["target"] not in ("windows-x64", "linux-x64", "linux-arm64"):
        problems.append("没有这个系统 / CPU 的运行时：" + info["target"])
    if info["system"] == "linux":
        version = tuple(int(p) for p in re.findall(r"\d+", info["libc_version"])[:2])
        if info["libc"] != "glibc" or version < (2, 28):
            problems.append("Linux / UOS 需要 glibc >= 2.28；不支持 musl，不能替换或升级系统 glibc 来硬装")
    return problems


def binary_status(binary: str | None) -> dict:
    if not binary:
        return {"ok": False, "error": "没有找到可执行文件"}
    try:
        done = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=12,
                              env=paths.child_env(), errors="replace")
        output = (done.stdout + done.stderr).strip()
        result = {"ok": done.returncode == 0, "path": binary, "version": output[:500]}
        match = re.search(r"(?:Chrom(?:e|ium)|ChromeDriver)\s+(\d+)\.", output, re.I)
        if match:
            result["major"] = int(match[1])
        if not result["ok"]:
            result["error"] = output[:500] or f"退出码 {done.returncode}"
        return result
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "path": binary, "error": str(error)[:500]}


def browser_pair(chrome: str | None, driver: str | None) -> dict:
    result = {"chrome": binary_status(chrome), "chromedriver": binary_status(driver)}
    result["ok"] = all(item["ok"] for item in result.values())
    if result["ok"] and (not result["chrome"].get("major") or
                          result["chrome"].get("major") != result["chromedriver"].get("major")):
        result.update(ok=False, error="Chromium / Chrome 与 chromedriver 主版本不匹配，或无法识别版本")
    return result


def configure(chrome: str | None = None, driver: str | None = None) -> dict:
    """Prefer the shipped pair; older UOS can explicitly use its own matching Chromium pair."""
    info = system_info()
    problems = runtime_problems(info)
    if problems:
        return {"ok": False, "platform": info, "problems": problems}
    explicit_chrome = chrome or os.environ.get("AUTOSKILL_CHROMIUM")
    explicit_driver = driver or os.environ.get("AUTOSKILL_CHROMEDRIVER")
    pairs = [(explicit_chrome or paths.chrome(bundled=True), explicit_driver or paths.chromedriver())]
    if not explicit_chrome and not explicit_driver:
        for name in ("chromium", "chromium-browser", "google-chrome"):
            if shutil.which(name) and shutil.which("chromedriver"):
                pairs.append((shutil.which(name), shutil.which("chromedriver")))
    attempted = []
    for browser, driver_path in pairs:
        check = browser_pair(browser, driver_path)
        attempted.append(check)
        if not check["ok"]:
            continue
        def portable(value):
            path = Path(value).absolute()
            return path.relative_to(paths.VENDOR).as_posix() if path.is_relative_to(paths.VENDOR) else str(path)
        paths.VENDOR.mkdir(parents=True, exist_ok=True)
        settings = {"chrome": portable(browser), "chromedriver": portable(driver_path)}
        (paths.VENDOR / "runtime-settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"ok": True, "platform": info, "browser_pair": check,
                "settings": str(paths.VENDOR / "runtime-settings.json"),
                "next": "autoskill/autoskill doctor --launch，然后在 DSH 安装解压根目录"}
    return {"ok": False, "platform": info, "attempts": attempted,
            "hint": "包内浏览器缺少系统库时，请由管理员安装系统 Chromium 和匹配的 chromedriver。"
                    "UOS 可运行 sh setup-linux.sh --chromium /绝对路径/chromium --chromedriver /绝对路径/chromedriver。"
                    "具体缺失库见 attempts；不自动下载、不修改系统库。"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium")
    parser.add_argument("--chromedriver")
    parser.add_argument("--launch", action="store_true", help="also launch both browsers on a local fixture")
    args = parser.parse_args()
    result = configure(args.chromium, args.chromedriver)
    if args.launch and result["ok"]:
        from .cli import doctor
        result["doctor"] = doctor(True)
        result["ok"] = result["doctor"]["ok"]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
