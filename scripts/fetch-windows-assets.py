"""Maintainer-only: download the Windows x64 runtime pieces from their official sources.

Run on a connected machine. End users never run this; the offline package that
build-windows-bundle.py assembles from these assets needs no network at all.
Every archive lands in its own directory and its SHA-256 is recorded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = "3.12.10"
AGENT_BROWSER = "0.42.0"
CFT_INDEX = "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True, help="new directory outside the repository")
    parser.add_argument("--chrome-version", help="Chrome for Testing version; default: current Stable")
    args = parser.parse_args()
    assets = args.assets.resolve()
    if assets.is_relative_to(ROOT):
        raise SystemExit("Keep assets outside the repository")
    assets.mkdir(parents=True, exist_ok=True)

    with urllib.request.urlopen(CFT_INDEX, timeout=60) as response:
        stable = json.load(response)["channels"]["Stable"]
    version = args.chrome_version or stable["version"]
    base = f"https://storage.googleapis.com/chrome-for-testing-public/{version}/win64"
    sources = {
        "python": fetch(
            f"https://www.python.org/ftp/python/{PYTHON}/python-{PYTHON}-embed-amd64.zip",
            assets / "python" / "python-embed.zip",
        ),
        "chrome": fetch(f"{base}/chrome-win64.zip", assets / "chrome" / "chrome.zip"),
        "chromedriver": fetch(f"{base}/chromedriver-win64.zip", assets / "chromedriver" / "chromedriver.zip"),
        "agent_browser": fetch(
            f"https://registry.npmjs.org/agent-browser/-/agent-browser-{AGENT_BROWSER}.tgz",
            assets / "agent-browser" / "agent-browser.tgz",
        ),
    }
    wheels = assets / "wheels"
    wheels.mkdir(exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "pip", "download", "--disable-pip-version-check", "--quiet",
         "--dest", str(wheels), "--platform", "win_amd64", "--python-version", "3.12",
         "--implementation", "cp", "--only-binary=:all:", "-r", str(ROOT / "requirements.txt")],
        check=True,
    )
    manifest = {
        "python_version": PYTHON,
        "chrome_version": version,
        "agent_browser_version": AGENT_BROWSER,
        "sources": sources,
        "wheels": [{"file": w.name, "bytes": w.stat().st_size, "sha256": sha256(w)} for w in sorted(wheels.glob("*.whl"))],
    }
    (assets / "download-sources.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    total = sum(s["bytes"] for s in sources.values()) + sum(w["bytes"] for w in manifest["wheels"])
    print(json.dumps({"assets": str(assets), "chrome": version, "wheels": len(manifest["wheels"]), "total_mb": round(total / 1e6, 1)}))


if __name__ == "__main__":
    main()
