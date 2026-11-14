"""Bridge JSON inputs preserve caller content through accepted CLI file options."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import cli, command_input, plan, projects
from autoskill.server.app import create_app


class BridgeFileInputTests(unittest.TestCase):
    def test_file_option_variants_preserve_the_selected_caller_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            environment = {"AUTOSKILL_WORKSPACE": str(folder / "workspace"), "AUTOSKILL_BRIDGE": "off",
                           "AUTOSKILL_CASE_SKILL": str(folder / "sheet"), "AUTOSKILL_SESSION": ""}
            with patch.dict(os.environ, environment):
                project_id = projects.create("Bridge input", "http://127.0.0.1:9", project_id="caller-input")["id"]
                source = folder / project_id
                (folder / "ignored.json").write_text("not selected JSON", encoding="utf-8")
                variants = (["--file", project_id], ["--file=" + project_id], ["--fi", project_id], ["--fi=" + project_id],
                            ["--file", "ignored.json", "--file=" + project_id], ["--fi=ignored.json", "--file", project_id])
                with TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787") as client:
                    for index, options in enumerate(variants):
                        with self.subTest(options=options):
                            payload = [{"id": "TC-001", "title": f"Selected payload {index}"}]
                            source.write_text(json.dumps(payload), encoding="utf-8")
                            argv = ["--input-base", str(folder), "plan", "write", project_id, *options]
                            forwarded, stdin, attachments = command_input.for_bridge(cli.parser().parse_args(argv), argv)
                            source.unlink()
                            response = client.post("/api/bridge", headers={"X-Autoskill-Bridge": "1"}, json={
                                "argv": forwarded, "stdin": stdin, "attachments": attachments, "caller_cwd": str(folder),
                                "case_skill": str(folder / "sheet")})
                            self.assertEqual(response.status_code, 200, response.text)
                            reply = response.json()
                            self.assertEqual(reply["exit"], 0, reply)
                            saved = json.loads(reply["stdout"])
                            self.assertEqual((saved["project"], saved["cases"]), (project_id, 1))
                            self.assertEqual(plan.show()["cases"][0]["title"], payload[0]["title"])


if __name__ == "__main__":
    unittest.main()
