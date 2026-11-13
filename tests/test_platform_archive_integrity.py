"""Public program bundles keep complete verified inventories and exclude user data."""
import hashlib
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("archive_publisher", ROOT / "scripts/publish-release.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)
builder_spec = importlib.util.spec_from_file_location("archive_builder", ROOT / "scripts/build-windows-bundle.py")
builder = importlib.util.module_from_spec(builder_spec)
builder_spec.loader.exec_module(builder)


class PlatformArchiveIntegrityTests(unittest.TestCase):
    def archive(self, folder, *, additional=None, corrupt=None, unlisted=None):
        files = dict.fromkeys(("cordis.patch.yml", "plugin/index.mjs", "autoskill/SKILL.md", "setup-linux.sh",
                               "autoskill/vendor/linux-x64/python/bin/python3", "autoskill/vendor/linux-x64/runtime-manifest.json"), b"fixture")
        files["package.json"] = b'{"version":"2.2.0"}'
        files["autoskill/offline-manifest.json"] = b'{"autoskill":"2.2.0","target":"linux-x64"}'
        ui = ROOT / "skills/autoskill/app/ui"
        files.update({"autoskill/app/ui/" + file.relative_to(ui).as_posix(): file.read_bytes() for file in ui.rglob("*") if file.is_file()})
        files.update(additional or {})
        inventory = {name: hashlib.sha256(value).hexdigest() for name, value in files.items() if name != unlisted}
        files["package-manifest.json"] = json.dumps({"format": 1, "version": "2.2.0", "files": inventory}).encode()
        if corrupt:
            files[corrupt] += b"modified"
        path = Path(folder) / "autoSkill-Linux-x64-Offline.zip"
        with zipfile.ZipFile(path, "w") as bundle:
            for name, value in files.items():
                bundle.writestr(name, value)
        return path

    def test_complete_inventory_is_verified_before_release(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.archive(folder)
            result = publisher.describe_archive(path, "2.2.0")
            self.assertEqual(result["bytes"], path.stat().st_size)
            self.assertEqual(result["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_modified_program_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(ValueError, "digest mismatch"):
            publisher.describe_archive(self.archive(folder, corrupt="plugin/index.mjs"), "2.2.0")

    def test_retired_updater_and_user_data_are_rejected_even_when_listed(self):
        excluded = ("autoskill-upgrade.py", "autoskill/app/autoskill/upgrade.py", "autoskill/app/autoskill/server/upgrades.py",
                    "data/secrets.json", "autoskill/data/projects/private.json", "autoskill/workspace/projects/private.json")
        for name in excluded:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(ValueError, "Incorrect package layout"):
                publisher.describe_archive(self.archive(folder, additional={name: b"fixture"}), "2.2.0")

    def test_library_data_directories_remain_valid_program_assets(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.archive(folder, additional={"autoskill/vendor/linux-x64/python/lib/package/data/fixture.json": b"{}"})
            self.assertEqual(publisher.describe_archive(path, "2.2.0")["file"], path.name)

    def test_unlisted_program_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(ValueError, "Incomplete program package inventory"):
            publisher.describe_archive(self.archive(folder, unlisted="plugin/index.mjs"), "2.2.0")

    def test_ui_from_an_old_source_is_rejected_even_with_a_matching_package_inventory(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.archive(folder)
            with zipfile.ZipFile(path) as bundle:
                files = {name: bundle.read(name) for name in bundle.namelist()}
            name = "autoskill/app/ui/css/tokens.css"
            files[name] = b"/* old UI */"
            manifest = json.loads(files["package-manifest.json"])
            manifest["files"][name] = hashlib.sha256(files[name]).hexdigest()
            files["package-manifest.json"] = json.dumps(manifest).encode()
            with zipfile.ZipFile(path, "w") as bundle:
                for key, value in files.items():
                    bundle.writestr(key, value)
            with self.assertRaisesRegex(ValueError, "UI source digest mismatch"):
                publisher.describe_archive(path, "2.2.0")

    def test_builder_manifest_contains_all_program_bytes_without_an_upgrade_helper(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            (output / "package.json").write_text(json.dumps({"version": "2.2.0", "files": ["plugin", "autoskill-upgrade.py"]}), encoding="utf-8")
            (output / "program.py").write_text("synthetic = True\n", encoding="utf-8")
            builder.write_package_manifest(output)
            config = json.loads((output / "package.json").read_text(encoding="utf-8"))
            inventory = json.loads((output / "package-manifest.json").read_text(encoding="utf-8"))["files"]
            self.assertNotIn("autoskill-upgrade.py", config["files"])
            self.assertFalse((output / "autoskill-upgrade.py").exists())
            self.assertEqual(set(inventory), {"package.json", "program.py"})
            for name, digest in inventory.items():
                self.assertEqual(digest, hashlib.sha256((output / name).read_bytes()).hexdigest())

    def test_builder_omits_skill_data_root_without_omitting_nested_assets(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source"
            for name in ("data/private.json", "workspace/private.json", ".local/private.json", "app/assets/data/public.json"):
                file = source / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("{}", encoding="utf-8")
            output = Path(folder) / "program"
            builder.copy_tree(source, output)
            self.assertFalse((output / "data").exists())
            self.assertFalse((output / "workspace").exists())
            self.assertFalse((output / ".local").exists())
            self.assertTrue((output / "app/assets/data/public.json").is_file())


if __name__ == "__main__":
    unittest.main()
