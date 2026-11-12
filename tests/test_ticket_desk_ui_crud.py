"""The product demo's real UI closes CRUD through recording and all replay paths."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import db, paths, projects, runner, secrets, sessions  # noqa: E402
from autoskill.recorder import session  # noqa: E402
from autoskill.replay import codegen, ir  # noqa: E402

SPEC = importlib.util.spec_from_file_location("ticket_desk_ui_crud_site", ROOT / "skills/autoskill/examples/ticket-desk/server.py")
ticket_desk = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ticket_desk)

PASSWORD_NAME = "TD_UI_CRUD_PASSWORD"
PASSWORD = "Passw0rd!demo"  # published synthetic demo account, never real user data


@unittest.skipUnless(paths.agent_browser() and paths.chrome() and paths.chromedriver(), "complete browser runtime required")
class TicketDeskUICrudTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ticket_desk.make_server(0)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env = mock.patch.dict(os.environ, {
            "AUTOSKILL_WORKSPACE": self.tmp.name, "AUTOSKILL_HEADED": "0", "AUTOSKILL_BRIDGE": "off",
            "AUTOSKILL_CASE_SKILL": str(Path(self.tmp.name) / "unused-autoskill-cases"),
            "NO_PROXY": "localhost,127.0.0.1,::1", "no_proxy": "localhost,127.0.0.1,::1",
        })
        self.env.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.env.stop)
        self.addCleanup(session.cancel)
        self.state = self.server.RequestHandlerClass.state
        self.state.reset()
        self.requests = []
        handler = self.server.RequestHandlerClass
        original_get = handler.do_GET

        def traced_get(request):
            entry = {"path": request.path, "started": time.monotonic()}
            self.requests.append(entry)
            try:
                return original_get(request)
            finally:
                entry["finished"] = time.monotonic()

        trace = mock.patch.object(handler, "do_GET", traced_get)
        trace.start()
        self.addCleanup(trace.stop)
        db.init()
        self.pid = projects.create("Ticket Desk UI CRUD", self.base, project_id="ticket-desk-ui-crud")["id"]
        projects.update(self.pid, timeout_seconds=10, page_load_seconds=20, assertion_timeout_seconds=10)
        secrets.set_secret(PASSWORD_NAME, PASSWORD)

    def tickets(self):
        with self.state.lock:
            return [dict(ticket) for ticket in self.state.tickets]

    def assert_seed_restored(self):
        actual = self.tickets()
        self.assertEqual(len(actual), 3, actual)
        self.assertEqual({(t["title"], t["priority"], t["status"]) for t in actual}, set(ticket_desk.SEED))
        self.assertEqual({t["id"] for t in actual}, {"T-1001", "T-1002", "T-1003"})

    def prepare_independent_run(self):
        # An earlier failed path must not cause later engines to fail only
        # because its test ticket remains. Use fresh ids so the test still
        # detects locators tied to the recorded auto-incremented ticket id.
        with self.state.lock:
            next_id = self.state.next_id + 10
        self.state.reset()
        self.requests.clear()
        with self.state.lock:
            self.state.next_id = next_id
        sessions.clear(self.pid)
        self.assert_seed_restored()

    def failure_evidence(self, case, **details):
        folder = Path(tempfile.mkdtemp(prefix="autoskill-ticket-desk-ui-crud-failure-"))
        (folder / "failure.json").write_text(json.dumps({"case": case, "tickets": self.tickets(),
                                                        "requests": self.requests, **details},
                                                        ensure_ascii=False, indent=2), encoding="utf-8")
        return str(folder / "failure.json")

    def browser(self, *argv):
        result = session.browser(list(argv))
        self.assertIsNot(result.get("replayable"), False, result)
        return result

    def check(self, kind, target, expected=None):
        return session.check(kind, target, expected, timeout_seconds=10)

    def record_login(self):
        session.start(self.pid, as_login=True)
        self.browser("fill", "#username", "qa_admin")
        self.browser("fill", "#password", "secret:" + PASSWORD_NAME)
        self.browser("click", "[data-testid=login-submit]")
        self.check("text", "#current-user", "测试管理员")
        made = session.stop("管理员登录", "登录")
        self.assertEqual(projects.auth(self.pid)["ui_case"], made["ui_case"]["id"])
        return made["ui_case"]["id"]

    def record_crud(self):
        initial, changed = "闭环新建-{{RAND}}", "闭环更新-{{RAND}}"
        session.start(self.pid)
        self.check("text", "#current-user", "测试管理员")
        self.check("count", "[data-testid=ticket-row]", "3")
        self.browser("click", "#new-ticket")
        self.browser("fill", "#title", initial)
        self.browser("select", "#priority", "high")
        self.browser("fill", "#description", "合成的 CRUD 回归描述")
        self.browser("click", "[data-testid=ticket-save]")
        self.check("contains", "#ticket-rows", initial)
        self.assertEqual(len(self.tickets()), 4)

        self.browser("fill", "#search", initial)
        self.check("value", "#search", initial)
        self.check("count", "[data-testid=ticket-row]", "1")
        self.browser("click", "[data-testid=ticket-edit]")
        self.check("value", "#title", initial)
        self.browser("fill", "#title", changed)
        self.browser("select", "#priority", "low")
        self.browser("select", "#ticket-status", "resolved")
        self.browser("fill", "#description", "已编辑并解决的合成工单")
        self.browser("click", "[data-testid=ticket-save]")
        self.browser("fill", "#search", changed)
        self.check("value", "#search", changed)
        self.browser("select", "#status-filter", "resolved")
        self.check("contains", "#ticket-rows", changed)
        self.check("count", "[data-testid=ticket-row]", "1")
        self.check("text", "[data-testid=ticket-row] td:nth-child(3)", "低")
        self.check("text", "[data-testid=ticket-row] .badge", "已解决")

        # Both choices are prepared before the same real confirm-triggering
        # action, so recording/replay cannot silently accept the cancellation.
        self.browser("dialog", "dismiss")
        self.browser("click", "[data-testid=ticket-delete]")
        self.check("count", "[data-testid=ticket-row]", "1")
        self.check("contains", "#ticket-rows", changed)
        self.assertEqual(len(self.tickets()), 4, "cancel must leave the created ticket intact")

        self.browser("dialog", "accept")
        self.browser("click", "[data-testid=ticket-delete]")
        self.check("count", "[data-testid=ticket-row]", "0")
        self.check("visible", "#empty")
        self.browser("fill", "#search", "")
        self.check("value", "#search", "")
        self.browser("select", "#status-filter", "")
        self.check("count", "[data-testid=ticket-row]", "3")
        self.check("text", "#ticket-total", "共 3 条")
        self.assert_seed_restored()
        made = session.stop("工单完整增删改查与取消删除", "工单")
        case = projects.case(self.pid, made["ui_case"]["id"])
        self.assertTrue(ir.is_replayable(case), case)
        dialogs = [step["mode"] for step in case["extensions"]["automation"]["actions"] if step.get("action") == "dialog"]
        self.assertEqual(dialogs, ["dismiss", "accept"])
        self.assertNotIn(PASSWORD, json.dumps(case, ensure_ascii=False))
        return case

    def test_recorded_product_crud_replays_with_both_engines_and_standalone_exports(self):
        login_id = self.record_login()
        case = self.record_crud()
        self.assertIn(login_id, projects.depends_on(case))
        for engine in ("playwright", "selenium"):
            with self.subTest(path="runner", engine=engine):
                self.prepare_independent_run()
                run = runner.create(self.pid, [case["id"]], engine)
                result = runner.execute(run["id"])
                verdicts = db.rows("SELECT case_id, status, reason, actual, detail FROM results WHERE run_id = ?", (run["id"],))
                evidence = self.failure_evidence(case, run=result, results=verdicts) if result["status"] != "Pass" else None
                self.assertEqual(result["status"], "Pass", {"run": result,
                                 "results": [{key: value for key, value in row.items() if key != "detail"} for row in verdicts],
                                 "evidence": evidence})
                self.assertEqual({row["case_id"] for row in verdicts}, {login_id, case["id"]})
                self.assertTrue(all(row["status"] == "Pass" for row in verdicts), verdicts)
                self.assert_seed_restored()

        exported = codegen.export(self.pid, str(Path(self.tmp.name) / "standalone"))
        self.assertFalse(exported["notes"], exported)
        for engine in ("playwright", "selenium"):
            with self.subTest(path="standalone", engine=engine):
                self.prepare_independent_run()
                name = case["id"].lower() + "_" + engine + ".py"
                self.assertIn(name, exported["scripts"])
                script = Path(exported["folder"]) / name
                source = script.read_text(encoding="utf-8")
                self.assertNotIn("from autoskill", source)
                self.assertNotIn("import autoskill", source)
                self.assertNotIn(PASSWORD, source)
                env = {**paths.child_env(), "CHROME_BINARY": paths.chrome(), "CHROMEDRIVER": paths.chromedriver(),
                       "TIMEOUT": "10", "ASSERTION_TIMEOUT": "10", "HEADED": "0", PASSWORD_NAME: PASSWORD}
                done = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=120)
                evidence = self.failure_evidence(case, engine=engine, stdout=done.stdout, stderr=done.stderr) if done.returncode else None
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr + ("\n" + evidence if evidence else ""))
                self.assert_seed_restored()


if __name__ == "__main__":
    unittest.main()
