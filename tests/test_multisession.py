"""Concurrent AI tasks keep browser state, command output and case sheets independent."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "skills/autoskill/app"
sys.path.insert(0, str(APP))

from autoskill import bridge, builder, cli, locking, paths, plan, projects, task_context
from autoskill.recorder import agent, session
from autoskill.replay import codegen, ir


class SessionUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": self.tmp.name, "AUTOSKILL_SESSION": "", "AUTOSKILL_CASE_SKILL": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_explicit_context_and_cli_prefix_override_environment_and_restore_it(self):
        with patch.dict(os.environ, {"AUTOSKILL_SESSION": "environment"}):
            self.assertEqual(task_context.session_id(), "environment")
            with task_context.use_session("订单 / ../甲 👩‍💻"):
                self.assertEqual(task_context.session_id(), "订单 / ../甲 👩‍💻")
                self.assertRegex(task_context.session_key(), r"^[a-f0-9]{32}$")
                with task_context.use_session(None):
                    self.assertIsNone(task_context.session_id())
                self.assertEqual(task_context.session_id(), "订单 / ../甲 👩‍💻")
            self.assertEqual(task_context.session_id(), "environment")
            args = cli.parser().parse_args(["--session", "explicit task", "record", "status"])
            self.assertEqual((args.session, args.command, args.action), ("explicit task", "record", "status"))

    def test_recording_paths_and_browser_names_are_isolated_without_using_identifiers_as_paths(self):
        with task_context.use_session(None):
            legacy = session._file()
            legacy_name = session.session_name()
            self.assertEqual(legacy, paths.local() / "recording.json")
            self.assertEqual(paths.case_skill(), (paths.SKILL.parent / "autoskill-cases").resolve())
        locations = []
        for identity in ("../任务/甲", "../任务/乙"):
            with task_context.use_session(identity):
                session._save({"project_id": identity, "rand": "token", "steps": []})
                self.assertEqual(session.current()["project_id"], identity)
                self.assertTrue(session._file().is_relative_to(paths.local()))
                self.assertNotIn("任务", str(session._file()))
                locations.append((session._file(), session.session_name(), paths.case_skill()))
                with patch.dict(os.environ, {"AUTOSKILL_CASE_SKILL": str(self.folder / "chosen-sheet")}):
                    self.assertEqual(paths.case_skill(), self.folder / "chosen-sheet")
        self.assertTrue(all(left != right for left, right in zip(*locations)))
        self.assertTrue(all(name != legacy_name for _, name, _ in locations))
        self.assertFalse(legacy.exists())

    def test_thread_context_and_rand_are_not_shared(self):
        ready = threading.Barrier(2)

        def identify(identity):
            with task_context.use_session(identity):
                ir.set_rand(identity)
                ready.wait(timeout=5)
                return task_context.session_id(), ir.expand("value-{{RAND}}")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(identify, ("alpha", "beta")))
        self.assertEqual(results, [("alpha", "value-alpha"), ("beta", "value-beta")])
        self.assertIsNone(task_context.session_id())

    def test_generated_case_skill_explains_how_to_reopen_its_sheet_in_another_session(self):
        project = projects.create("Plan fixture", "http://127.0.0.1:9", project_id="plan-fixture")
        source = self.folder / "planned.json"
        source.write_text(json.dumps([{"title": "Planned task"}]), encoding="utf-8")
        with task_context.use_session("named plan task"):
            made = plan.write(project["id"], str(source))
        text = (Path(made["skill"]) / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("AUTOSKILL_CASE_SKILL", text)
        self.assertIn("本 `SKILL.md` 所在目录", text)
        self.assertIn("相同的 `--session` 标识", text)

    def test_parallel_agent_calls_even_in_one_session_only_delete_their_own_output(self):
        ready = threading.Barrier(2)

        def execute(command, **kwargs):
            kwargs["stdout"].write(json.dumps({"success": True, "data": {"value": command[-2]}}).encode())
            kwargs["stdout"].flush()
            ready.wait(timeout=5)
            return SimpleNamespace(returncode=0)

        with (patch.object(paths, "agent_browser", return_value="fixture-browser"),
              patch.object(agent.subprocess, "run", side_effect=execute), ThreadPoolExecutor(max_workers=2) as pool):
            results = list(pool.map(lambda value: agent.call("same browser", ["echo", value]), ("alpha", "beta")))
        self.assertEqual(results, [{"value": "alpha"}, {"value": "beta"}])
        self.assertFalse([file for file in (paths.local() / "agent-io").rglob("*") if file.is_file()])

    def test_file_locks_are_reentrant_and_distinct_names_do_not_block(self):
        with locking.project_lock("project-a"), locking.project_lock("project-a"):
            script = "from autoskill.locking import project_lock\nwith project_lock('project-b'):\n print('acquired')\n"
            done = subprocess.run([sys.executable, "-c", script], env=paths.child_env(), capture_output=True,
                                  text=True, check=False, timeout=10)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "acquired"), done.stderr)

    def test_cli_recording_dispatch_serializes_only_matching_sessions(self):
        entered = threading.Event()
        release = threading.Event()
        seen = []

        def record(args):
            seen.append(task_context.session_id())
            if args.action == "start":
                entered.set()
                self.assertTrue(release.wait(5))
            return {}, 0

        def dispatch(identity, action):
            with task_context.use_session(identity):
                return cli.dispatch(SimpleNamespace(command="record", action=action))

        with patch.object(cli, "_dispatch_recording", side_effect=record), ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(dispatch, "alpha", "start")
            self.assertTrue(entered.wait(5))
            same = pool.submit(dispatch, "alpha", "status")
            other = pool.submit(dispatch, "beta", "status")
            try:
                self.assertEqual(other.result(timeout=5), ({}, 0))
                self.assertFalse(same.done())
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5), ({}, 0))
            self.assertEqual(same.result(timeout=5), ({}, 0))
        self.assertEqual(seen, ["alpha", "beta", "alpha"])

    def test_bridge_forward_sends_the_callers_context(self):
        with task_context.use_session("任务甲"), patch.object(bridge, "_request", return_value={"exit": 0}) as request:
            bridge.forward(8787, ["record", "status"])
        self.assertEqual(request.call_args.args[2]["session_id"], "任务甲")
        self.assertEqual(request.call_args.args[2]["argv"], ["--session=任务甲", "record", "status"])

    def test_forward_preserves_identity_when_an_older_service_ignores_the_json_field(self):
        for identity, project in ((None, "default-project"), ("named task", "named-project"), ("--task", "option-like-project")):
            with task_context.use_session(identity):
                session._save({"project_id": project, "rand": "token", "steps": [], "started_at": "now", "start_url": "/"})

        def old_service(port, endpoint, body, timeout):
            return bridge.execute(body["argv"])

        with patch.object(bridge, "_request", side_effect=old_service), patch.dict(os.environ, {"AUTOSKILL_SESSION": "service-task"}):
            for identity, expected in ((None, "default-project"), ("named task", "named-project"), ("--task", "option-like-project")):
                with self.subTest(identity=identity), task_context.use_session(identity):
                    reply = bridge.forward(8787, ["record", "status"])
                    self.assertEqual(reply["exit"], 0, reply)
                    self.assertEqual(json.loads(reply["stdout"])["project"], expected)

    def test_bridge_default_does_not_inherit_the_services_session(self):
        with task_context.use_session("service-task"):
            session._save({"project_id": "service-only", "rand": "token", "steps": [], "started_at": "now", "start_url": "/"})
        with patch.dict(os.environ, {"AUTOSKILL_SESSION": "service-task"}):
            default = bridge.execute(["record", "status"])
            named = bridge.execute(["record", "status"], session_id="service-task")
        self.assertEqual(default["exit"], 0, default)
        self.assertEqual(json.loads(default["stdout"]), {"recording": False})
        self.assertEqual(json.loads(named["stdout"])["project"], "service-only")


TASK_PAGE = b'''<!doctype html><html><head><title>Concurrent task fixture</title></head><body>
<label for="value">Value</label><input id="value" data-testid="value">
<button id="save">Save</button><output id="result" data-testid="result"></output>
<script>
document.getElementById('save').onclick = async () => {
  const response = await fetch('/api/save' + location.pathname, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({value: document.getElementById('value').value})
  });
  document.getElementById('result').textContent = (await response.json()).value;
};
</script></body></html>'''


class TaskSite(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(TASK_PAGE)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"value": payload["value"], "status": "saved"}).encode())

    def log_message(self, *args):
        pass


class ConcurrentRecordingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.env = {**os.environ, "PYTHONPATH": str(APP), "AUTOSKILL_WORKSPACE": str(self.folder / "workspace"),
                    "AUTOSKILL_HEADED": "0", "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_SESSION": "", "AUTOSKILL_CASE_SKILL": ""}
        self.site = ThreadingHTTPServer(("127.0.0.1", 0), TaskSite)
        self.thread = threading.Thread(target=self.site.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_site)
        self.origin = f"http://127.0.0.1:{self.site.server_port}"
        self.identities = ("任务 / ../甲", "任务 / ../乙")
        self.service = None
        self.addCleanup(self.cleanup_processes)

    def close_site(self):
        self.site.shutdown()
        self.thread.join(timeout=5)
        self.site.server_close()

    def cleanup_processes(self):
        for identity in self.identities:
            subprocess.run([sys.executable, "-m", "autoskill", "--session", identity, "record", "cancel"],
                           env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=45)
        if self.service:
            self.service.terminate()
            try:
                self.service.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.service.kill()
                self.service.wait(timeout=5)

    def command(self, identity, *args, via_bridge=False, use_environment=False):
        env = {**self.env, "AUTOSKILL_BRIDGE": "on" if via_bridge else "off"}
        prefix = [] if identity is None or use_environment else ["--session", identity]
        env["AUTOSKILL_SESSION"] = identity or "" if use_environment else ""
        done = subprocess.run([sys.executable, "-m", "autoskill", *prefix, *args], env=env,
                              capture_output=True, text=True, encoding="utf-8", check=False, timeout=180)
        self.assertEqual(done.returncode, 0, f"{args}: {done.stdout}\n{done.stderr}")
        return json.loads(done.stdout)

    def together(self, function):
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(function, range(2)))

    def start_bridge(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        log = self.folder / "bridge-service.log"
        with log.open("wb") as output:
            self.service = subprocess.Popen([sys.executable, "-m", "autoskill", "dashboard", "--port", str(port), "--headless"],
                                            env={**self.env, "AUTOSKILL_SESSION": "service-inherited-task"},
                                            stdin=subprocess.DEVNULL, stdout=output, stderr=output)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(80):
            if self.service.poll() is not None:
                self.fail(f"Bridge service exited ({self.service.returncode}): {log.read_text(encoding='utf-8')}")
            try:
                with opener.open(f"http://127.0.0.1:{port}/api/status", timeout=1) as reply:
                    if json.loads(reply.read()).get("bridge"):
                        return
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        self.fail("Bridge service did not start: " + log.read_text(encoding="utf-8"))

    def test_bridge_case_skill_override_resolves_at_the_caller_and_empty_values_restore_isolation(self):
        configured = self.folder / "service-sheet"
        self.env["AUTOSKILL_CASE_SKILL"] = str(configured)
        self.start_bridge()
        self.env.pop("AUTOSKILL_CASE_SKILL")
        pid = self.command(None, "project", "create", "--name", "Plan task", "--url", self.origin, "--id", "plans", via_bridge=True)["created"]["id"]
        sources = []
        for index in range(2):
            source = self.folder / f"plan-{index}.json"
            source.write_text(json.dumps([{"title": f"Task {index}"}]), encoding="utf-8")
            sources.append(str(source))
        inherited = self.command(self.identities[0], "plan", "write", pid, "--file", sources[0], via_bridge=True)
        self.assertEqual(Path(inherited["skill"]), configured)

        explicit = self.folder / "caller-selected-sheet"
        self.env["AUTOSKILL_CASE_SKILL"] = os.path.relpath(explicit, Path.cwd())
        selected = self.command(self.identities[0], "plan", "write", pid, "--file", sources[0], via_bridge=True)
        self.assertEqual(Path(selected["skill"]), explicit)

        self.env["AUTOSKILL_CASE_SKILL"] = ""
        separate = self.together(lambda index: self.command(self.identities[index], "plan", "write", pid,
                                                           "--file", sources[index], via_bridge=True))
        self.assertNotEqual(separate[0]["skill"], separate[1]["skill"])
        self.assertTrue(all(Path(item["skill"]).is_relative_to(Path(self.env["AUTOSKILL_WORKSPACE"]) / ".local") for item in separate))
        for index in range(2):
            shown = self.command(self.identities[index], "plan", "show", via_bridge=True)
            self.assertEqual(shown["cases"][0]["title"], f"Task {index}")

    @unittest.skipUnless(paths.agent_browser() and paths.chrome(), "browser runtime required")
    def test_two_real_cli_tasks_record_cancel_save_and_replay_through_the_bridge(self):
        self.start_bridge()
        pid = self.command(None, "project", "create", "--name", "Concurrent tasks", "--url", self.origin,
                           "--id", "shared", via_bridge=True)["created"]["id"]

        def call(index, *args):
            return self.command(self.identities[index], *args, via_bridge=True, use_environment=index == 0)

        def start(index):
            return call(index, "record", "start", pid, "--no-login", "--url", self.origin + ("/alpha" if index == 0 else "/beta"))

        self.together(start)
        values = ("alpha-{{RAND}}", "beta-{{RAND}}")
        self.together(lambda index: call(index, "browser", "fill", "#value", values[index]))
        self.assertFalse(self.command(None, "record", "status", via_bridge=True)["recording"])
        self.assertTrue(call(0, "record", "cancel")["cancelled"])
        self.assertTrue(call(1, "record", "status")["recording"])
        self.assertIn("beta-", json.dumps(call(1, "browser", "get", "value", "#value")))
        start(0)
        call(0, "browser", "fill", "#value", values[0])

        def finish(index):
            call(index, "browser", "click", "#save")
            call(index, "check", "text", "#result", values[index])
            return call(index, "record", "stop", "--title", "Task " + str(index))

        saved = self.together(finish)
        ids = [item["ui_case"]["id"] for item in saved]
        self.assertEqual(len(set(ids)), 2)
        cases = self.command(None, "cases", pid)["cases"]
        self.assertEqual({case["id"] for case in cases if case["kind"] == "ui"}, set(ids))
        self.assertEqual(sum(case["kind"] == "api" for case in cases), 2)
        self.assertFalse(any(file.name in ("recording.json", "recording.har", "recording-state.json")
                             for file in Path(self.env["AUTOSKILL_WORKSPACE"]).rglob("*")))

        results = self.together(lambda index: call(index, "run", pid, "--case", ids[index], "--headless"))
        self.assertEqual([item["status"] for item in results], ["Pass", "Pass"], results)
        api_result = self.command(None, "run", pid, "--kind", "api", "--headless", via_bridge=True)
        self.assertEqual(api_result["status"], "Pass", api_result)

    @unittest.skipUnless(paths.chrome(), "browser runtime required")
    def test_exported_scripts_replay_rand_after_context_local_random_state(self):
        case = builder.ui_case("TC-UI-001", "Task RAND", {
            "start_url": "/alpha", "actions": [{"action": "fill", "selector": "#value", "value": "value-{{RAND}}"},
                                                {"action": "click", "selector": "#save"}],
            "assertions": [{"assertion": "text", "selector": "#result", "expected": "value-{{RAND}}"}],
        })
        ir.set_rand("must-not-be-copied")
        for engine in ("playwright", "selenium"):
            with self.subTest(engine=engine):
                if engine == "selenium" and not (paths.chrome(bundled=True) and paths.chromedriver()):
                    continue
                script = self.folder / ("standalone_" + engine + ".py")
                script.write_text(codegen.script([case], self.origin, engine), encoding="utf-8")
                env = {**self.env, "PYTHONPATH": "", "HEADED": "0", "RAND": "standalone",
                       "CHROME_BINARY": paths.chrome(bundled=engine == "selenium"), "CHROMEDRIVER": paths.chromedriver() or ""}
                result = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True,
                                        encoding="utf-8", check=False, timeout=90)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("PASS TC-UI-001", result.stdout)


if __name__ == "__main__":
    unittest.main()
