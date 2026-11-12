"""Prerequisites and login state: nobody has to say "log in first"."""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from autoskill import db, demo, paths, projects, runner, secrets, sessions  # noqa: E402

spec = importlib.util.spec_from_file_location("ticket_desk", ROOT / "skills/autoskill/examples/ticket-desk/server.py")
ticket_desk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ticket_desk)
PID = demo.PROJECT_ID


class RunnerTests(unittest.TestCase):
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
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("AUTOSKILL_WORKSPACE")
        os.environ["AUTOSKILL_WORKSPACE"] = self.tmp.name
        self.post("/api/test/reset")
        demo.install(self.base)

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("AUTOSKILL_WORKSPACE", None)
        else:
            os.environ["AUTOSKILL_WORKSPACE"] = self.previous
        self.tmp.cleanup()

    def post(self, path):
        request = urllib.request.Request(self.base + path, data=b"{}", method="POST")
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def run_cases(self, *case_ids, engine="playwright"):
        run = runner.create(PID, list(case_ids), engine)
        final = runner.execute(run["id"])
        rows = db.rows("SELECT * FROM results WHERE run_id = ? ORDER BY id", (run["id"],))
        for row in rows:
            row["detail"] = json.loads(row["detail"])
        return final, rows

    def test_choosing_one_ui_case_logs_in_first_then_reuses_the_login(self):
        final, rows = self.run_cases("TC-UI-002")
        self.assertEqual([(r["case_id"], r["status"], r["prerequisite"]) for r in rows],
                         [("TC-UI-001", "Pass", 1), ("TC-UI-002", "Pass", 0)])
        self.assertEqual((final["status"], final["total"], final["passed"]), ("Pass", 1, 1))  # prerequisites are not counted
        self.assertFalse(rows[0]["detail"].get("reused"))
        _, again = self.run_cases("TC-UI-003")
        self.assertTrue(again[0]["detail"].get("reused"))
        self.assertEqual(again[1]["status"], "Pass")

    def test_login_is_renewed_after_the_server_expires_the_session(self):
        self.run_cases("TC-UI-003")
        self.assertEqual(self.post("/api/test/expire-sessions")["expired"], 1)
        _, rows = self.run_cases("TC-UI-003")
        self.assertFalse(rows[0]["detail"].get("reused"))
        self.assertEqual([r["status"] for r in rows], ["Pass", "Pass"])

    def test_api_chain_passes_variables_and_reuses_the_token(self):
        _, rows = self.run_cases("TC-API-004")
        self.assertEqual([r["case_id"] for r in rows], ["TC-API-001", "TC-API-003", "TC-API-004"])
        self.assertTrue(all(r["status"] == "Pass" for r in rows))
        self.assertEqual(rows[2]["engine"], "http")
        _, again = self.run_cases("TC-API-002")
        self.assertTrue(again[0]["detail"].get("reused"))
        self.post("/api/test/expire-sessions")
        _, renewed = self.run_cases("TC-API-002")
        self.assertFalse(renewed[0]["detail"].get("reused"))
        self.assertEqual(renewed[1]["status"], "Pass")

    def test_anonymous_api_case_stays_anonymous_after_a_login_in_the_same_run(self):
        _, rows = self.run_cases("TC-UI-002", "TC-API-004", "TC-API-006")
        by_id = {r["case_id"]: r for r in rows}
        self.assertEqual(by_id["TC-UI-002"]["status"], "Pass")
        self.assertEqual(by_id["TC-API-006"]["status"], "Pass", by_id["TC-API-006"]["reason"])
        self.assertEqual(by_id["TC-API-006"]["detail"]["response"]["status"], 401)

    def test_failed_login_blocks_dependents_instead_of_failing_them(self):
        secrets.set_secret("TD_PASSWORD", "wrong-password")
        final, rows = self.run_cases("TC-API-003", "TC-API-006")
        by_id = {r["case_id"]: r for r in rows}
        self.assertEqual(by_id["TC-API-001"]["status"], "Fail")
        self.assertEqual((by_id["TC-API-003"]["status"], by_id["TC-API-003"]["category"]), ("Blocked", "prerequisite_failed"))
        self.assertEqual(by_id["TC-API-006"]["status"], "Pass")  # needs no login, still runs
        self.assertEqual((final["status"], final["passed"], final["failed"], final["blocked"]), ("Blocked", 1, 0, 1))

    def test_missing_secret_is_reported_by_name(self):
        secrets.delete_secret("TD_PASSWORD")
        os.environ.pop("TD_PASSWORD", None)
        self.assertEqual(projects.get(PID)["missing_secrets"], ["TD_PASSWORD"])
        _, rows = self.run_cases("TC-API-002")
        self.assertEqual((rows[0]["status"], rows[0]["category"]), ("Blocked", "missing_secret"))
        self.assertIn("TD_PASSWORD", rows[0]["reason"])

    def test_nothing_secret_is_stored_in_readable_form(self):
        self.run_cases("TC-UI-002", "TC-API-004")
        local = paths.local()
        token = sessions.load(PID)["variables"]["token"]
        self.assertGreater(len(token), 20)
        for file in local.rglob("*"):
            if file.is_file() and file.suffix in (".json", ".db", ".db-wal"):
                data = file.read_bytes()
                self.assertNotIn(token.encode(), data, file.name)
                self.assertNotIn(b"Passw0rd!demo", data, file.name)

    def test_dependency_cycles_and_unknown_cases_are_rejected(self):
        cases = {c["id"]: c for c in projects.cases(PID)}
        cases["TC-API-001"]["extensions"]["depends_on"] = ["TC-API-004"]
        with self.assertRaisesRegex(ValueError, "循环"):
            runner.build_plan(["TC-API-004"], cases)
        with self.assertRaisesRegex(ValueError, "不存在"):
            runner.create(PID, ["TC-NOPE-1"])


if __name__ == "__main__":
    unittest.main()
