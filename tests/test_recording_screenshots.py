"""Recording screenshots and replay attachment failures on a local fixture."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from test_replay_ui import serve

from autoskill import bridge, builder, db, execution_inputs, paths, plan, projects, runner, task_context
from autoskill.recorder import agent, screenshots, session
from autoskill.replay.playwright_driver import PlaywrightDriver


class ScreenshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.root = self.folder / "data"
        self.enterContext(patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root), "AUTOSKILL_HEADED": "0", "AUTOSKILL_BRIDGE": "off",
                                                 "AUTOSKILL_BRIDGE_OUTPUT_ROOT": str(self.root)}))
        self.enterContext(task_context.use_session("截图验证"))

    def test_verified_receipt_is_local_to_the_session_and_has_no_image_payload(self):
        def capture(_name, argv):
            folder = Path(argv[argv.index("--screenshot-dir") + 1])
            file = folder / "screenshot-123.png"
            Image.new("RGB", (19, 11)).save(file)
            return {"path": str(file), "annotations": [], "image": "ignored"}

        with patch.object(agent, "call", side_effect=capture):
            first = screenshots.capture("browser", ["#flow"])
            with task_context.use_session("另一份录制"):
                second = screenshots.capture("another", [])
        self.assertNotEqual(first["reference"], second["reference"])
        for result in (first, second):
            file = self.root / result["reference"]
            self.assertEqual(file, Path(result["path"]))
            self.assertEqual((result["width"], result["height"], result["format"]), (19, 11, "png"))
            self.assertEqual(result["size"], file.stat().st_size)
            self.assertEqual(result["sha256"], hashlib.sha256(file.read_bytes()).hexdigest())
            self.assertNotIn("image", result)

    def test_bridge_checks_files_and_directory_options_before_capture(self):
        inside = self.root / "desired"
        outside = self.folder / "caller"
        for field in ([str(outside / "diagram.png")], ["#flow", str(outside / "diagram.png")], ["--screenshot-dir", str(outside)],
                      ["--screenshot-dir=" + str(outside)], ["--screenshot-dir", str(inside), "--screenshot-dir", str(outside)]):
            with self.subTest(field=field), self.assertRaises(ValueError):
                bridge.check(["browser", "screenshot", *field])
        bridge.check(["browser", "screenshot", "--full", "--threshold", "0.1", "--screenshot-dir=" + str(inside), "#flow", str(inside / "figure.png")])
        self.assertFalse(outside.exists())

    def test_unchanged_missing_unreadable_and_unexpected_images_never_claim_a_file(self):
        for reply in ({"changed": False, "revision": 2}, {}, {"path": str(self.folder / "other.png")}):
            with self.subTest(reply=reply), patch.object(agent, "call", return_value=reply), patch.object(screenshots, "_inspect") as read:
                if reply.get("changed") is False:
                    self.assertEqual(screenshots.capture("browser", ["--if-changed"]), reply)
                else:
                    with self.assertRaises(ValueError):
                        screenshots.capture("browser", [])
                read.assert_not_called()
        file = self.root / "invalid.png"
        file.write_bytes(b"previous bytes")
        with patch.object(agent, "call", return_value={"path": str(file)}), self.assertRaises(ValueError):
            screenshots.capture("browser", [str(file)])
        def broken(_name, argv):
            Path(argv[-1]).write_bytes(b"incomplete image")
            return {"path": argv[-1]}
        with patch.object(agent, "call", side_effect=broken), self.assertRaises(agent.AgentError):
            screenshots.capture("browser", [str(file)])
        self.assertEqual(file.read_bytes(), b"previous bytes")

    def test_unusable_default_directory_preserves_old_files_and_explicit_destinations(self):
        directory = self.root / ".local" / "recordings" / task_context.session_key() / "screenshots"
        directory.parent.mkdir(parents=True)
        directory.write_text("old entry", encoding="utf-8")
        with patch.object(agent, "call") as called, self.assertRaises(agent.AgentError):
            screenshots.capture("browser", [])
        called.assert_not_called()
        def write(_name, argv):
            Image.new("RGB", (10, 10)).save(argv[-1])
            return {"path": argv[-1]}
        explicit = self.root / "separate" / "output.png"
        with patch.object(agent, "call", side_effect=write):
            proof = screenshots.capture("browser", [str(explicit)])
        self.assertEqual(directory.read_text(encoding="utf-8"), "old entry")
        self.assertEqual(proof["sha256"], hashlib.sha256(explicit.read_bytes()).hexdigest())

    @unittest.skipUnless(paths.chrome() and paths.agent_browser(), "requires local Chromium and agent-browser")
    def test_running_recording_uses_the_managed_directory_and_honours_selector_and_flags(self):
        site, origin = serve()
        self.addCleanup(site.server_close)
        self.addCleanup(site.shutdown)
        project = projects.create("截图录制", origin)["id"]
        session.start(project)
        self.addCleanup(session.cancel)
        self.assertIn("todos", session.browser(["snapshot", "--all"])["snapshot"])
        self.assertTrue(session.check("text", "h1", "todos")["passed"])
        first = session.browser(["screenshot", "--if-changed", "h1"])["result"]
        self.assertTrue((self.root / first["reference"]).is_file())
        second = session.browser(["screenshot", "--if-changed", "h1"])["result"]
        self.assertFalse(second["changed"])
        self.assertNotIn("path", second)
        explicit = self.root / "explicit.jpg"
        result = session.browser(["screenshot", "--screenshot-format", "jpeg", "--screenshot-quality", "80", "h1", str(explicit)])["result"]
        self.assertEqual((result["path"], result["format"]), (str(explicit), "jpeg"))
        self.assertEqual(result["sha256"], hashlib.sha256(explicit.read_bytes()).hexdigest())
        relative = session.browser(["screenshot", "relative.png"])["result"]
        self.assertEqual(relative["reference"], "relative.png")
        self.assertEqual(Path(relative["path"]), self.root / "relative.png")

    @unittest.skipUnless(paths.chrome(), "requires local Chromium")
    def test_optional_png_write_and_first_read_failures_retain_text_verdict_and_reports(self):
        site, origin = serve()
        self.addCleanup(site.server_close)
        self.addCleanup(site.shutdown)
        project = projects.create("文本回放", origin)["id"]
        case = builder.ui_case("TC-UI-001", "标题", {"start_url": "/", "actions": [{"assertion": "text", "selector": "h1", "expected": "todos"}]})
        source = self.folder / "design.json"
        source.write_text(json.dumps([{"id": "TC-001", "title": "标题", "module": "首页", "steps": ["读取标题"], "expected": "显示 todos"}]), encoding="utf-8")
        original_open = Path.open
        def without_png(file, mode="r", *args, **kwargs):
            if file.suffix == ".png" and file.is_relative_to(self.root / ".local" / "runs") and mode == "rb":
                raise PermissionError("optional PNG")
            return original_open(file, mode, *args, **kwargs)
        for action in ("capture", "read"):
            with self.subTest(action=action), task_context.use_session(action):
                first = plan.write(project, str(source))
                self.assertEqual(first["test_report"]["status"], "ready")
                run = runner.create(project, [case["id"]], candidate_cases=[case])
                frozen = execution_inputs.load(run)["cases"][case["id"]]
                plan.bind_execution("TC-001", project, frozen, run["id"], candidate_id="candidate-local")
                fault = (patch.object(PlaywrightDriver, "screenshot", side_effect=PermissionError("optional PNG")) if action == "capture"
                         else patch.object(Path, "open", without_png))
                with fault:
                    completed = runner.execute(run["id"])
                self.assertEqual((completed["status"], completed["test_report"]["status"]), ("Pass", "ready"))
                row = db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],))
                detail = json.loads(row["detail"])
                self.assertEqual(detail["assertions"][0]["actual"], "todos")
                self.assertEqual(detail["screenshot_warnings"][0]["operation"], action)
                self.assertFalse(detail.get("screenshots"))
                self.assertTrue(execution_inputs.result_evidence(completed, row))
                plan.sync(run_id=run["id"])
                written = plan.show()
                receipt = written["test_report"]
                self.assertEqual(receipt["status"], "ready")
                for key, prefix in (("excel", ""), ("markdown", "markdown_"), ("bugs", "bugs_")):
                    self.assertEqual(hashlib.sha256(Path(receipt[key]).read_bytes()).hexdigest(), receipt[prefix + "sha256"])
                book = load_workbook(written["delivery_excel"], read_only=True)
                try:
                    sheet = book["功能测试用例"]
                    self.assertEqual(sheet.max_column, 14)
                    self.assertTrue(any(row[9] == "通过" for row in sheet.values))
                finally:
                    book.close()
