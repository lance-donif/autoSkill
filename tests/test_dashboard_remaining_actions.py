"""Cover remaining human dashboard branches with Chromium, real APIs and isolated data roots."""
import contextlib
import hashlib
import io
import json
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import openpyxl
from playwright.sync_api import expect
from test_data_dashboard import DataServer
from test_load_dashboard import DashboardWorkspace

from autoskill import builder, db, execution_inputs, native_assets, project_materials, projects, runtime_identity


class CancelTarget(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/slow":
            self.server.slow_started.set()
            self.server.release.wait(timeout=20)
        else:
            self.server.completed.set()
        data = b'{"answer":"done"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(data)


class RemainingDashboardActionsTests(DashboardWorkspace):
    def create_folder(self, name, parent=None):
        current = self.client.get(f"/api/projects/{self.pid}/folders").json()
        response = self.client.post(f"/api/projects/{self.pid}/folders", json={"name": name, "parent_id": parent, "expected_revision": current["revision"]})
        self.assertEqual(response.status_code, 200, response.text)
        return next(item["id"] for item in response.json()["folders"] if item["name"] == name)

    def assign(self, cases, folder):
        tree = self.client.get(f"/api/projects/{self.pid}/folders").json()
        response = self.client.post(f"/api/projects/{self.pid}/folders/move-cases", json={
            "case_ids": cases, "folder_id": folder, "expected_revision": tree["revision"]})
        self.assertEqual(response.status_code, 200, response.text)

    def knowledge(self, name="规则/退款.md", content="按原支付渠道退款。"):
        item = project_materials.write_knowledge(self.pid, name, content)
        return projects.path(self.pid) / "knowledge" / name, item

    def materials(self):
        self.open("project-materials?project=" + self.pid)
        expect(self.page.get_by_role("heading", name="知识库", exact=True)).to_be_visible()

    def wait_run(self, identity, family="runs"):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/{family}/{identity}" + ("?all=1" if family == "native-runs" else ""))
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            if result["status"] not in ("Queued", "Running"):
                return result
            time.sleep(0.05)
        self.fail("Execution did not finish: " + self.log.read_text(encoding="utf-8"))
        return None

    def download(self, locator):
        with self.page.expect_download() as event:
            locator.click()
        return Path(event.value.path()).read_bytes()

    def test_new_project_becomes_visible_only_after_a_valid_saved_case(self):
        self.open("cases?range=all")
        expect(self.page.get_by_role("button", name="新建项目", exact=True)).to_have_count(0)
        created = self.client.post("/api/projects", json={"name": "通过 DSH 建立的项目", "url": f"http://localhost:{self.target.server_port}"})
        self.assertEqual(created.status_code, 200, created.text)
        project_id = created.json()["id"]
        self.assertFalse(created.json().get("reused", False))
        self.assertEqual(self.client.get("/api/projects/" + project_id).json()["cases"]["total"], 0)
        expect(self.page.locator(".project-head h2").filter(has_text="通过 DSH 建立的项目")).to_have_count(0)
        saved = builder.api_case("TC-NEW-PROJECT", "新项目有效用例", "GET", "/slow", {"status": 200, "json_equals": {"/ok": True}})
        projects.save_cases(project_id, [saved])
        self.page.reload()
        expect(self.page.get_by_text("新项目有效用例", exact=True)).to_be_visible()
        expect(self.page.locator(".project-head h2").filter(has_text="通过 DSH 建立的项目")).to_have_count(1)
        self.assertEqual(self.client.get("/api/projects/" + project_id).json()["cases"]["total"], 1)
        self.evidence("remaining-created-project.png")

    def test_bulk_move_across_nested_folders_preserves_unselected_cases_after_reload(self):
        first = self.create_folder("订单")
        second = self.create_folder("售后")
        first_child = self.create_folder("订单回归", first)
        second_child = self.create_folder("售后回归", second)
        destination = self.create_folder("共同验证", first_child)
        self.assign(["TC-CHECK"], first_child)
        self.assign(["TC-SLOW"], second_child)
        self.assign(["TC-CHANGING"], second)
        before = self.client.get(f"/api/projects/{self.pid}/folders").json()["assignments"]
        definitions = {item["id"]: execution_inputs.canonical(item) for item in projects.cases(self.pid)}
        self.open("cases?range=all")
        for identity in ("TC-CHECK", "TC-SLOW"):
            self.page.get_by_role("checkbox", name="选择 " + identity, exact=True).check()
        self.page.get_by_role("button", name="移动到文件夹", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="移动用例", exact=True)
        dialog.get_by_label(f"「{self.pid}」目标文件夹", exact=True).select_option(destination)
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/folders/move-cases")) as moved:
            dialog.get_by_role("button", name="移动用例", exact=True).click()
        self.assertEqual(moved.value.status, 200, moved.value.text())
        expect(dialog).not_to_be_visible()
        self.page.reload()
        self.page.get_by_role("button", name="展开订单", exact=True).click()
        self.page.get_by_role("button", name="展开订单回归", exact=True).click()
        expect(self.page.get_by_role("button", name="共同验证，2 条用例", exact=True)).to_be_visible()
        self.page.get_by_role("button", name="共同验证，2 条用例", exact=True).click()
        expect(self.page.get_by_role("checkbox", name="选择 TC-CHECK", exact=True)).to_be_visible()
        expect(self.page.get_by_role("checkbox", name="选择 TC-SLOW", exact=True)).to_be_visible()
        expect(self.page.get_by_role("checkbox", name="选择 TC-CHANGING", exact=True)).to_have_count(0)
        after = self.client.get(f"/api/projects/{self.pid}/folders").json()["assignments"]
        self.assertEqual({identity for identity in before if before[identity] != after[identity]}, {"TC-CHECK", "TC-SLOW"})
        self.assertEqual(after["TC-CHECK"], destination)
        self.assertEqual(after["TC-SLOW"], destination)
        self.assertEqual({item["id"]: execution_inputs.canonical(item) for item in projects.cases(self.pid)}, definitions)
        self.evidence("remaining-bulk-folder-move.png")

    def test_selected_and_all_replay_buttons_keep_scope_and_retry_preserves_the_original_failure(self):
        self.open("cases?range=all")
        for identity in ("TC-CHECK", "TC-SLOW"):
            self.page.get_by_role("checkbox", name="选择 " + identity, exact=True).check()
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/api/runs")) as selected:
            self.page.get_by_role("button", name="运行所选 2 条", exact=True).click()
        self.assertEqual(selected.value.status, 200, selected.value.text())
        selected_id = selected.value.json()["runs"][0]["id"]
        run = self.wait_run(selected_id)
        self.assertEqual((run["status"], run["total"], run["passed"]), ("Pass", 2, 2), run)
        self.assertEqual({row["case_id"] for row in run["results"] if not row["prerequisite"]}, {"TC-CHECK", "TC-SLOW"})
        self.assertEqual({row["case_id"] for row in run["results"] if row["prerequisite"]}, {"TC-BEGIN"})
        self.open("cases?range=all")
        self.page.get_by_role("checkbox", name="选择「页面压测验收」当前显示的 5 条用例", exact=True).check()
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/api/runs")) as all_started:
            self.page.get_by_role("button", name="运行所选 5 条", exact=True).click()
        self.assertEqual(all_started.value.status, 200, all_started.value.text())
        original_id = all_started.value.json()["runs"][0]["id"]
        original = self.wait_run(original_id)
        self.assertEqual((original["status"], original["total"], original["passed"], original["failed"]), ("Fail", 5, 4, 1), original)
        self.assertEqual({row["case_id"] for row in original["results"] if not row["prerequisite"]}, {"TC-BEGIN", "TC-CHECK", "TC-SLOW", "TC-CHANGING", "TC-UI"})
        expect(self.page.locator(".run-summary .status").first).to_have_text("不通过", timeout=15000)
        expect(self.page.get_by_role("button", name="重跑失败的 1 条", exact=True)).to_have_count(0)
        retried = self.client.post(f"/api/runs/{original_id}/rerun", json={"failed_only": True, "headed": False})
        self.assertEqual(retried.status_code, 200, retried.text)
        repeated = self.wait_run(retried.json()["id"])
        self.assertEqual((repeated["status"], repeated["total"], repeated["passed"]), ("Pass", 1, 1), repeated)
        self.assertEqual([row["case_id"] for row in repeated["results"] if not row["prerequisite"]], ["TC-CHANGING"])
        self.assertEqual(self.client.get("/api/runs/" + original_id).json()["failed"], 1)

    def test_knowledge_delete_cancel_preserves_bytes_and_confirm_removes_only_selected_file(self):
        selected, _ = self.knowledge()
        retained, _ = self.knowledge("规则/不能删除.md", "这份资料必须保留。")
        original = selected.read_bytes()
        retained_bytes = retained.read_bytes()
        self.materials()
        row = self.page.get_by_role("row").filter(has_text="规则/退款.md")
        row.get_by_role("button", name="删除", exact=True).click()
        confirm = self.page.get_by_role("alertdialog", name="删除这份知识？", exact=True)
        confirm.get_by_role("button", name="取消", exact=True).click()
        self.assertEqual(selected.read_bytes(), original)
        row.get_by_role("button", name="删除", exact=True).click()
        with self.page.expect_response(lambda response: response.request.method == "DELETE" and response.url.endswith("/knowledge")) as deleted:
            self.page.get_by_role("alertdialog", name="删除这份知识？", exact=True).get_by_role("button", name="删除", exact=True).click()
        self.assertEqual(deleted.value.status, 200, deleted.value.text())
        expect(row).to_have_count(0)
        self.page.reload()
        expect(self.page.get_by_role("row").filter(has_text="规则/不能删除.md")).to_have_count(1)
        self.assertFalse(selected.exists())
        self.assertEqual(retained.read_bytes(), retained_bytes)

    def test_knowledge_conflict_keeps_remote_bytes_and_local_draft_then_recovers_after_reopen(self):
        file, initial = self.knowledge()
        self.materials()
        self.page.get_by_role("row").filter(has_text="规则/退款.md").get_by_role("button", name="查看 / 编辑", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="查看与编辑知识", exact=True)
        dialog.get_by_label("内容", exact=True).fill("本地尚未提交的草稿。")
        changed = self.client.put(f"/api/project-materials/{self.pid}/knowledge", json={
            "file": "规则/退款.md", "content": "另一会话保存的退款规则。", "expected_hash": initial["sha256"]})
        self.assertEqual(changed.status_code, 200, changed.text)
        with self.page.expect_response(lambda response: response.request.method == "PUT" and response.url.endswith("/knowledge")) as rejected:
            dialog.get_by_role("button", name="保存", exact=True).click()
        self.assertEqual(rejected.value.status, 400, rejected.value.text())
        expect(dialog.get_by_role("alert")).to_contain_text("知识文件已变化")
        expect(dialog.get_by_label("内容", exact=True)).to_have_value("本地尚未提交的草稿。")
        expect(dialog.get_by_role("button", name="保存", exact=True)).to_be_enabled()
        self.assertEqual(file.read_text(), "另一会话保存的退款规则。")
        dialog.get_by_role("button", name="取消", exact=True).click()
        self.page.reload()
        self.page.get_by_role("row").filter(has_text="规则/退款.md").get_by_role("button", name="查看 / 编辑", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="查看与编辑知识", exact=True)
        expect(dialog.get_by_label("内容", exact=True)).to_have_value("另一会话保存的退款规则。")
        dialog.get_by_label("内容", exact=True).fill("读取最新版本后保存的最终规则。")
        dialog.get_by_role("button", name="保存", exact=True).click()
        expect(dialog).not_to_be_visible()
        self.assertEqual(file.read_text(), "读取最新版本后保存的最终规则。")
        self.evidence("remaining-knowledge-conflict-recovered.png")

    def test_browser_uploaded_share_imports_into_empty_data_root_and_default_repeat_preserves_existing_bytes(self):
        knowledge, _ = self.knowledge()
        parent = self.create_folder("分享的目录")
        nested = self.create_folder("分享的空子目录", parent)
        self.assign(["TC-CHECK"], parent)
        case_files = {file.relative_to(projects.path(self.pid)).as_posix(): file.read_bytes()
                      for file in (projects.path(self.pid) / "testcases").rglob("*.json")}
        self.materials()
        self.page.get_by_role("button", name="分享与导入", exact=True).click()
        self.page.get_by_role("button", name="生成分享包", exact=True).click()
        package = self.download(self.page.get_by_role("link", name="下载分享包", exact=True))
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(package)))
        consumer = DataServer(self.folder / "empty-share-consumer")
        self.addCleanup(consumer.close)
        self.assertEqual(consumer.api("/projects"), [])
        target_page = self.context.new_page()
        self.addCleanup(target_page.close)
        target_page.on("pageerror", lambda error: self.errors.append(str(error)))
        target_page.goto(consumer.origin + "/#/project-materials?tab=share")
        expect(target_page.get_by_role("heading", name="项目资料", exact=True)).to_be_visible()
        expect(target_page.get_by_label("替换本机已有同名项目", exact=True)).not_to_be_checked()
        target_page.get_by_label("从这台设备选择 ZIP", exact=True).set_input_files({"name": "shared-project.zip", "mimeType": "application/zip", "buffer": package})
        with target_page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/api/shares/upload")) as imported:
            target_page.get_by_role("button", name="导入项目分享包", exact=True).click()
        self.assertEqual(imported.value.status, 200, imported.value.text())
        expect(target_page.get_by_text("已导入 1 个项目 · 0 个未导入", exact=True)).to_be_visible()
        self.assertEqual([item["id"] for item in imported.value.json()["imported"]], [self.pid])
        imported_root = consumer.root / "projects" / self.pid
        for name, content in case_files.items():
            self.assertEqual((imported_root / name).read_bytes(), content)
        imported_knowledge = imported_root / "knowledge/规则/退款.md"
        self.assertEqual(imported_knowledge.read_bytes(), knowledge.read_bytes())
        tree = consumer.api(f"/projects/{self.pid}/folders")
        self.assertEqual(tree["assignments"]["TC-CHECK"], parent)
        self.assertEqual(next(item["parent_id"] for item in tree["folders"] if item["id"] == nested), parent)
        current_hash = hashlib.sha256(imported_knowledge.read_bytes()).hexdigest()
        consumer.api(f"/project-materials/{self.pid}/knowledge", method="PUT", body={
            "file": "规则/退款.md", "content": "导入后新增的本机规则不得覆盖。", "expected_hash": current_hash})
        target_page.get_by_role("button", name="导入项目分享包", exact=True).click()
        expect(target_page.get_by_text("已导入 0 个项目 · 1 个未导入", exact=True)).to_be_visible()
        self.assertEqual(imported_knowledge.read_text(), "导入后新增的本机规则不得覆盖。")
        self.assertEqual(consumer.api("/projects")[0]["cases"]["total"], 5)
        self.assertFalse(list((consumer.root / ".local/share-imports").glob("*.zip")))

    def test_native_cancellation_preserves_completed_case_and_unfinished_denominator(self):
        target = ThreadingHTTPServer(("127.0.0.1", 0), CancelTarget)
        target.completed, target.slow_started, target.release = threading.Event(), threading.Event(), threading.Event()
        threading.Thread(target=target.serve_forever, daemon=True).start()
        self.addCleanup(target.server_close)
        self.addCleanup(target.shutdown)
        self.addCleanup(target.release.set)
        source = self.folder / "cancel-spec.json"
        fast = f"http://127.0.0.1:{target.server_port}/fast"
        slow = f"http://127.0.0.1:{target.server_port}/slow"
        source.write_text(json.dumps({"endpoint": {"protocol": "http", "url": fast, "answer_pointer": "/answer"},
            "samples": [{"id": "completed", "title": "已经完成的对话", "question": "first", "expected": "done"},
                        {"id": "interrupted", "title": "取消中的对话", "question": "second", "expected": "done", "endpoint": {"url": slow}},
                        {"id": "not-started", "title": "尚未执行的对话", "question": "third", "expected": "done"}],
            "load": {"concurrency": 1, "iterations": 1}}), encoding="utf-8")
        asset = native_assets.import_asset(self.pid, str(source), "ai-eval", name="可取消原生对话")
        started = self.client.post("/api/native-runs", json={"project_id": self.pid, "asset_id": asset["id"]})
        self.assertEqual(started.status_code, 200, started.text)
        run_id = started.json()["id"]
        self.addCleanup(lambda: self.client.post("/api/native-runs/" + run_id + "/cancel", json={}))
        self.open("native-runs/" + run_id)
        self.assertTrue(target.completed.wait(timeout=10), "The first native conversation never reached the real service")
        self.assertTrue(target.slow_started.wait(timeout=10), "The second native conversation never reached the blocking service")
        owner = db.one("SELECT pid,owner_identity FROM runs WHERE id = ?", (run_id,))
        self.assertTrue(runtime_identity.owns(owner["pid"], owner["owner_identity"]))
        expect(self.page.get_by_role("button", name="取消运行", exact=True)).to_have_count(0)
        cancelled = self.client.post(f"/api/native-runs/{run_id}/cancel", json={})
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        run = self.wait_run(run_id, "native-runs")
        self.assertEqual(run["status"], "Cancelled", run)
        deadline = time.monotonic() + 10
        while runtime_identity.owns(owner["pid"], owner["owner_identity"]) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(runtime_identity.owns(owner["pid"], owner["owner_identity"]))
        self.assertEqual(run["outcome"]["metrics"]["planned_cases"], 3)
        self.assertEqual(run["outcome"]["metrics"]["quality_passed"], 1)
        self.assertLess(run["outcome"]["metrics"]["completed_cases"], 3)
        completed = [item for item in run["results"] if item["case_id"] == "completed:1"]
        self.assertEqual(len(completed), 1)
        self.assertEqual((completed[0]["status"], completed[0]["detail"]["business_status"]), ("Pass", "Pass"))
        self.assertEqual(completed[0]["detail"]["turns"][0]["answer"], "done")
        self.assertFalse(any(item["case_id"] == "not-started:1" for item in run["results"]))
        expect(self.page.locator("#view section .status").first).to_have_text("不通过", timeout=15000)
        self.page.reload()
        expect(self.page.get_by_role("button", name="取消运行", exact=True)).to_have_count(0)
        expect(self.page.locator(".result-head").filter(has_text="已经完成的对话")).to_be_visible()
        self.evidence("remaining-native-cancel.png")

    def test_project_folder_downloads_and_self_contained_html_open_offline_with_embedded_screenshot(self):
        folder = self.create_folder("离线报告目录")
        child = self.create_folder("子目录", folder)
        self.assign(["TC-UI"], child)
        self.open("cases?range=all")
        self.page.get_by_role("button", name="离线报告目录，0 条用例", exact=True).click()
        scoped = self.download(self.page.get_by_role("link", name="导出文件夹 Excel", exact=True))
        full = self.download(self.page.get_by_role("link", name="导出测试用例", exact=True))
        with contextlib.ExitStack() as stack:
            folder_book = stack.enter_context(contextlib.closing(openpyxl.load_workbook(io.BytesIO(scoped))))
            project_book = stack.enter_context(contextlib.closing(openpyxl.load_workbook(io.BytesIO(full))))
            self.assertEqual({row[1] for row in list(folder_book["编号映射"].values)[1:]}, {"TC-UI"})
            self.assertEqual({row[1] for row in list(project_book["编号映射"].values)[1:]}, {"TC-BEGIN", "TC-CHECK", "TC-SLOW", "TC-CHANGING", "TC-UI"})
        self.page.get_by_role("button", name="子目录，1 条用例", exact=True).click()
        row = self.page.get_by_role("row").filter(has_text="页面独立浏览器会话")
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/api/runs")) as started:
            row.get_by_role("button", name="运行", exact=True).click()
        self.assertEqual(started.value.status, 200, started.value.text())
        run_id = started.value.json()["runs"][0]["id"]
        run = self.wait_run(run_id)
        self.assertEqual((run["status"], run["passed"], run["total"]), ("Pass", 1, 1), run)
        expect(self.page.locator(".run-summary .status").first).to_have_text("通过", timeout=15000)
        html = self.download(self.page.get_by_role("button", name="下载 HTML 报告", exact=True))
        self.assertIn(b"data:image/png;base64,", html)
        offline_file = self.folder / "standalone-report.html"
        offline_file.write_bytes(html)
        isolated = self.browser.new_context(offline=True)
        self.addCleanup(isolated.close)
        reader = isolated.new_page()
        reader.on("pageerror", lambda error: self.errors.append(str(error)))
        requests = []
        reader.on("request", lambda request: requests.append(request.url))
        self.assertEqual(offline_file.read_bytes(), html)
        # Managed Chromium blocks file navigation; this verifies the exact saved bytes offline.
        # Opening the file:// URL itself remains outside this environment's acceptance.
        reader.set_content(offline_file.read_bytes().decode('utf-8'))
        expect(reader.get_by_text("页面独立浏览器会话", exact=True)).to_be_visible()
        images = reader.locator("a.shot img")
        self.assertGreater(images.count(), 0)
        self.assertTrue(all(images.evaluate_all("nodes => nodes.map(node => node.complete && node.naturalWidth > 0)")))
        self.assertTrue(all(not url.startswith(("http://", "https://")) for url in requests), requests)
        reader.get_by_role("button", name="全部收起", exact=True).click()
        expect(reader.locator(".result-head").first).to_have_attribute("aria-expanded", "false")
        reader.get_by_role("button", name="全部展开", exact=True).click()
        expect(reader.locator(".result-head").first).to_have_attribute("aria-expanded", "true")
        self.evidence("remaining-downloads.png")
