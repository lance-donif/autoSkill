"""Native npm packages and their offline guides stay pinned to the intended platform."""
import hashlib
import importlib.util
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from autoskill import paths

SPEC = importlib.util.spec_from_file_location("agent_bundle", ROOT / "scripts/build-windows-bundle.py")
bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundle)


def archive(path, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as output:
        for name, data in members.items():
            payload = data.encode() if isinstance(data, str) else data
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            output.addfile(member, io.BytesIO(payload))


class AgentPackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assets = self.root / "assets"
        self.package = self.root / "package"

    def core(self, version="0.43.0", extra=None):
        archive(self.assets / "agent-browser/core.tgz", {
            "package/package.json": json.dumps({"name": "agent-browser", "version": version}),
            "package/LICENSE": "Apache-2.0 fixture",
            "package/skill-data/core/SKILL.md": "# Current guide\n",
            "package/skill-data/core/references/commands.md": "Current commands\n",
            **(extra or {}),
        })
        return {"agent_browser_version": version, "sources": {"agent_browser": {"file": "core.tgz"}}}

    def test_split_native_archives_install_matching_binary_and_refresh_guide_hashes(self):
        for target, npm_name, filename, payload in (
                ("linux-x64", "cli-linux-x64-gnu", "agent-browser", b"\x7fELFx64"),
                ("linux-arm64", "cli-linux-arm64-gnu", "agent-browser", b"\x7fELFarm64"),
                ("windows-x64", "cli-win32-x64-msvc", "agent-browser.exe", b"MZwindows")):
            with self.subTest(target=target):
                recorded = self.core()
                archive(self.assets / "agent-browser/native.tgz", {
                    "package/package.json": json.dumps({"name": "@agent-browser/" + npm_name, "version": "0.43.0"}),
                    "package/bin/" + filename: payload,
                })
                recorded["sources"]["agent_browser_native"] = {"file": "native.tgz"}
                guide = self.package / "references/agent-browser-core.offline"
                guide.mkdir(parents=True, exist_ok=True)
                (guide / "removed-upstream.md").write_text("old guide")
                for _ in range(2):
                    bundle.install_agent_browser(self.assets, recorded, self.package, target)
                    installed = self.package / "vendor" / target / "agent-browser"
                    self.assertEqual((installed / filename).read_bytes(), payload)
                    self.assertEqual((installed / "LICENSE").read_text(), "Apache-2.0 fixture")
                    self.assertFalse((installed / "agent-browser.js").exists())
                    self.assertFalse((guide / "removed-upstream.md").exists())
                    source = json.loads((guide / "source.json").read_text())
                    self.assertEqual(source["package"], "agent-browser@0.43.0")
                    self.assertEqual(set(source["files"]), {"SKILL.md", "references/commands.md"})
                    for name, expected in source["files"].items():
                        self.assertEqual(hashlib.sha256((guide / name).read_bytes()).hexdigest(), expected)

    def test_old_combined_archives_remain_buildable(self):
        recorded = self.core("0.42.0", {"package/bin/agent-browser-linux-x64": b"\x7fELFlegacy"})
        bundle.install_agent_browser(self.assets, recorded, self.package, "linux-x64")
        self.assertEqual((self.package / "vendor/linux-x64/agent-browser/agent-browser").read_bytes(), b"\x7fELFlegacy")
        source = json.loads((self.package / "references/agent-browser-core.offline/source.json").read_text())
        self.assertEqual(source["package"], "agent-browser@0.42.0")

    def test_wrong_native_platform_or_version_cannot_be_mislabeled(self):
        for name, version in (("cli-linux-arm64-gnu", "0.43.0"), ("cli-linux-x64-gnu", "0.42.0")):
            with self.subTest(name=name, version=version):
                recorded = self.core()
                archive(self.assets / "agent-browser/native.tgz", {
                    "package/package.json": json.dumps({"name": "@agent-browser/" + name, "version": version}),
                    "package/bin/agent-browser": b"\x7fELFwrong",
                })
                recorded["sources"]["agent_browser_native"] = {"file": "native.tgz"}
                with self.assertRaisesRegex(ValueError, "platform or version"):
                    bundle.install_agent_browser(self.assets, recorded, self.package, "linux-x64")
                self.assertFalse((self.package / "vendor").exists())

    def test_native_archive_is_covered_by_the_download_manifest(self):
        sources = {}
        for name in ("python", "chrome", "chromedriver", "agent_browser", "agent_browser_native"):
            folder = "agent-browser" if name.startswith("agent_browser") else name
            path = self.assets / folder / (name + ".tgz")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
            sources[name] = {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        (self.assets / "download-sources.json").write_text(json.dumps({"sources": sources, "wheels": []}))
        self.assertEqual(bundle.check_sources(self.assets)["sources"], sources)
        (self.assets / "agent-browser/agent_browser_native.tgz").write_bytes(b"changed native archive")
        with self.assertRaisesRegex(ValueError, "changed"):
            bundle.check_sources(self.assets)

    def test_invalid_core_metadata_and_escaping_guide_do_not_write_a_package(self):
        recorded = self.core()
        recorded["agent_browser_version"] = "0.42.0"
        with self.assertRaisesRegex(ValueError, "core archive"):
            bundle.install_agent_browser(self.assets, recorded, self.package, "linux-x64")
        recorded = self.core(extra={"package/skill-data/core/../escape.md": "outside"})
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            bundle.install_agent_browser(self.assets, recorded, self.package, "linux-x64")
        self.assertFalse(self.package.exists())


class NativeDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch.object(paths, "VENDOR", self.root / "vendor"))
        self.enterContext(patch.object(paths, "runtime_target", return_value="linux-x64"))
        self.enterContext(patch("subprocess.run", side_effect=AssertionError("Path lookup must not run npm or a shim")))

    def file(self, name, payload=b"\x7fELFfixture"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.chmod(0o755)
        return path

    def resolve(self, shim):
        with patch.object(paths.shutil, "which", return_value=str(shim)):
            return paths.agent_browser()

    def test_hoisted_and_global_nested_platform_packages(self):
        for nested in (False, True):
            with self.subTest(nested=nested):
                prefix = "global" if nested else "local"
                shim = self.file(prefix + "/node_modules/agent-browser/bin/agent-browser.js", b"#!/usr/bin/env node\n")
                modules = shim.parent.parent / "node_modules" if nested else shim.parent.parent.parent
                native = modules / "@agent-browser/cli-linux-x64-gnu/bin/agent-browser"
                native.parent.mkdir(parents=True)
                native.write_bytes(b"\x7fELFnative")
                native.chmod(0o755)
                self.assertEqual(self.resolve(shim), str(native))

    def test_windows_cmd_shim_resolves_msvc_without_executing_node(self):
        shim = self.file("windows/agent-browser.cmd", b"@node agent-browser.js")
        native = self.file("windows/node_modules/@agent-browser/cli-win32-x64-msvc/bin/agent-browser.exe", b"MZfixture")
        with patch.object(paths, "runtime_target", return_value="windows-x64"):
            self.assertEqual(self.resolve(shim), str(native))

    def test_native_link_and_legacy_binary_are_both_supported(self):
        native = self.file("linked/agent-browser")
        self.assertEqual(self.resolve(native), str(native))
        shim = self.file("legacy/bin/agent-browser.js", b"#!/usr/bin/env node\n")
        legacy = self.file("legacy/bin/agent-browser-linux-x64")
        self.assertEqual(self.resolve(shim), str(legacy))

    def test_js_only_and_other_platform_packages_never_become_a_native_fallback(self):
        shim = self.file("node_modules/agent-browser/bin/agent-browser.js", b"#!/usr/bin/env node\n")
        self.file("node_modules/@agent-browser/cli-linux-arm64-gnu/bin/agent-browser")
        self.assertIsNone(self.resolve(shim))
        self.file("node_modules/@agent-browser/cli-linux-x64-gnu/bin/agent-browser", b"#!/usr/bin/env node\n")
        self.assertIsNone(self.resolve(shim))

    def test_explicit_native_override_precedes_bundled_and_npm_paths(self):
        chosen = self.file("chosen/agent-browser")
        self.file("vendor/agent-browser/agent-browser")
        with patch.dict(os.environ, {"AUTOSKILL_AGENT_BROWSER": str(chosen)}):
            self.assertEqual(self.resolve(self.root / "missing"), str(chosen))

    @unittest.skipIf(os.name == "nt", "Unix executable permission")
    def test_nonexecutable_native_package_is_not_selected(self):
        shim = self.file("node_modules/agent-browser/bin/agent-browser.js", b"#!/usr/bin/env node\n")
        native = self.file("node_modules/@agent-browser/cli-linux-x64-gnu/bin/agent-browser")
        native.chmod(0o644)
        self.assertIsNone(self.resolve(shim))
        native.chmod(0o755)
        self.assertEqual(self.resolve(shim), str(native))
