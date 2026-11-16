"""Inspect host-created dialogue sources and real stateful multi-turn execution evidence."""
import contextlib
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dashboard_revision_fixture import change_project_and_wait_for_ui
from playwright.sync_api import expect
from test_load_dashboard import DashboardWorkspace

from autoskill import projects


class ConversationTarget(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        question = body["question"]
        state = self.server.fixture
        with state["lock"]:
            if question == "全年销售额？":
                conversation_id = "server-" + uuid.uuid4().hex
                state["sessions"][conversation_id] = 1
                answer = 700
            else:
                conversation_id = body.get("conversation_id", "")
                valid = state["sessions"].get(conversation_id) == 1 and self.headers.get("Cookie") == "sid=" + conversation_id
                answer = 200 if valid and question == "北京呢？" else -1
                state["sessions"][conversation_id] = 2
            state["requests"].append({"question": question, "conversation_id": conversation_id, "answer": answer})
        raw = json.dumps({"result": answer, "conversation_id": conversation_id}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Set-Cookie", "sid=" + conversation_id + "; Path=/")
        self.end_headers()
        self.wfile.write(raw)


class ConversationDashboardTests(DashboardWorkspace):
    def setUp(self):
        super().setUp()
        self.dialogue = ThreadingHTTPServer(("127.0.0.1", 0), ConversationTarget)
        self.dialogue.fixture = {"lock": threading.Lock(), "sessions": {}, "requests": []}
        threading.Thread(target=self.dialogue.serve_forever, daemon=True).start()
        self.addCleanup(self.dialogue.server_close)
        self.addCleanup(self.dialogue.shutdown)
        self.project = projects.create("多轮页面验收", f"http://127.0.0.1:{self.dialogue.server_port}", project_id="conversation-dashboard")
        self.endpoint = self.project["base_url"] + "/dialogue"

    def capture_har(self):
        entries = []
        for question, answer in (("全年销售额？", 700), ("北京呢？", 200)):
            entries.append({"_resourceType": "fetch", "request": {"method": "POST", "url": self.endpoint,
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "postData": {"mimeType": "application/json", "text": json.dumps({"question": question, "conversation_id": "recorded-session"})}},
                "response": {"status": 200, "content": {"mimeType": "application/json", "text": json.dumps({"result": answer, "conversation_id": "recorded-session"})}}})
        return {"log": {"version": "1.2", "entries": entries}}

    def open_conversations(self):
        self.open("conversations?project=" + self.project["id"] + "&range=all")
        expect(self.page.get_by_role("heading", name="多轮对话", exact=True)).to_be_visible()

    def import_source(self, *, upload):
        if upload:
            content = json.dumps(self.capture_har(), ensure_ascii=False)
            format_name = "har"
        else:
            content = "curl '" + self.endpoint + "' --json '{\"question\":\"全年销售额？\",\"conversation_id\":\"captured\"}'"
            format_name = "curl"
        response = self.client.post("/api/conversations/" + self.project["id"] + "/import-content", json={
            "name": "固定销售数据", "format": format_name, "content": content})
        self.assertEqual(response.status_code, 200, response.text)
        capture = response.json()
        self.open_conversations()
        expect(self.page.get_by_role("button", name="导入对话来源", exact=True)).to_have_count(0)
        self.page.get_by_role("link", name="查看原始来源", exact=True).click()
        expect(self.page.get_by_role("heading", name="真实请求与响应", exact=True)).to_be_visible()
        expect(self.page.get_by_role("button", name="构建多轮资产", exact=True)).to_have_count(0)
        self.assertEqual(self.page.locator("#view input, #view textarea").count(), 0)
        return capture

    def build(self, captured, mapping):
        return self.client.post("/api/conversations/" + self.project["id"] + "/" + captured["id"] + "/build",
                                json={"name": "固定销售数据", "mapping": mapping})

    def run_asset(self, asset, load):
        started = self.client.post("/api/native-runs", json={"project_id": self.project["id"], "asset_id": asset["id"], "parameters": {"load": load}})
        self.assertEqual(started.status_code, 200, started.text)
        return self.wait_native(started.json()["id"])

    def wait_native(self, run_id):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            run = self.client.get("/api/native-runs/" + run_id + "?all=1").json()
            if run["status"] not in ("Queued", "Running"):
                return run
            time.sleep(0.1)
        self.fail("Conversation run did not finish: " + self.log.read_text(encoding="utf-8"))
        return None

    def tearDown(self):
        with contextlib.suppress(Exception):
            for run in self.client.get("/api/runs?limit=200").json().get("items", []):
                if run.get("family") == "native" and run["status"] in ("Queued", "Running"):
                    self.client.post("/api/native-runs/" + run["id"] + "/cancel", json={})
        super().tearDown()

    def test_host_har_mapping_validation_and_browser_show_real_two_turn_results(self):
        captured = self.import_source(upload=True)
        self.page.get_by_text("请求 1 · POST " + self.endpoint, exact=True).click()
        expect(self.page.locator("pre.code").first).to_contain_text('"question"')
        mapping = {"turns": [1, 2], "question_pointer": "/question", "conversation_pointer": "/conversation_id",
                   "conversation_response_pointer": "/conversation_id", "turn_overrides": [{"expected": 700}, {"expected": 200}]}
        rejected = self.build(captured, mapping)
        self.assertEqual(rejected.status_code, 400, rejected.text)
        self.assertIn("answer_pointer", rejected.text)
        self.assertEqual(self.client.get("/api/assets/" + self.project["id"] + "?all=1").json()["assets"], [])
        mapping["answer_pointer"] = "/result"
        built = self.build(captured, mapping)
        self.assertEqual(built.status_code, 200, built.text)
        asset = built.json()
        self.assertEqual(asset["origin"]["capture_id"], captured["id"])
        result = self.run_asset(asset, {"concurrency": 2, "iterations": 3, "ramp_up_seconds": 0})
        self.assertEqual((result["status"], result["outcome"]["business_status"]), ("Pass", "Pass"), result)
        self.assertEqual((result["outcome"]["metrics"]["sent_turns"], result["outcome"]["metrics"]["planned_cases"]), (6, 3))
        self.assertEqual(len(self.dialogue.fixture["sessions"]), 3)
        self.assertEqual([row["answer"] for row in self.dialogue.fixture["requests"]].count(200), 3)
        self.open("native-runs/" + result["id"])
        self.page.locator(".result-head").first.click()
        expect(self.page.get_by_text("700", exact=True).first).to_be_visible(timeout=15000)
        self.evidence("conversation-report.png")
        self.open_conversations()
        expect(self.page.get_by_role("link", name="查看原始来源", exact=True)).to_have_count(1)
        self.page.get_by_role("link", name="查看资产", exact=True).click()
        expect(self.page.locator("#view")).to_contain_text(asset["asset_version"])
        self.open_conversations()
        self.page.reload()
        expect(self.page.get_by_role("link", name="查看原始来源", exact=True)).to_have_count(1)

    def test_curl_source_stays_expanded_after_refresh_and_host_reuses_turn_template(self):
        captured = self.import_source(upload=False)
        source = self.page.locator("#view details").first
        source.locator("summary").click()
        original = source.element_handle()
        change_project_and_wait_for_ui(self, self.project["id"])
        self.assertTrue(original.evaluate("node => node.isConnected && node.open"))
        expect(source).to_contain_text("全年销售额？")
        mapping = {"turns": [1, 1], "answer_pointer": "/result", "conversation_pointer": "/conversation_id",
                   "conversation_response_pointer": "/conversation_id", "turn_overrides": [{"expected": 700}, {"question": "北京呢？", "expected": 200}]}
        built = self.build(captured, mapping)
        self.assertEqual(built.status_code, 200, built.text)
        self.assertEqual(built.json()["turns"], 2)
        run = self.run_asset(built.json(), {"concurrency": 1, "iterations": 1, "ramp_up_seconds": 0})
        self.assertEqual((run["status"], run["outcome"]["business_status"]), ("Pass", "Pass"), run)
        self.assertEqual([turn["question"] for turn in run["results"][0]["detail"]["turns"]], ["全年销售额？", "北京呢？"])
        self.open_conversations()
        self.page.get_by_role("link", name="项目执行记录", exact=True).click()
        expect(self.page.locator("#view h1")).to_have_text("执行记录")
        expect(self.page.locator('#view a[href*="' + run["id"] + '"]')).to_be_visible()
