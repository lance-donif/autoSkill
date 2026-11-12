"""Maintainer-only: fetch pinned native Linux runtimes and glibc >= 2.28 wheels."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON, PYTHON_BUILD = "3.12.10", "20250529"
AGENT_BROWSER = "0.42.0"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(url: str, target: Path) -> dict:
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file():
        partial = target.with_name(target.name + ".part")
        with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as out:
            while block := response.read(1 << 20):
                out.write(block)
        partial.replace(target)
    return {"file": target.name, "url": url, "bytes": target.stat().st_size, "sha256": sha256(target)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--arch", choices=("x64", "arm64"), required=True)
    parser.add_argument("--chrome-version", default="157.0.8103.78")
    args = parser.parse_args()
    assets = args.assets.resolve()
    if assets.is_relative_to(ROOT):
        raise ValueError("Keep assets outside the repository")
    machine = "aarch64" if args.arch == "arm64" else "x86_64"
    cft = "linux-arm64" if args.arch == "arm64" else "linux64"
    python_file = f"cpython-{PYTHON}+{PYTHON_BUILD}-{machine}-unknown-linux-gnu-install_only_stripped.tar.gz"
    python_url = f"https://github.com/astral-sh/python-build-standalone/releases/download/{PYTHON_BUILD}/{python_file}"
    base = f"https://storage.googleapis.com/chrome-for-testing-public/{args.chrome_version}/{cft}"
    sources = {
        "python": fetch(python_url, assets / "python" / "python.tar.gz"),
        "chrome": fetch(base + f"/chrome-{cft}.zip", assets / "chrome" / "chrome.zip"),
        "chromedriver": fetch(base + f"/chromedriver-{cft}.zip", assets / "chromedriver" / "chromedriver.zip"),
        "agent_browser": fetch(f"https://registry.npmjs.org/agent-browser/-/agent-browser-{AGENT_BROWSER}.tgz",
                               assets / "agent-browser" / "agent-browser.tgz"),
    }
    with urllib.request.urlopen(python_url + ".sha256", timeout=60) as response:
        expected = response.read().decode().split()[0]
    if sources["python"]["sha256"] != expected:
        raise ValueError("Python archive does not match upstream SHA-256")
    wheels = assets / "wheels"
    wheels.mkdir(exist_ok=True)
    subprocess.run([sys.executable, "-m", "pip", "download", "--disable-pip-version-check", "--quiet", "--dest", str(wheels),
                    "--platform", f"manylinux_2_28_{machine}", "--platform", f"manylinux2014_{machine}",
                    "--platform", f"manylinux_2_17_{machine}", "--python-version", "3.12", "--implementation", "cp",
                    "--only-binary=:all:", "-r", str(ROOT / "requirements-linux.txt")], check=True)
    data = {"target": "linux-" + args.arch, "python_version": PYTHON, "python_build": PYTHON_BUILD,
            "chrome_version": args.chrome_version, "agent_browser_version": AGENT_BROWSER, "sources": sources,
            "wheels": [{"file": p.name, "bytes": p.stat().st_size, "sha256": sha256(p)} for p in sorted(wheels.glob("*.whl"))]}
    (assets / "download-sources.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"assets": str(assets), "target": data["target"], "wheels": len(data["wheels"])}))


if __name__ == "__main__":
    main()
