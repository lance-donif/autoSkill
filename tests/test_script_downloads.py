"""Dashboard script downloads preserve prerequisites, budgets and required assets."""
import ast
import os
from pathlib import Path
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from fastapi.testclient import TestClient  # noqa: E402
from autoskill import builder, projects  # noqa: E402
from autoskill.replay import codegen  # noqa: E402
from autoskill.server.app import create_app  # noqa: E402


class ScriptDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.temporary.name, "AUTOSKILL_BRIDGE": "off"})
        self.environment.start()
        projects.create("Downloads", "http://127.0.0.1:1", project_id="downloads")
        self.case = builder.ui_case("TC-UI", "UI flow", {"start_url": "/", "actions": [], "assertions": []})
        self.client = TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787")
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.environment.stop()
        self.temporary.cleanup()

    def download(self, engine="playwright"):
        return self.client.get("/api/projects/downloads/cases/TC-UI/script", params={"engine": engine})

    def test_pure_ui_chain_downloads_both_engines_with_project_budgets(self):
        login = builder.ui_case("TC-LOGIN", "UI prerequisite", {"start_url": "/login"})
        self.case["extensions"]["depends_on"] = [login["id"]]
        projects.save_cases("downloads", [login, self.case])
        projects.update("downloads", timeout_seconds=41, assertion_timeout_seconds=720)
        for engine in ("playwright", "selenium"):
            with self.subTest(engine=engine):
                response = self.download(engine)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn("tc-ui_" + engine + ".py", response.headers["content-disposition"])
                tree = ast.parse(response.text)
                functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("case_")]
                self.assertEqual(len(functions), 2)
                # Load just the generated configuration before optional engine
                # imports, without needing those packages or launching Chrome.
                configuration = []
                for node in tree.body:
                    if isinstance(node, ast.ImportFrom) and node.module.split(".")[0] in ("playwright", "selenium"):
                        break
                    configuration.append(node)
                namespace = {"__name__": "downloaded_standalone", "__file__": str(Path(self.temporary.name) / "script.py")}
                with patch.dict(os.environ):
                    os.environ.pop("TIMEOUT", None)
                    os.environ.pop("ASSERTION_TIMEOUT", None)
                    exec(compile(ast.Module(body=configuration, type_ignores=[]), "downloaded_configuration", "exec"), namespace)
                self.assertEqual(namespace["TIMEOUT"], 41)
                self.assertEqual(namespace["ASSERTION_TIMEOUT"], 720)

    def test_api_prerequisite_is_not_omitted_from_single_script_download(self):
        prerequisite = builder.api_case("TC-API-PREREQUISITE", "Create required data", "POST", "/items",
                                        {"status": 201, "json_equals": {"/created": True}}, json={"name": "Synthetic"})
        self.case["extensions"]["depends_on"] = [prerequisite["id"]]
        projects.save_cases("downloads", [prerequisite, self.case])
        response = self.download()
        self.assertEqual(response.status_code, 400)
        self.assertIn(prerequisite["id"], response.json()["detail"])
        self.assertIn("不能省略", response.json()["detail"])

    def test_native_prerequisite_blocks_single_script_download(self):
        prerequisite = builder.ui_case("TC-NATIVE", "Native prerequisite", {"start_url": "/", "actions": [
            {"action": "native_step", "command": "state", "args": ["load", "state.json"], "reason": "Unsupported state import"}]})
        self.case["extensions"]["depends_on"] = [prerequisite["id"]]
        projects.save_cases("downloads", [prerequisite, self.case])
        response = self.download()
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported state import", response.json()["detail"])

    def test_upload_script_download_directs_to_complete_project_export(self):
        self.case["extensions"]["automation"]["actions"] = [
            {"action": "upload", "selector": "#input", "files": ["files/uploads/input.txt"]}]
        projects.save_cases("downloads", [self.case])
        asset = projects.path("downloads") / "files/uploads/input.txt"
        asset.parent.mkdir(parents=True)
        asset.write_text("Synthetic upload")
        response = self.download()
        self.assertEqual(response.status_code, 400)
        self.assertIn("autoskill export scripts downloads", response.json()["detail"])
        self.assertIn("assets/", response.json()["detail"])
        complete = codegen.export("downloads")
        self.assertEqual(len(complete["scripts"]), 2, complete)
        self.assertTrue((Path(complete["folder"]) / "assets/files/uploads/input.txt").is_file())

    def test_voice_script_download_does_not_omit_fake_microphone_asset(self):
        projects.save_cases("downloads", [self.case])
        asset = projects.path("downloads") / "files/audio/input.wav"
        asset.parent.mkdir(parents=True)
        with wave.open(str(asset), "wb") as stream:
            stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            stream.writeframes(b"\x00\x00" * 800)
        projects.update("downloads", media={"audio_input_file": "files/audio/input.wav"})
        response = self.download("selenium")
        self.assertEqual(response.status_code, 400)
        self.assertIn("测试麦克风", response.json()["detail"])
        self.assertIn("assets/", response.json()["detail"])
        complete = codegen.export("downloads")
        self.assertEqual(len(complete["scripts"]), 2, complete)
        self.assertEqual((Path(complete["folder"]) / "assets/files/audio/input.wav").read_bytes(), asset.read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
