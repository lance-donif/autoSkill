"""Complete sharing normalizes credentials and saved logins without copying machine history."""
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from autoskill import builder, paths, projects, secrets, sessions, share


class FullPackCredentialsTests(unittest.TestCase):
    def fixture(self, root, layout):
        home = root / "home"
        skill = root / "autoskill"
        (skill / "app/autoskill").mkdir(parents=True)
        (skill / "SKILL.md").write_text("Synthetic skill", encoding="utf-8")
        (skill / "app/autoskill/cli.py").write_text("", encoding="utf-8")
        workspace = {"default": home / "autoSkill-data", "external": root / "external-data", "inside": skill / "team-data"}[layout]
        environment = {"AUTOSKILL_WORKSPACE": str(workspace), "ENV_PASSWORD": "synthetic-env-password", "HOME": str(home), "USERPROFILE": str(home)}
        self.enterContext(patch.dict(os.environ, environment))
        self.enterContext(patch.object(Path, "home", return_value=home))
        if layout == "default":
            os.environ.pop("AUTOSKILL_WORKSPACE")
        self.enterContext(patch.object(paths, "SKILL", skill))
        self.enterContext(patch.object(paths, "VENDOR", skill / "vendor"))
        projects.create("合成分享项目", "http://127.0.0.1:9", project_id="account")
        case = builder.api_case("TC-API-001", "合成凭据引用", "GET", "/example", {"status": 200, "json_equals": {"/ok": True}},
                                headers={"X-Password": "${ENV:STORED_PASSWORD}", "X-Env": "${ENV:ENV_PASSWORD}"})
        projects.save_cases("account", [case])
        secrets.set_secret("STORED_PASSWORD", "synthetic-stored-password")
        secrets.set_secret("UNUSED_TOKEN", "synthetic-unused-token")
        sessions.save("account", ui={"cookies": [{"name": "session", "value": "synthetic-cookie", "domain": "127.0.0.1", "path": "/"}],
                                     "origins": []}, variables={"token": "synthetic-api-token"})
        for name in ("runs/old/screen.png", "recording.json", "recording.har", "service.json", "bridge-io/result.out", "reports/old.xlsx"):
            file = paths.local() / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("synthetic history", encoding="utf-8")
        if layout != "default":
            decoy = skill / "workspace/.local/credentials.json"
            decoy.parent.mkdir(parents=True)
            decoy.write_text('{"DECOY":"unused-default-workspace"}', encoding="utf-8")
        return skill, workspace

    def test_default_external_and_nested_workspaces_share_usable_logins(self):
        for layout in ("default", "external", "inside"):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, original_workspace = self.fixture(root, layout)
                saved = sessions.load("account")
                result = share.pack_full(str(root / "full.zip"))
                with zipfile.ZipFile(result["file"]) as archive:
                    names = archive.namelist()
                    self.assertEqual(len(names), len(set(names)))
                    self.assertEqual(result["files"], len(names))
                    self.assertIn("autoskill/workspace/projects/account/testcases/api-cases.json", names)
                    self.assertEqual({name for name in names if "/.local/" in name},
                                     {"autoskill/workspace/.local/credentials.json", "autoskill/workspace/.local/sessions/account.json"})
                    self.assertFalse(any("team-data/" in name or name.endswith((".db", ".png", ".har", ".out")) for name in names))
                    archive.extractall(root / "recipient")
                self.assertEqual(result["secrets_included"], ["ENV_PASSWORD", "STORED_PASSWORD", "UNUSED_TOKEN"])
                self.assertEqual(result["sessions_included"], ["account"])
                self.assertEqual(result["secrets_not_included"], [])
                self.assertIn("包含已保存的凭据和登录态", result["note"])
                self.assertNotIn("synthetic-", json.dumps(result))
                recipient = root / "recipient/autoskill/workspace"
                with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(recipient), "ENV_PASSWORD": "different-recipient-value"}):
                    self.assertEqual(secrets.get_secret("STORED_PASSWORD"), "synthetic-stored-password")
                    self.assertEqual(secrets.get_secret("ENV_PASSWORD"), "synthetic-env-password")
                    self.assertEqual(secrets.get_secret("UNUSED_TOKEN"), "synthetic-unused-token")
                    self.assertEqual(sessions.load("account"), saved)
                    self.assertEqual(projects.get("account")["missing_secrets"], [])
                self.assertEqual(paths.workspace(), original_workspace)

    def test_readable_legacy_stores_are_exported_as_portable_plain_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root, "external")
            (paths.local() / "credentials.json").unlink()
            (paths.local() / "secrets.json").write_text(json.dumps({"STORED_PASSWORD": "sealed-credential"}), encoding="utf-8")
            (paths.local() / "sessions/account.json").write_text(json.dumps({"sealed": "sealed-login"}), encoding="utf-8")
            saved = {"variables": {"token": "synthetic-legacy-token"}}
            unsealed = {"sealed-credential": "synthetic-legacy-password", "sealed-login": json.dumps(saved)}
            with patch.object(secrets, "unseal", side_effect=unsealed.__getitem__):
                result = share.pack_full(str(root / "full.zip"))
            with zipfile.ZipFile(result["file"]) as archive:
                credentials = json.loads(archive.read("autoskill/workspace/.local/credentials.json"))
                login = json.loads(archive.read("autoskill/workspace/.local/sessions/account.json"))
                self.assertFalse(any(name.endswith(("secrets.json", "encryption.key")) for name in archive.namelist()))
            self.assertEqual(credentials["STORED_PASSWORD"], "synthetic-legacy-password")
            self.assertEqual(login, saved)
            self.assertNotIn("synthetic-", json.dumps(result))

    def test_ordinary_pack_still_omits_credentials_and_saved_logins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root, "external")
            result = share.pack(out=str(root / "ordinary.zip"))
            with zipfile.ZipFile(result["file"]) as archive:
                self.assertFalse(any(".local" in name for name in archive.namelist()))
                self.assertFalse(any(b"synthetic-" in archive.read(name) for name in archive.namelist()))
            self.assertEqual(result["secrets_included"], [])
            self.assertEqual(result["secrets_not_included"], ["ENV_PASSWORD", "STORED_PASSWORD"])

    def test_missing_referenced_credential_is_named_without_blocking_the_pack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root, "external")
            case = projects.case("account", "TC-API-001")
            case["headers"]["X-Missing"] = "${ENV:AUTOSKILL_FIXTURE_MISSING_PASSWORD}"
            projects.save_cases("account", [case])
            with patch.dict(os.environ):
                os.environ.pop("AUTOSKILL_FIXTURE_MISSING_PASSWORD", None)
                result = share.pack_full(str(root / "full.zip"))
            self.assertEqual(result["secrets_not_included"], ["AUTOSKILL_FIXTURE_MISSING_PASSWORD"])
            self.assertIn("仍需设置缺少的凭据", result["note"])
            self.assertEqual(result["sessions_included"], ["account"])
