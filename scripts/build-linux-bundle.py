"""Assemble a native Linux x64/ARM64 offline bundle. OS shared libraries and DSH are prerequisites."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("windows_bundle", ROOT / "scripts/build-windows-bundle.py")
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)


def unzip(archive: Path, destination: Path):
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            if not (destination / member.filename).resolve().is_relative_to(destination.resolve()):
                raise ValueError("Unsafe archive member: " + member.filename)
        bundle.extractall(destination)
        for member in bundle.infolist():
            mode = (member.external_attr >> 16) & 0o777
            if mode:
                (destination / member.filename).chmod(mode)


def elf_check(path: Path, arch: str):
    with path.open("rb") as stream:
        header = stream.read(20)
    if header[:5] != b"\x7fELF\x02" or header[5] != 1 or int.from_bytes(header[18:20], "little") != {"x64": 62, "arm64": 183}[arch]:
        raise ValueError("Wrong ELF architecture: " + str(path))
    path.chmod(path.stat().st_mode | 0o111)


def _install_wheels(assets: Path, site: Path) -> list[dict]:
    """Install sorted wheels before assembling browser assets."""
    wheels = []
    for wheel in sorted((assets / "wheels").glob("*.whl")):
        with zipfile.ZipFile(wheel) as archive:
            meta = Parser().parsestr(archive.read(next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))).decode())
        if "telemetry" in meta["Name"].lower():
            raise ValueError("Telemetry dependency: " + meta["Name"])
        unzip(wheel, site)
        wheels.append({"name": meta["Name"], "version": meta["Version"], "file": wheel.name, "sha256": common.sha256(wheel)})
    # pip wheel .data/purelib and .data/platlib trees belong in site-packages, not beside them.
    for data in list(site.glob("*.data")):
        for kind in ("purelib", "platlib"):
            if (data / kind).is_dir():
                shutil.copytree(data / kind, site, dirs_exist_ok=True)
        shutil.rmtree(data)
    return wheels


def build(assets: Path, output: Path, arch: str) -> dict:
    if output.exists() or output.is_relative_to(ROOT) or assets.is_relative_to(output):
        raise ValueError("Choose a new output folder outside the repository and assets")
    recorded = common.check_sources(assets)
    target = "linux-" + arch
    if recorded.get("target") != target:
        raise ValueError("Assets target does not match requested architecture")
    package = output / "autoskill"
    output.mkdir(parents=True)
    common.copy_tree(ROOT / "skills/autoskill", package)
    shutil.copytree(ROOT / "plugin", output / "plugin")
    shutil.copytree(ROOT / "docs", output / "docs")
    for name in ("README.md", "cordis.patch.yml"):
        shutil.copy2(ROOT / name, output / name)
    shutil.copy2(ROOT / "scripts/setup-linux.sh", output / "setup-linux.sh")
    config = json.loads((ROOT / "package.json").read_text())
    config["files"] = ["plugin", "autoskill", "cordis.patch.yml", "setup-linux.sh", "docs", "README.md"]
    config.pop("scripts", None)
    (output / "package.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    vendor = package / "vendor" / target
    vendor.mkdir(parents=True)
    with tarfile.open(assets / "python" / recorded["sources"]["python"]["file"]) as archive:
        archive.extractall(vendor, filter="data")
    site = vendor / "python/lib/python3.12/site-packages"
    site.mkdir(parents=True, exist_ok=True)
    wheels = _install_wheels(assets, site)
    cft = "linux-arm64" if arch == "arm64" else "linux64"
    for key in ("chrome", "chromedriver"):
        unzip(assets / key / recorded["sources"][key]["file"], vendor / key)
    common.install_agent_browser(assets, recorded, package, target)
    for file in (vendor / "python/bin/python3", vendor / f"chrome/chrome-{cft}/chrome",
                 vendor / f"chromedriver/chromedriver-{cft}/chromedriver", vendor / "agent-browser/agent-browser", site / "playwright/driver/node"):
        elf_check(file, arch)
    for cache in list(vendor.rglob("__pycache__")):
        shutil.rmtree(cache)
    (package / "autoskill").chmod(0o755)
    (output / "setup-linux.sh").chmod(0o755)
    # Import-only assembly checks; no website access, browser regression or UOS acceptance claim.
    environment = {**os.environ, "PYTHONPATH": str(package / "app"), "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run([str(vendor / "python/bin/python3"), "-c", "import autoskill, playwright, selenium, cryptography, httpx, fastapi, uvicorn, openpyxl, jsonschema"],
                   check=True, env=environment, cwd=output)
    common.plugin_check(output)
    files = {p.relative_to(vendor).as_posix(): common.sha256(p) for p in sorted(vendor.rglob("*")) if p.is_file()}
    (vendor / "runtime-manifest.json").write_text(json.dumps({"files": files}, indent=1) + "\n", encoding="utf-8")
    manifest = {"target": target, "autoskill": config["version"], "python": recorded["python_version"],
                "python_distribution": "astral-sh/python-build-standalone", "chrome_for_testing": recorded["chrome_version"],
                "chromedriver": recorded["chrome_version"], "agent_browser": recorded["agent_browser_version"],
                "wheels": wheels, "sources": recorded["sources"], "network_install": False, "telemetry": False, "auto_update": False,
                "runtime_files": len(files), "system_dependencies_bundled": False, "glibc_minimum_runtime": "2.28",
                "uos_hardware_verified": False, "browser_compatibility": "Chrome requires system shared libraries; older UOS may need its own matched Chromium/driver pair"}
    (package / "offline-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    common.write_package_manifest(output)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--arch", choices=("x64", "arm64"), required=True)
    parser.add_argument("--zip", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    manifest = build(args.assets.resolve(), output, args.arch)
    result = {"target": manifest["target"], "autoskill": manifest["autoskill"], "runtime_files": manifest["runtime_files"]}
    if args.zip:
        result["zip"] = common.zip_folder(output, output.parent / f"autoSkill-Linux-{args.arch}-Offline.zip")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
