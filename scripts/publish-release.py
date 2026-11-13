"""Publish all three platform archives together, after checking every temporary upload."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import urllib.parse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("legacy_publisher", ROOT / "scripts/publish-windows-release.py")
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)
api = legacy.api
TARGETS = {
    "autoSkill-Windows-x64-Offline.zip": ("windows-x64", "python/python.exe"),
    "autoSkill-Linux-x64-Offline.zip": ("linux-x64", "python/bin/python3"),
    "autoSkill-Linux-arm64-Offline.zip": ("linux-arm64", "python/bin/python3"),
}


def verify_inventory(bundle: zipfile.ZipFile, version: str) -> None:
    """Require the complete program inventory before an archive becomes a public upgrade source."""
    manifest = json.loads(bundle.read("package-manifest.json"))
    files = {item.filename for item in bundle.infolist() if not item.is_dir()}
    expected = manifest.get("files", {})
    if manifest.get("format") != 1 or manifest.get("version") != version or not isinstance(expected, dict):
        raise ValueError("Invalid upgrade package manifest")
    if files - {"package-manifest.json"} != set(expected):
        raise ValueError("Incomplete upgrade package inventory")
    for name, digest in expected.items():
        with bundle.open(name) as source:
            actual = hashlib.file_digest(source, "sha256").hexdigest()
        if actual != digest:
            raise ValueError("Upgrade package digest mismatch: " + name)


def describe_archive(archive: Path, version: str) -> dict:
    target, interpreter = TARGETS[archive.name]
    prefix = f"autoskill/vendor/{target}/"
    required = {"package.json", "package-manifest.json", "autoskill-upgrade.py", "cordis.patch.yml", "plugin/index.mjs", "autoskill/SKILL.md",
                "autoskill/offline-manifest.json", prefix + interpreter, prefix + "runtime-manifest.json"}
    required.add("autoskill/autoskill.cmd" if target.startswith("windows") else "setup-linux.sh")
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)) or not required.issubset(names) or any("workspace" in Path(n).parts or ".local" in Path(n).parts
                                              or n.startswith("/") or ".." in Path(n).parts for n in names):
            raise ValueError("Incorrect package layout or private data included: " + archive.name)
        if bundle.testzip() is not None:
            raise ValueError("ZIP CRC failure: " + archive.name)
        manifest = json.loads(bundle.read("autoskill/offline-manifest.json"))
        if manifest.get("autoskill") != version or json.loads(bundle.read("package.json")).get("version") != version:
            raise ValueError("Archive version mismatch: " + archive.name)
        if target.startswith("linux") and manifest.get("target") != target:
            raise ValueError("Archive architecture mismatch: " + archive.name)
        verify_inventory(bundle, version)
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {**manifest, "target": target, "file": archive.name, "bytes": archive.stat().st_size, "sha256": digest,
            "verification": {"mode": "archive-only", "functional_tests_run": False, "zip_crc_ok": True}}


def publish(files: list[tuple[Path, dict]], notes: str, tag: str, commit: str) -> tuple[dict, list[dict]]:
    releases = api("GET", "/releases?per_page=100")
    release = next((r for r in releases if r["tag_name"] == tag), None)
    if release is None:
        release = api("POST", "/releases", {"tag_name": tag, "target_commitish": commit,
                      "name": "autoSkill " + tag, "body": notes, "draft": True})
    old = api("GET", f"/releases/{release['id']}/assets")
    uploaded = []
    for path, local in files:
        temporary = path.name + "." + os.environ.get("GITHUB_RUN_ID", "new") + ".upload"
        for asset in old:
            if asset["name"] == temporary:
                api("DELETE", f"/releases/assets/{asset['id']}")
        item = api("POST", release["upload_url"].split("{")[0] + "?name=" + urllib.parse.quote(temporary), binary=path.read_bytes())
        item["name"] = temporary
        checked = api("GET", f"/releases/assets/{item['id']}")
        if checked["size"] != local["bytes"] or checked.get("digest") != "sha256:" + local["sha256"]:
            raise ValueError("Uploaded size/digest mismatch; prior final assets retained: " + path.name)
        uploaded.append((item, local))
    return legacy.replace_assets(release, old, uploaded, {"name": "autoSkill " + tag, "body": notes,
                                 "draft": False, "prerelease": False, "make_latest": "true"}, request=api)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("build", "proof", "result"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    tag, commit = os.environ["RELEASE_TAG"], os.environ["RELEASE_COMMIT"]
    version = json.loads((ROOT / "package.json").read_text())["version"]
    if tag != "v" + version:
        raise ValueError("Release tag must match package.json version")
    archives = list(args.build.rglob("*.zip"))
    if len(archives) != len(TARGETS) or {p.name for p in archives} != set(TARGETS):
        raise ValueError("Exactly one Windows x64, Linux x64 and Linux ARM64 archive is required")
    archives.sort(key=lambda p: list(TARGETS).index(p.name))
    packages = [describe_archive(p, version) for p in archives]
    args.proof.mkdir(parents=True, exist_ok=True)
    checksum = args.proof / "SHA256SUMS.txt"
    checksum.write_text("".join(p["sha256"] + "  " + p["file"] + "\n" for p in packages), encoding="ascii")
    checksum_data = {"file": checksum.name, "bytes": checksum.stat().st_size, "sha256": hashlib.sha256(checksum.read_bytes()).hexdigest()}
    files = [*zip(archives, packages), (checksum, checksum_data)]
    (args.proof / "archive-check.json").write_text(json.dumps(packages, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    notes = (f"## autoSkill {tag}\n\n提供 3 个按操作系统和 CPU 划分的安装包，另附 SHA256SUMS.txt：\n\n"
             "| 安装包 | 用途 |\n| --- | --- |\n"
             "| autoSkill-Windows-x64-Offline.zip | Windows 10/11 Intel/AMD x64 |\n"
             "| autoSkill-Linux-x64-Offline.zip | Linux x86_64 |\n"
             "| autoSkill-Linux-arm64-Offline.zip | Linux ARM64 / 统信 UOS ARM64（鲲鹏、飞腾等） |\n\n"
             "各包均包含原生 Python、Chrome for Testing、匹配的 chromedriver、agent-browser、Playwright / Selenium 库。"
             "解压根目录可作为 DSH 的本地插件目录。Linux / UOS 先运行 `sh setup-linux.sh`，详见包内 docs/Linux-DSH.md。\n\n"
             "Linux / UOS 需要 glibc >= 2.28 和浏览器依赖的系统共享库，包不替换系统库。旧版 UOS 的系统库可能无法运行包内 Chrome；"
             "可在 setup-linux.sh 中指定系统 Chromium 和匹配驱动。UOS 不在 Playwright 官方支持的发行版列表内，"
             "尚未在 UOS ARM64 真机验证，不承诺所有 UOS 版本开箱即用。DSH 宿主需 Node >= 24.9。\n\n"
             "本版增加可恢复的 Skill 工作流、原生 Selenium/Postman/Apifox/JMeter 资产执行、页面捕获转多轮 AI 对话评测与并发压测、"
             "独立宿主 AI 判定、后台调度和按需分页读取。多轮对话作为一条用例，保留首次真实失败，业务与性能结论分别记录。\n\n"
             "旧版本升级：先在新包目录使用独立 `autoskill-upgrade.py` 检查和准备，再停止旧进程并应用；不要直接覆盖或删除旧 workspace。"
             "新版本也提供 `autoskill upgrade` 给 AI 操作，包含备份、校验、中断恢复与回滚。具体命令与外置数据目录处理见包内升级文档。\n\n"
             "CI 打包阶段不重复运行功能回归；执行组装导入检查、原生 ELF 架构检查、ZIP 结构/CRC、全文件清单、大小和 SHA-256 校验。"
             f"本地功能实测及限制见[验证报告](https://github.com/lance-donif/autoSkill/blob/{tag}/reports/系统化技能与多轮对话验证.md)。\n\n"
             "已知问题：Windows 独立 Selenium 导出脚本曾偶发停留在初始空白页 data:,；本次未宣称已修复。"
             "DSH 宿主接口已用 SDK 替身和真实本地 HTTP/CLI 验证，尚未完成真实 DSH 模型及默认沙箱验收。"
             "本机完整 WebRTC 音频接收受浏览器管理策略阻塞，未计为通过；Windows/UOS ARM64 功能未做真机验证。\n\n"
             f"源码提交：`{commit}`\n\n| 附件 | 字节 | SHA-256 |\n| --- | ---: | --- |\n" +
             "".join(f"| {p['file']} | {p['bytes']:,} | `{p['sha256']}` |\n" for p in packages))
    release, finals = publish(files, notes, tag, commit)
    for package, uploaded in zip(packages, finals):
        package.update(asset_id=uploaded["id"], download_url=uploaded["browser_download_url"])
        package["verification"]["uploaded_digest_verified"] = True
    verification = {"mode": "archive-only", "functional_tests_run": False, "zip_crc_ok": True, "uploaded_digest_verified": True}
    result = {**packages[0], "packages": packages, "package_count": len(packages), "tag": tag, "built_from_commit": commit,
              "release_id": release["id"], "release_url": release["html_url"], "verification": verification,
              "checksum_asset": {**checksum_data, "asset_id": finals[-1]["id"], "download_url": finals[-1]["browser_download_url"]},
              "real_dsh_agent_sandbox_verified": False, "uos_hardware_verified": False}
    (args.proof / "upload-check.json").write_text(json.dumps(finals, indent=2) + "\n", encoding="utf-8")
    args.result.mkdir(parents=True, exist_ok=True)
    (args.result / "offline-package.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"release": release["html_url"], "packages": len(packages)}))


if __name__ == "__main__":
    main()
