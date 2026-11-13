"""Exercise browser upload and mapping against real stateful multi-turn HTTP conversations."""
import contextlib
import json
import re
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
        self.open_conversations()
        self.page.get_by_role("button", name="导入对话来源", exact=True).first.click()
        dialog = self.page.get_by_role("dialog", name="导入对话来源", exact=True)
        if upload:
            source = self.folder / "conversation.har"
            source.write_text(json.dumps(self.capture_har(), ensure_ascii=False), encoding="utf-8")
            dialog.get_by_label("选择 cURL 或 HAR 文件", exact=True).set_input_files(str(source))
            expect(dialog.get_by_label("来源内容", exact=True)).to_have_value(re.compile('"entries"'))
        else:
            dialog.get_by_label("来源格式", exact=True).select_option("curl")
            dialog.get_by_label("来源内容", exact=True).fill("curl '" + self.endpoint + "' --json '{\"question\":\"全年销售额？\",\"conversation_id\":\"captured\"}'")
        dialog.get_by_label("来源名称", exact=True).fill("固定销售数据")
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/import-content")) as response:
            dialog.get_by_role("button", name="保存来源", exact=True).click()
        self.assertEqual(response.value.status, 200, response.value.text())
        capture = response.value.json()
        expect(self.page.get_by_role("heading", name="建立多轮验证", exact=True)).to_be_visible()
        return capture

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

    def test_uploaded_har_recovers_invalid_mapping_and_executes_two_turn_conversations(self):
        captured = self.import_source(upload=True)
        self.page.get_by_text("请求 1 · POST " + self.endpoint, exact=True).click()
        expect(self.page.locator("pre.code").first).to_contain_text('"question"')
        self.page.get_by_label("问题字段", exact=True).fill("/question")
        self.page.get_by_label("请求中的会话字段", exact=True).fill("/conversation_id")
        self.page.get_by_label("响应中的会话字段", exact=True).fill("/conversation_id")
        for index, expected in ((1, "700"), (2, "200")):
            self.page.get_by_label(f"第 {index} 轮预期类型", exact=True).select_option("json")
            self.page.get_by_label(f"第 {index} 轮独立预期", exact=True).fill(expected)
        self.page.get_by_text("参考数据、流事件与高级映射", exact=True).click()
        self.page.get_by_label("高级映射 JSON", exact=True).fill("{invalid")
        self.page.get_by_role("button", name="构建多轮资产", exact=True).click()
        expect(self.page.locator("#view [role=alert]")).to_be_visible()
        self.assertEqual(self.client.get("/api/assets/" + self.project["id"] + "?all=1").json()["assets"], [])
        self.page.get_by_label("高级映射 JSON", exact=True).fill("{}")
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/build")) as rejected:
            self.page.get_by_role("button", name="构建多轮资产", exact=True).click()
        self.assertEqual(rejected.value.status, 400)
        expect(self.page.locator("#view [role=alert]")).to_contain_text("answer_pointer")
        self.page.get_by_label("回答字段", exact=True).fill("/result")
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/build")) as built:
            self.page.get_by_role("button", name="构建多轮资产", exact=True).click()
        self.assertEqual(built.value.status, 200, built.value.text())
        self.assertEqual(built.value.json()["origin"]["capture_id"], captured["id"])
        dialog = self.page.get_by_role("dialog", name="运行多轮对话", exact=True)
        dialog.get_by_label("并发会话数", exact=True).fill("2")
        dialog.get_by_label("每条对话迭代次数", exact=True).fill("3")
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/api/native-runs")) as started:
            dialog.get_by_role("button", name="开始运行", exact=True).click()
        self.assertEqual(started.value.status, 200, started.value.text())
        run_id = started.value.json()["id"]
        result = self.wait_native(run_id)
        self.assertEqual((result["status"], result["outcome"]["business_status"]), ("Pass", "Pass"), result)
        self.assertEqual((result["outcome"]["metrics"]["sent_turns"], result["outcome"]["metrics"]["planned_cases"]), (6, 3))
        self.assertEqual(len(self.dialogue.fixture["sessions"]), 3)
        self.assertEqual([row["answer"] for row in self.dialogue.fixture["requests"]].count(200), 3)
        expect(self.page).to_have_url(re.compile("/#/native-runs/" + run_id))
        self.page.locator(".result-head").first.click()
        expect(self.page.get_by_text("700", exact=True).first).to_be_visible(timeout=15000)
        self.evidence("conversation-report.png")
        self.open_conversations()
        expect(self.page.get_by_role("link", name="查看与构建", exact=True)).to_have_count(1)
        self.page.reload()
        expect(self.page.get_by_role("link", name="查看与构建", exact=True)).to_have_count(1)

    def test_pasted_curl_builds_repeated_turn_template_with_explicit_expectations(self):
        self.import_source(upload=False)
        self.page.get_by_label("轮次请求序号", exact=True).fill("1, 1")
        self.page.get_by_label("轮次请求序号", exact=True).press("Tab")
        self.page.get_by_label("回答字段", exact=True).fill("/result")
        self.page.get_by_label("请求中的会话字段", exact=True).fill("/conversation_id")
        self.page.get_by_label("响应中的会话字段", exact=True).fill("/conversation_id")
        self.page.get_by_label("第 2 轮问题", exact=True).fill("北京呢？")
        for index, expected in ((1, "700"), (2, "200")):
            self.page.get_by_label(f"第 {index} 轮预期类型", exact=True).select_option("json")
            self.page.get_by_label(f"第 {index} 轮独立预期", exact=True).fill(expected)
        original = self.page.locator('#view form').element_handle()
        change_project_and_wait_for_ui(self, self.project['id'])
        self.assertTrue(original.evaluate("node => node === document.querySelector('#view form') && node.isConnected"))
        expect(self.page.get_by_label('轮次请求序号', exact=True)).to_have_value('1, 1')
        expect(self.page.get_by_label('回答字段', exact=True)).to_have_value('/result')
        expect(self.page.get_by_label('请求中的会话字段', exact=True)).to_have_value('/conversation_id')
        expect(self.page.get_by_label('响应中的会话字段', exact=True)).to_have_value('/conversation_id')
        expect(self.page.get_by_label('第 2 轮问题', exact=True)).to_have_value('北京呢？')
        for index, expected in ((1, '700'), (2, '200')):
            expect(self.page.get_by_label(f'第 {index} 轮预期类型', exact=True)).to_have_value('json')
            expect(self.page.get_by_label(f'第 {index} 轮独立预期', exact=True)).to_have_value(expected)
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/build")) as built:
            self.page.get_by_role("button", name="构建多轮资产", exact=True).click()
        self.assertEqual(built.value.status, 200, built.value.text())
        self.assertEqual(built.value.json()["turns"], 2)
        mapping = built.value.request.post_data_json['mapping']
        self.assertEqual(mapping['turns'], [1, 1])
        self.assertEqual([item['expected'] for item in mapping['turn_overrides']], [700, 200])
        self.assertEqual(mapping['turn_overrides'][1]['question'], '北京呢？')
        dialog = self.page.get_by_role("dialog", name="运行多轮对话", exact=True)
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/api/native-runs")) as started:
            dialog.get_by_role("button", name="开始运行", exact=True).click()
        run = self.wait_native(started.value.json()["id"])
        self.assertEqual((run["status"], run["outcome"]["business_status"]), ("Pass", "Pass"), run)
        self.assertEqual([turn["question"] for turn in run["results"][0]["detail"]["turns"]], ["全年销售额？", "北京呢？"])
