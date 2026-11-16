"""Data roots survive program replacement while browser control remains task-local."""
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import maintenance, paths, platforms, runtime_identity, task_context
from autoskill.recorder import agent, capture, session


class DataPathTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.enterContext(patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "data")}, clear=True))

    def binary(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture: not executed", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_default_root_is_installation_independent_and_resolution_never_writes(self):
        for system, expected in (("linux", self.root / "home"), ("win32", self.root / "windows-user")):
            with self.subTest(system=system), patch.object(paths.sys, "platform", system), \
                    patch.object(Path, "home", return_value=self.root / "home"), \
                    patch.dict(os.environ, {"USERPROFILE": str(self.root / "windows-user")}, clear=True), \
                    patch.object(paths, "local", side_effect=AssertionError("pure path resolution must not initialize")), \
                    task_context.use_session(None):
                for install in ("program-v1", "replacement/program-v2"):
                    with patch.object(paths, "SKILL", self.root / install):
                        self.assertEqual(paths.workspace(), expected / "autoSkill-data")
                        self.assertEqual(paths.default_workspace(), expected / "autoSkill-data")
                        self.assertEqual(paths.case_skill(), paths.workspace() / ".local/case-plans/default/autoskill-cases")
                        self.assertEqual(paths.browser_control(), paths.workspace() / ".local/browser-control")
                        self.assertEqual(paths.runtime_settings_file().parent, paths.workspace() / ".local/runtime-settings")
                        self.assertFalse(paths.workspace().exists())

    def test_explicit_workspace_and_case_sheet_remain_authoritative_without_creation(self):
        chosen = self.root / "chosen-data"
        external = self.root / "external-sheet"
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(chosen)}), task_context.use_session("a/../b"):
            self.assertEqual(paths.workspace(), chosen)
            self.assertEqual(paths.case_skill(), chosen / ".local/case-plans" / task_context.session_key() / "autoskill-cases")
            with patch.dict(os.environ, {"AUTOSKILL_CASE_SKILL": str(external)}):
                self.assertEqual(paths.case_skill(), external)
            self.assertFalse(chosen.exists())
            self.assertFalse(external.exists())

    def test_explicit_case_sheet_mapping_preserves_the_original_and_rejects_bad_mappings(self):
        root = paths.workspace()
        root.mkdir()
        external = self.root / "old-sheet"
        external.mkdir()
        (external / "cases.xlsx").write_bytes(b"original workbook")
        location = ".local/case-plans/imported/fixture/autoskill-cases"
        manifest = {"schema_version": 1, "path_mappings": [{"source": str(external), "destination": location}]}
        file = root / "data-root.json"
        file.write_text(json.dumps(manifest), encoding="utf-8")
        with patch.dict(os.environ, {"AUTOSKILL_CASE_SKILL": str(external)}):
            self.assertEqual(paths.case_skill(), root / location)
            self.assertEqual((external / "cases.xlsx").read_bytes(), b"original workbook")
            manifest["path_mappings"][0]["destination"] = "../escape"
            file.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "数据根"):
                paths.case_skill()

    def test_unmapped_relative_case_sheet_keeps_the_callers_directory(self):
        with patch.dict(os.environ, {"AUTOSKILL_CASE_SKILL": "relative-sheet"}), patch("os.getcwd", return_value=str(self.root)):
            self.assertEqual(paths.case_skill(), self.root / "relative-sheet")

    def test_legacy_runtime_read_is_pure_and_configuration_writes_only_data_root(self):
        vendor = self.root / "program/vendor"
        browser = self.binary(vendor / "chrome/chrome-linux64/chrome")
        driver = self.binary(vendor / "chromedriver/chromedriver-linux64/chromedriver")
        legacy = vendor / "runtime-settings.json"
        legacy.write_text(json.dumps({"chrome": "chrome/chrome-linux64/chrome", "chromedriver": str(driver),
                                      "browser_selection": "explicit"}), encoding="utf-8")
        original = legacy.read_bytes()
        with patch.object(paths, "VENDOR", vendor), patch.object(paths, "runtime_target", return_value="linux-x64"), \
                patch.object(platforms, "runtime_problems", return_value=[]), \
                patch.object(platforms, "browser_pair", return_value={"ok": True}):
            self.assertEqual(paths.chrome(), str(browser))
            self.assertFalse(paths.workspace().exists())
            result = platforms.configure()
            self.assertTrue(result["ok"])
            self.assertEqual(result["settings"], str(paths.runtime_settings_file()))
            self.assertEqual(json.loads(paths.runtime_settings_file().read_text())["chrome"], "chrome/chrome-linux64/chrome")
            self.assertEqual(legacy.read_bytes(), original)

    def test_relative_runtime_paths_follow_the_replacement_program_not_the_data_root(self):
        settings = paths.runtime_settings_file()
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"chrome": "chrome/chrome-linux64/chrome", "browser_selection": "explicit"}), encoding="utf-8")
        for install in ("first", "replacement"):
            vendor = self.root / install / "vendor"
            browser = self.binary(vendor / "chrome/chrome-linux64/chrome")
            wrong = self.binary(vendor / "wrong-browser")
            (vendor / "runtime-settings.json").write_text(json.dumps({"chrome": str(wrong)}), encoding="utf-8")
            with patch.object(paths, "VENDOR", vendor):
                self.assertEqual(paths.chrome(), str(browser))

    def test_settings_merge_nested_preferences_and_do_not_lose_a_concurrent_writer(self):
        paths.save_settings(browser="bundled", dashboard={"theme": "dark", "replay": {"engine": "selenium", "headed": True}})
        first_read, release, second_entered, second_read = (threading.Event() for _ in range(4))
        original = paths.settings

        def read():
            value = original()
            if not first_read.is_set():
                first_read.set()
                self.assertTrue(release.wait(5))
            else:
                second_read.set()
            return value

        def second():
            second_entered.set()
            return paths.save_settings(dashboard={"replay": {"headed": False}}, dashboard_legacy_imported=True)

        with patch.object(paths, "settings", side_effect=read), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(paths.save_settings, browser="system", dashboard={"theme": "light"})
            self.assertTrue(first_read.wait(5))
            other = pool.submit(second)
            try:
                self.assertTrue(second_entered.wait(5))
                self.assertFalse(second_read.wait(0.1))
            finally:
                release.set()
            first.result(timeout=5)
            other.result(timeout=5)
        self.assertEqual(paths.settings(), {"browser": "system", "dashboard": {"theme": "light", "replay": {"engine": "selenium", "headed": False}},
                                           "dashboard_legacy_imported": True})
        self.assertFalse((paths.local() / "settings.json.tmp").exists())

    @unittest.skipUnless(os.name != "nt", "Unix export gate")
    def test_runtime_configuration_cannot_write_through_an_exclusive_data_snapshot(self):
        import fcntl

        file = paths.runtime_settings_file()
        file.parent.mkdir(parents=True)
        file.write_text('{"browser_selection":"detected"}\n', encoding="utf-8")
        before = file.read_bytes()
        with patch.object(maintenance, "gate_path", return_value=self.root / "snapshot.lock"), \
                patch.object(paths, "chrome", return_value="/browser"), patch.object(paths, "chromedriver", return_value="/driver"), \
                patch.object(platforms, "runtime_problems", return_value=[]), \
                patch.object(platforms, "browser_pair", return_value={"ok": True}), \
                (self.root / "snapshot.lock").open("a+b") as gate:
            fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with self.assertRaises(ValueError):
                    platforms.configure()
                self.assertEqual(file.read_bytes(), before)
            finally:
                fcntl.flock(gate, fcntl.LOCK_UN)
            self.assertTrue(platforms.configure()["ok"])
            self.assertEqual(json.loads(file.read_text())["chrome"], "/browser")

    @unittest.skipUnless(os.name != "nt", "Unix export gate")
    def test_preferences_cannot_write_through_an_exclusive_data_snapshot(self):
        import fcntl

        paths.save_settings(browser="bundled", dashboard={"theme": "dark"})
        file = paths.local() / "settings.json"
        before = file.read_bytes()
        with patch.object(maintenance, "gate_path", return_value=self.root / "snapshot.lock"), \
                (self.root / "snapshot.lock").open("a+b") as gate:
            fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with self.assertRaises(ValueError):
                    paths.save_settings(dashboard={"theme": "light"})
                self.assertEqual(file.read_bytes(), before)
            finally:
                fcntl.flock(gate, fcntl.LOCK_UN)
            paths.save_settings(dashboard={"theme": "light"})
        self.assertEqual(paths.settings(), {"browser": "bundled", "dashboard": {"theme": "light"}})

    def test_platform_settings_are_separate_and_windows_selection_is_read(self):
        vendor = self.root / "vendor"
        browser = self.binary(vendor / "chrome/chrome-win64/chrome.exe")
        with patch.object(paths.sys, "platform", "win32"), patch.object(paths, "VENDOR", vendor), \
                patch.object(paths, "runtime_target", return_value="windows-x64"):
            file = paths.runtime_settings_file()
            file.parent.mkdir(parents=True)
            file.write_text(json.dumps({"chrome": "chrome/chrome-win64/chrome.exe", "browser_selection": "explicit"}), encoding="utf-8")
            self.assertEqual(file.name, "windows-x64.json")
            self.assertEqual(paths.chrome(), str(browser))
        with patch.object(paths, "runtime_target", return_value="linux-arm64"):
            self.assertEqual(paths.runtime_settings_file().name, "linux-arm64.json")
            self.assertFalse(paths.runtime_settings_file().exists())

    def test_agent_uses_the_data_controller_and_drops_inherited_browser_overrides(self):
        seen = {}

        def execute(_command, **options):
            seen.update(options["env"])
            options["stdout"].write(b'{"success":true,"data":{"observed":true}}')
            return SimpleNamespace(returncode=0)

        with patch.dict(os.environ, {"AGENT_BROWSER_SOCKET_DIR": str(self.root / "unrelated"),
                                    "AGENT_BROWSER_PROFILE": "unrelated-profile"}), \
                patch.object(paths, "agent_browser", return_value="fixture-browser"), \
                patch.object(agent.subprocess, "run", side_effect=execute):
            self.assertEqual(agent.call("isolated", ["get", "url"]), {"observed": True})
        self.assertEqual(Path(seen["AGENT_BROWSER_SOCKET_DIR"]).resolve(), paths.browser_control())
        self.assertNotIn("AGENT_BROWSER_PROFILE", seen)
        self.assertFalse((self.root / "unrelated").exists())

    def test_copied_controller_pid_is_retained_without_signalling_or_reusing_it(self):
        folder = paths.browser_control()
        folder.mkdir(parents=True)
        (folder / "copied.pid").write_text(str(os.getpid()), encoding="ascii")
        owner = {**runtime_identity.current(), "location_id": "another-data-location"}
        (folder / "copied.owner.json").write_text(json.dumps(owner), encoding="utf-8")
        token = agent._prepare_controller("copied")
        self.assertIsNotNone(token)
        self.assertFalse((folder / "copied.pid").exists())
        retained = list((paths.local() / "relocations").glob("*/runtime/browser-control/copied.pid"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_text(), str(os.getpid()))
        self.assertTrue(runtime_identity.owns(os.getpid(), runtime_identity.current()))
        self.assertIsNone(agent._prepare_controller("copied"))  # A concurrent launch sees the live starter receipt.
        agent._remember_controller("copied", token)

    def test_detached_capture_returns_saved_evidence_without_writing_control_or_deleting_it(self):
        folder = paths.workspace() / ".local/network-finished"
        folder.mkdir(parents=True)
        saved = {"token": "old-token", "source": "cdp_network", "entries": [{"observed": True}], "available": True}
        (folder / "capture.json").write_text(json.dumps(saved), encoding="utf-8")
        handle = {"folder": ".local/network-finished", "pid": os.getpid(), "token": "old-token",
                  "owner": {**runtime_identity.current(), "location_id": "old-location"}}
        child = SimpleNamespace(wait=lambda **_options: self.fail("a relocated handle must not wait on an old process"))
        with patch.dict(capture._children, {os.getpid(): (child, "old-token")}):
            result = capture.stop(handle)
        self.assertEqual(result["entries"], saved["entries"])
        self.assertEqual(result["runtime_state"], "detached")
        self.assertFalse((folder / "control.json").exists())
        capture.discard(handle, folder.parent)
        self.assertEqual(json.loads((folder / "capture.json").read_text()), saved)

    def test_detached_unfinished_capture_keeps_partial_bytes_and_does_not_send_stop(self):
        folder = paths.workspace() / ".local/network-partial"
        folder.mkdir(parents=True)
        (folder / "events.jsonl").write_bytes(b'{"observation":"partial"}\n')
        handle = {"folder": ".local/network-partial", "pid": os.getpid(), "token": "old-token"}
        result = capture.stop(handle)
        self.assertFalse(result["available"])
        self.assertEqual(result["entries"], [])
        self.assertIn("未控制任何进程", result["error"])
        self.assertEqual(Path(result["original_events_path"]), folder / "events.jsonl")
        self.assertEqual((folder / "events.jsonl").read_bytes(), b'{"observation":"partial"}\n')
        self.assertFalse((folder / "control.json").exists())

    def test_browser_restriction_checks_the_selected_data_root_not_user_home(self):
        home = self.root / "blocked-home"
        home.write_text("not a directory", encoding="utf-8")
        with patch.dict(os.environ, {"USERPROFILE": str(home)}):
            self.assertEqual(paths.restricted(), "")
        paths.browser_control().rmdir()
        paths.browser_control().write_text("not a directory", encoding="utf-8")
        self.assertIn("浏览器控制目录不可写", paths.restricted())

    @unittest.skipUnless(os.name != "nt", "Unix socket aliases")
    def test_short_socket_alias_never_replaces_or_follows_an_unrelated_path(self):
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / ("long" * 40))}):
            target = self.root / "unrelated"
            target.mkdir()
            marker = target / "keep.txt"
            marker.write_text("unchanged", encoding="utf-8")
            temporary = Path(tempfile.gettempdir())
            if len(os.fsencode(str(temporary))) > 25:
                temporary = Path("/tmp")
            aliases = temporary / ("asb-" + str(os.getuid()))
            aliases.mkdir(mode=0o700, exist_ok=True)
            alias = aliases / hashlib.sha256(os.fsencode(str(paths.browser_control()))).hexdigest()[:20]
            alias.symlink_to(target, target_is_directory=True)
            try:
                with self.assertRaisesRegex(agent.AgentError, "不一致"):
                    agent._control_directory("fixture")
                self.assertEqual(alias.resolve(), target)
                self.assertEqual(marker.read_text(), "unchanged")
            finally:
                alias.unlink()


@unittest.skipUnless(sys.platform == "linux" and paths.agent_browser() and paths.chrome(), "requires the local Linux recorder runtime")
class DataControlBrowserTests(unittest.TestCase):
    def test_real_agent_browser_keeps_short_and_long_root_state_inside_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            names = []
            for name in ("short", "long-" + "directory" * 16):
                with self.subTest(name=name[:5]), patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(root / name),
                                                                       "AUTOSKILL_HEADED": "0"}), \
                        task_context.use_session("control-" + uuid.uuid4().hex):
                    identity = session.session_name()
                    names.append(identity)
                    global_file = Path.home() / ".agent-browser" / (identity + ".target")
                    self.assertFalse(global_file.exists())
                    try:
                        result = agent.call(identity, ["open", "about:blank"], launch=agent.launch_args([]), timeout=45)
                        self.assertEqual(result["url"], "about:blank")
                        for suffix in (".sock", ".pid", ".target", ".config"):
                            self.assertTrue((paths.browser_control() / (identity + suffix)).exists(), suffix)
                        pid = int((paths.browser_control() / (identity + ".pid")).read_text())
                        owner = json.loads((paths.browser_control() / (identity + ".owner.json")).read_text())
                        self.assertNotEqual(pid, os.getpid())
                        self.assertTrue(runtime_identity.owns(pid, owner))
                        self.assertEqual(agent._control_directory(identity).resolve(), paths.browser_control())
                    finally:
                        closed = agent.call(identity, ["close"], timeout=30)
                    self.assertTrue(closed["closed"])
                    self.assertFalse(global_file.exists())
                    self.assertFalse((paths.browser_control() / (identity + ".pid")).exists())
            self.assertEqual(len(set(names)), 2)
