"""Maintainer-only: assemble the Windows x64 offline package from assets fetched by fetch-windows-assets.py.

No network access. The result is a folder (and a zip) that runs on a machine
with nothing installed: its own Python, Chrome, chromedriver, agent-browser and
libraries. End users never run this script.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "autoskill"
# Chrome locale packs kept in the package; the rest only translate the browser's own menus.
LOCALES = ("en-US.pak", "zh-CN.pak")
# Parts of Chrome for Testing that install, update or report: not needed to automate a page.
CHROME_DROP = ("setup.exe", "elevation_service.exe", "elevated_tracing_service.exe", "notification_helper.exe",
               "chrome_proxy.exe", "chrome_pwa_launcher.exe", "chrome_wer.dll", "PrivacySandboxAttestationsPreloaded",
               "MEIPreload", "first_party_sets.db")
SKIP_NAMES = {"__pycache__", "vendor", "workspace", ".local"}
SKIP_SUFFIXES = {".pyc", ".pyo"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def unzip(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            if not (destination / member.filename).resolve().is_relative_to(destination.resolve()):
                raise ValueError("Unsafe archive member: " + member.filename)
        bundle.extractall(destination)


def copy_tree(source: Path, destination: Path) -> None:
    def ignore(folder, names):
        return [n for n in names if n in SKIP_NAMES or Path(n).suffix in SKIP_SUFFIXES or (Path(folder) == source and n == "data")]

    shutil.copytree(source, destination, ignore=ignore)


def copy_package_docs(output: Path, version: str) -> None:
    """Keep skill links local in the bundle; historical evidence lives in the tagged repository."""
    shutil.copytree(ROOT / "docs", output / "docs")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme = readme.replace("](skills/autoskill/", "](autoskill/")
    readme = readme.replace("](reports/", f"](https://github.com/lance-donif/autoSkill/blob/v{version}/reports/")
    (output / "README.md").write_text(readme, encoding="utf-8", newline="\n")


def write_package_manifest(output: Path) -> None:
    """Hash every immutable program file without bundling user data or migration helpers."""
    config = json.loads((output / "package.json").read_text(encoding="utf-8"))
    config["files"] = [name for name in config["files"] if name != "autoskill-upgrade.py"]
    if "package-manifest.json" not in config["files"]:
        config["files"].append("package-manifest.json")
    (output / "package.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    inventory = {file.relative_to(output).as_posix(): sha256(file) for file in sorted(output.rglob("*"))
                 if file.is_file() and file != output / "package-manifest.json"}
    (output / "package-manifest.json").write_text(json.dumps({"format": 1, "version": config["version"], "files": inventory},
                                                           ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check_sources(assets: Path) -> dict:
    """Every downloaded archive must still match the hash recorded when it was fetched."""
    recorded = json.loads((assets / "download-sources.json").read_text(encoding="utf-8"))
    folders = {"python": "python", "chrome": "chrome", "chromedriver": "chromedriver", "agent_browser": "agent-browser"}
    if "agent_browser_native" in recorded["sources"]:
        folders["agent_browser_native"] = "agent-browser"
    for key, folder in folders.items():
        item = recorded["sources"][key]
        if sha256(assets / folder / item["file"]) != item["sha256"]:
            raise ValueError("Downloaded archive changed since it was fetched: " + item["file"])
    for wheel in recorded["wheels"]:
        if sha256(assets / "wheels" / wheel["file"]) != wheel["sha256"]:
            raise ValueError("Wheel changed since it was fetched: " + wheel["file"])
    return recorded


def _agent_guide(archive: tarfile.TarFile) -> dict[str, bytes]:
    prefix = "package/skill-data/core/"
    files = {}
    for member in archive.getmembers():
        if not member.isfile() or not member.name.startswith(prefix):
            continue
        relative = member.name[len(prefix):]
        if Path(relative).is_absolute() or ".." in Path(relative).parts or "\\" in relative:
            raise ValueError("Unsafe agent-browser guide member: " + member.name)
        files[relative] = archive.extractfile(member).read()
    if "SKILL.md" not in files:
        raise ValueError("agent-browser core guide is missing SKILL.md")
    return files


def install_agent_browser(assets: Path, recorded: dict, package: Path, target: str) -> None:
    """Install the pinned core guide and native platform package without running npm."""
    version = recorded["agent_browser_version"]
    core_file = assets / "agent-browser" / recorded["sources"]["agent_browser"]["file"]
    native_source = recorded["sources"].get("agent_browser_native")
    native_names = {"linux-x64": "cli-linux-x64-gnu", "linux-arm64": "cli-linux-arm64-gnu", "windows-x64": "cli-win32-x64-msvc"}
    native_name = "agent-browser.exe" if target == "windows-x64" else "agent-browser"
    with tarfile.open(core_file) as archive:
        metadata = json.loads(archive.extractfile("package/package.json").read())
        if metadata.get("name") != "agent-browser" or metadata.get("version") != version:
            raise ValueError("agent-browser core archive does not match its recorded version")
        guide_files = _agent_guide(archive)
        licenses = {"LICENSE": archive.extractfile("package/LICENSE").read()}
        for member in archive.getmembers():
            if member.isfile() and member.name.startswith("package/cli/src/native/a11y/LICENSE-"):
                licenses[Path(member.name).name] = archive.extractfile(member).read()
        if not native_source:
            legacy_name = "agent-browser-win32-x64.exe" if target == "windows-x64" else "agent-browser-" + target
            binary = archive.extractfile("package/bin/" + legacy_name).read()
    if native_source:
        with tarfile.open(assets / "agent-browser" / native_source["file"]) as archive:
            metadata = json.loads(archive.extractfile("package/package.json").read())
            if metadata.get("name") != "@agent-browser/" + native_names[target] or metadata.get("version") != version:
                raise ValueError("agent-browser native archive does not match its platform or version")
            binary = archive.extractfile("package/bin/" + native_name).read()
    agent = package / "vendor" / target / "agent-browser"
    agent.mkdir(parents=True, exist_ok=True)
    executable = agent / native_name
    executable.write_bytes(binary)
    executable.chmod(0o755)
    for name, payload in licenses.items():
        (agent / name).write_bytes(payload)
    guide = package / "references/agent-browser-core.offline"
    if guide.exists():
        shutil.rmtree(guide)
    for name, payload in guide_files.items():
        destination = guide / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    source = {"package": "agent-browser@" + version, "source_path": "skill-data/core", "license": "Apache-2.0",
              "files": {name: hashlib.sha256(payload).hexdigest() for name, payload in sorted(guide_files.items())}}
    (guide / "source.json").write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def smoke_test(package: Path) -> None:
    """Start the packaged interpreter with an empty environment and import the packaged app."""
    import os
    import subprocess

    python = package / "vendor" / "windows-x64" / "python" / "python.exe"
    env = {
        "SystemRoot": os.environ.get("SystemRoot", "C:/WINDOWS"),  # noqa: SIM112  # Preserve the canonical Windows key on case-sensitive hosts.
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "LOCUST_SKIP_MONKEY_PATCH": "1",
    }
    code = "import autoskill, playwright, selenium, httpx, fastapi, uvicorn, openpyxl, jsonschema, locust; print(autoskill.__file__)"
    done = subprocess.run([str(python), "-c", code], env=env, capture_output=True, text=True, cwd=str(package.parent), check=False)
    located = done.stdout.strip()
    if done.returncode or not Path(located).resolve().is_relative_to(package.resolve()):
        raise ValueError("Packaged interpreter cannot start the packaged app: " + (done.stderr.strip() or located)[-400:])


def plugin_check(output: Path) -> None:
    """The packaged plugin must find the packaged skill and its bundled Python (needs Node on the build machine)."""
    import subprocess

    node = shutil.which("node")
    if not node:
        return
    code = ("const m = await import(process.argv[1]); const s = m.runtimeSpec();"
            "console.log(JSON.stringify({skill: m.SKILL_DIR, python: s.python, bundled: s.bundled}));")
    url = (output / "plugin" / "index.mjs").resolve().as_uri()
    env = {k: v for k, v in __import__("os").environ.items() if k != "AUTOSKILL_DEV_PYTHON"}
    done = subprocess.run([node, "--input-type=module", "-e", code, url], capture_output=True, text=True, env=env, encoding="utf-8", check=False)
    found = json.loads(done.stdout) if done.returncode == 0 and done.stdout.strip() else {}
    if not found or found.get("python") != found.get("bundled") or not Path(found["skill"]).resolve().is_relative_to(output.resolve()):
        raise ValueError("Packaged DSH plugin cannot find the packaged runtime: " + (done.stderr.strip() or done.stdout.strip())[-400:])


def _install_wheels(assets: Path, site: Path) -> list[dict]:
    """Install sorted wheels before assembling browser assets."""
    wheels = []
    for wheel in sorted((assets / "wheels").glob("*.whl")):
        with zipfile.ZipFile(wheel) as archive:
            meta_name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
            meta = Parser().parsestr(archive.read(meta_name).decode("utf-8"))
        if "telemetry" in meta["Name"].lower():
            raise ValueError("Telemetry dependency found: " + meta["Name"])
        wheels.append({"name": meta["Name"], "version": meta["Version"], "license": meta.get("License-Expression") or meta.get("License", "")[:60],
                       "file": wheel.name, "sha256": sha256(wheel)})
        unzip(wheel, site)
    return wheels


def build(assets: Path, output: Path) -> dict:
    if output.exists():
        raise ValueError("Output folder already exists; choose a new one")
    if output.is_relative_to(ROOT) or assets.is_relative_to(output):
        raise ValueError("Build outside the repository and outside the assets folder")
    recorded = check_sources(assets)
    package = output / "autoskill"
    output.mkdir(parents=True)
    copy_tree(SKILL, package)
    # The package root is itself an installable DeepSeek Harness bundle: choose this folder in DSH.
    shutil.copytree(ROOT / "plugin", output / "plugin")
    shutil.copy2(ROOT / "cordis.patch.yml", output / "cordis.patch.yml")
    manifest_json = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    copy_package_docs(output, manifest_json["version"])
    manifest_json["files"] = ["plugin", "autoskill", "cordis.patch.yml", "docs", "README.md"]
    manifest_json.pop("scripts", None)
    (output / "package.json").write_text(json.dumps(manifest_json, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")

    vendor = package / "vendor" / "windows-x64"
    vendor.mkdir(parents=True)

    # Python: the official embeddable build, pointed at our site-packages only.
    unzip(assets / "python" / recorded["sources"]["python"]["file"], vendor / "python")
    site = vendor / "python" / "Lib" / "site-packages"
    site.mkdir(parents=True)
    pth = next((vendor / "python").glob("python3*._pth"))
    # The embeddable build ignores PYTHONPATH once a ._pth file exists, so the app folder is listed here,
    # relative to python.exe: vendor/windows-x64/python -> ../../../app.
    pth.write_text(f"{pth.stem}.zip\n.\nLib/site-packages\n../../../app\nimport site\n", encoding="ascii")
    wheels = _install_wheels(assets, site)
    # Chrome for Testing and its matching driver.
    unzip(assets / "chrome" / recorded["sources"]["chrome"]["file"], vendor / "chrome")
    chrome = vendor / "chrome" / "chrome-win64"
    for pak in (chrome / "locales").iterdir():
        if pak.name not in LOCALES:
            pak.unlink()
    for name in CHROME_DROP:
        target = chrome / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    unzip(assets / "chromedriver" / recorded["sources"]["chromedriver"]["file"], vendor / "chromedriver")

    install_agent_browser(assets, recorded, package, "windows-x64")

    for binary in ("python/python.exe", "chrome/chrome-win64/chrome.exe", "chromedriver/chromedriver-win64/chromedriver.exe",
                   "agent-browser/agent-browser.exe"):
        if (vendor / binary).read_bytes()[:2] != b"MZ":
            raise ValueError("Not a Windows executable: " + binary)

    smoke_test(package)
    plugin_check(output)
    files = {p.relative_to(vendor).as_posix(): sha256(p) for p in sorted(vendor.rglob("*")) if p.is_file()}
    (vendor / "runtime-manifest.json").write_text(json.dumps({"files": files}, indent=1), encoding="utf-8")
    manifest = {
        "target": "Windows 10/11 x64", "autoskill": json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"],
        "python": recorded["python_version"], "chrome_for_testing": recorded["chrome_version"],
        "chromedriver": recorded["chrome_version"], "agent_browser": recorded["agent_browser_version"],
        "wheels": wheels, "sources": recorded["sources"], "network_install": False, "telemetry": False, "auto_update": False,
        "runtime_files": len(files),
    }
    (package / "offline-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    write_package_manifest(output)
    return manifest


def zip_folder(folder: Path, target: Path) -> dict:
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for file in sorted(folder.rglob("*")):
            if file.is_file():
                archive.write(file, file.relative_to(folder).as_posix())
    return {"file": target.name, "bytes": target.stat().st_size, "sha256": sha256(target)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new folder outside the repository")
    parser.add_argument("--zip", action="store_true", help="also write autoSkill-Windows-x64-Offline.zip next to the folder")
    args = parser.parse_args()
    output = args.output.resolve()
    manifest = build(args.assets.resolve(), output)
    summary = {k: manifest[k] for k in ("autoskill", "python", "chrome_for_testing", "agent_browser", "runtime_files")}
    summary["folder_mb"] = round(sum(f.stat().st_size for f in output.rglob("*") if f.is_file()) / 1e6, 1)
    if args.zip:
        summary["zip"] = zip_folder(output, output.parent / "autoSkill-Windows-x64-Offline.zip")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
