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
    """Require the complete program inventory before an archive becomes a public release."""
    manifest = json.loads(bundle.read("package-manifest.json"))
    files = {item.filename for item in bundle.infolist() if not item.is_dir()}
    expected = manifest.get("files", {})
    if manifest.get("format") != 1 or manifest.get("version") != version or not isinstance(expected, dict):
        raise ValueError("Invalid program package manifest")
    if files - {"package-manifest.json"} != set(expected):
        raise ValueError("Incomplete program package inventory")
    for name, digest in expected.items():
        with bundle.open(name) as source:
            actual = hashlib.file_digest(source, "sha256").hexdigest()
        if actual != digest:
            raise ValueError("Program package digest mismatch: " + name)


def _excluded_member(name: str) -> bool:
    """Keep user roots and retired updater code out without excluding library data assets."""
    path = Path(name)
    retired = {"autoskill-upgrade.py", "autoskill/app/autoskill/upgrade.py", "autoskill/app/autoskill/server/upgrades.py"}
    return ("workspace" in path.parts or ".local" in path.parts or name.startswith("/") or ".." in path.parts
            or name in retired or path.parts[:1] == ("data",) or path.parts[:2] == ("autoskill", "data"))


def describe_archive(archive: Path, version: str) -> dict:
    target, interpreter = TARGETS[archive.name]
    prefix = f"autoskill/vendor/{target}/"
    required = {"package.json", "package-manifest.json", "cordis.patch.yml", "plugin/index.mjs", "autoskill/SKILL.md",
                "autoskill/offline-manifest.json", prefix + interpreter, prefix + "runtime-manifest.json"}
    required.add("autoskill/autoskill.cmd" if target.startswith("windows") else "setup-linux.sh")
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)) or not required.issubset(names) or any(_excluded_member(name) for name in names):
            raise ValueError("Incorrect package layout or private data included: " + archive.name)
        if bundle.testzip() is not None:
            raise ValueError("ZIP CRC failure: " + archive.name)
        manifest = json.loads(bundle.read("autoskill/offline-manifest.json"))
        if manifest.get("autoskill") != version or json.loads(bundle.read("package.json")).get("version") != version:
            raise ValueError("Archive version mismatch: " + archive.name)
        if target.startswith("linux") and manifest.get("target") != target:
            raise ValueError("Archive architecture mismatch: " + archive.name)
        verify_inventory(bundle, version)
        ui_files = verify_ui_source(bundle)
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {**manifest, "target": target, "file": archive.name, "bytes": archive.stat().st_size, "sha256": digest, "ui_files": ui_files,
            "verification": {"mode": "archive-only", "functional_tests_run": False, "zip_crc_ok": True, "ui_source_verified": True}}


def verify_ui_source(bundle: zipfile.ZipFile) -> dict:
    """Check every shipped UI byte against the tag checkout, including the Apple-style dashboard."""
    source = ROOT / "skills/autoskill/app/ui"
    files = {"autoskill/app/ui/" + file.relative_to(source).as_posix(): file for file in source.rglob("*") if file.is_file()}
    actual = {name for name in bundle.namelist() if name.startswith("autoskill/app/ui/") and not name.endswith("/")}
    if actual != set(files):
        raise ValueError("UI file inventory differs from the release source")
    digests = {}
    for name, file in files.items():
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        if hashlib.sha256(bundle.read(name)).hexdigest() != digest:
            raise ValueError("UI source digest mismatch: " + name)
        digests[name] = digest
    return digests


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
             "## 本版变化\n\n"
             "- 新增页面压测入口：复用已有 UI/API 用例与原断言，配置并发、迭代和渐增负载，查看进度、吞吐、失败与阻塞率及耗时分位数，支持停止和历史报告。\n"
             "- 用例支持嵌套文件夹、批量移动、按目录递归导出；项目资料统一查看知识、用例原文件与报告，支持资料归档和项目分享导入。\n"
             "- 设计用例表与报告遵循附带 test-cases Skill 的 14 列格式，保留需求来源、真人操作步骤、独立预期和规范编号；关联真实动作、检查与执行证据，设计文字不冒充已执行。\n"
             "- 需求迭代可按来源关联分析影响、选择已有用例并建立维护范围；全部约定回归完成后才发布满足要求的候选，未选用例与旧版本失败保留。\n"
             "- 补齐原生资产、原生/多轮报告、对话捕获构建、需求工作流、设计表、执行器诊断和项目设置页面；定时任务支持回放、原生资产与 AI 维护。\n"
             "- DSH 仅保留无参数 `/autoskill-use`：加载技能后直接对话描述任务，不传 task/mode；移除输入区任务栏、自动任务弹层/通知和强制待办，工具结果默认折叠。\n"
             "- 修复隐藏窗口偏好在 DSH、录制、回放、登录及定时任务中失效；修复筛选抖动、窄屏表格溢出、录制控制兼容及 Python 继承 DSH IPv6 代理绕过项的问题。\n\n"
             "## 数据与更新\n\n"
             "程序与数据目录分离：升级只替换程序并重新加载 DSH 插件，继续使用原外置数据目录；本发布包不包含用户数据或模型密钥。"
             "知识、用例、报告及版本来源按项目归档，旧证据保留原字节。项目分享包携带项目报告附件，完整运行历史、截图、凭据和登录态使用 `autoskill data export` 备份。"
             "首次从旧 workspace 迁入时使用内置数据管理或 `data import` 复制到空目录，不覆盖或合并已有目录。\n\n"
             "## 实测与验证边界\n\n"
             "- 本轮真实 Linux DSH 和远程模型调用技能执行冻结自测资产：104 个父测试与 20 条子测试通过，共 124 条原生结果，0 失败、0 跳过；另完成 12 页 × 3 种规格的 36 个真实 iframe 视图巡检。\n"
             "- 菜鸟教程最终版本新录制的 6 条独立检查与 UI 回放通过，14 列 Excel 保留完整来源设计与 FT-JCYF-001 编号，三个任务报告实际由 DSH present 交付。\n"
             "- Selenium 官方站点 17 条用例的当前最新记录为 12 Pass、5 上游连接 Fail；失败与各轮证据原样保留，不并入上述插件自测分母，也不将连接问题认作网站功能缺陷。\n"
             "- CI 打包不重复运行功能回归；执行组装导入、原生平台架构、ZIP 结构/CRC、全文件清单、包内 UI 与源码逐字节一致、大小和上传 SHA-256 校验。\n\n"
             f"详见[本轮 DSH 实测](https://github.com/lance-donif/autoSkill/blob/{tag}/reports/DSH真实验收.md)、"
             f"[页面专项](https://github.com/lance-donif/autoSkill/blob/{tag}/reports/页面压测入口验证.md)和"
             f"[Selenium 官方站点专项](https://github.com/lance-donif/autoSkill/blob/{tag}/reports/Selenium官方测试页验证.md)。"
             "自测资产为预编写回归与合成场景，不代表模型自主设计准确率或生产压测容量。Windows 与 UOS ARM64 本轮未做真机功能验收；"
             "历史 Windows 独立 Selenium 导出脚本偶发停留 data:, 的问题未在本轮设备复验，WebRTC 音频受浏览器策略阻塞仍未计为通过。\n\n"
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
