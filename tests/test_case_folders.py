"""Case folders retain empty hierarchies and move only organization across processes and packages."""
import concurrent.futures
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from autoskill import builder, case_folders, db, execution_inputs, projects, reporting, runner, share
from autoskill.server.app import create_app

PID = "folders"


class CaseFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "data"), "AUTOSKILL_HEADED": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        projects.create("Folders", "https://folders.example.invalid", project_id=PID)
        self.cases = [builder.api_case("TC-API-001", "Create", "POST", "/items", {"status": 201, "json_equals": {"/created": True}}),
                      builder.api_case("TC-API-002", "Read", "GET", "/items/{{item_id}}", {"status": 200, "json_equals": {"/id": "{{item_id}}"}})]
        self.cases[0]["module"] = "订单/查询"
        self.cases[0]["extensions"]["extract"] = {"item_id": "/id"}
        self.cases[1]["module"] = "查询"
        self.cases[1]["extensions"]["depends_on"] = [self.cases[0]["id"]]
        projects.save_cases(PID, self.cases)

    def snapshot(self):
        return case_folders.list_folders(PID)

    def create(self, name, parent_id=None):
        current = self.snapshot()
        result = case_folders.create(PID, name, parent_id=parent_id, expected_revision=current["revision"])
        folder = next(folder for folder in result["folders"] if folder["name"] == name and folder["parent_id"] == parent_id)
        return result, folder["id"]

    def test_legacy_modules_are_literal_labels_not_path_segments(self):
        result = self.snapshot()
        self.assertEqual({folder["name"] for folder in result["folders"]}, {"订单/查询", "查询"})
        self.assertTrue(all(folder["parent_id"] is None for folder in result["folders"]))
        self.assertEqual(result, self.snapshot())
        self.assertFalse((projects.path(PID) / "domain/HEAD.json").exists())

    def test_empty_nested_folders_persist_and_batch_move_preserves_case_bytes(self):
        before = {file.name: file.read_bytes() for file in (projects.path(PID) / "testcases").iterdir()}
        _, parent = self.create("回归")
        current, child = self.create("查询", parent)
        moved = case_folders.move_cases(PID, [case["id"] for case in self.cases], child, expected_revision=current["revision"])
        self.assertEqual(set(moved["assignments"].values()), {child})
        self.assertIn(parent, {folder["id"] for folder in self.snapshot()["folders"]})
        self.assertEqual(before, {file.name: file.read_bytes() for file in (projects.path(PID) / "testcases").iterdir()})
        current = case_folders.move_cases(PID, [self.cases[0]["id"]], None, expected_revision=moved["revision"])
        self.assertIsNone(current["assignments"][self.cases[0]["id"]])
        self.assertEqual(current["assignments"][self.cases[1]["id"]], child)
        self.assertEqual(current, self.snapshot())

    def test_missing_case_batch_is_atomic_and_deleted_assignments_are_filtered(self):
        current, folder_id = self.create("移动目标")
        with self.assertRaisesRegex(ValueError, "不存在或已归档"):
            case_folders.move_cases(PID, [self.cases[0]["id"], "TC-API-999"], folder_id, expected_revision=current["revision"])
        self.assertEqual(current, self.snapshot())
        moved = case_folders.move_cases(PID, [self.cases[1]["id"]], folder_id, expected_revision=current["revision"])
        projects.delete_case(PID, self.cases[1]["id"])
        filtered = self.snapshot()
        self.assertNotIn(self.cases[1]["id"], filtered["assignments"])
        self.assertIn(folder_id, {folder["id"] for folder in filtered["folders"]})
        with self.assertRaises(case_folders.FolderConflictError):
            case_folders.move_cases(PID, [self.cases[1]["id"]], None, expected_revision=moved["revision"])

    def test_duplicate_names_missing_parent_and_folder_cycles_do_not_publish(self):
        current, parent = self.create("回归")
        current, child = self.create("查询", parent)
        operations = [lambda: case_folders.create(PID, "查询", parent_id=parent, expected_revision=current["revision"]),
                      lambda: case_folders.create(PID, "新建", parent_id="gone", expected_revision=current["revision"]),
                      lambda: case_folders.update(PID, parent, {"parent_id": child}, expected_revision=current["revision"]),
                      lambda: case_folders.update(PID, child, {"parent_id": child}, expected_revision=current["revision"]),
                      lambda: case_folders.update(PID, child, {"parent_id": "gone"}, expected_revision=current["revision"])]
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                operation()
            self.assertEqual(current, self.snapshot())
        renamed = case_folders.update(PID, child, {"name": "修改", "parent_id": None}, expected_revision=current["revision"])
        self.assertEqual(next(folder for folder in renamed["folders"] if folder["id"] == child), {"id": child, "name": "修改", "parent_id": None})

    def test_competing_updates_allow_one_winner_without_lost_folders(self):
        current = self.snapshot()

        def create(name):
            try:
                case_folders.create(PID, name, expected_revision=current["revision"])
                return "saved"
            except case_folders.FolderConflictError:
                return "conflict"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(create, ["甲", "乙"]))
        self.assertEqual(sorted(outcomes), ["conflict", "saved"])
        names = {folder["name"] for folder in self.snapshot()["folders"]}
        self.assertEqual(len(names & {"甲", "乙"}), 1)

    def test_current_case_edit_conflicts_instead_of_overwriting_its_definition(self):
        current, folder_id = self.create("目标")
        case = projects.case(PID, self.cases[0]["id"])
        case["title"] = "Updated title"
        projects.save_cases(PID, [case])
        with self.assertRaises(case_folders.FolderConflictError):
            case_folders.move_cases(PID, [case["id"]], folder_id, expected_revision=current["revision"])
        self.assertEqual(projects.case(PID, case["id"])["title"], "Updated title")

    def test_project_pack_retains_empty_folders_and_assignments(self):
        _, parent = self.create("空目录")
        current, child = self.create("嵌套", parent)
        expected = case_folders.move_cases(PID, [self.cases[0]["id"]], child, expected_revision=current["revision"])
        package = share.pack([PID], str(self.root / "project.zip"))["file"]
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.root / "imported")}):
            result = share.import_pack(package)
            self.assertEqual([item["id"] for item in result["imported"]], [PID])
            self.assertEqual(expected, self.snapshot())

    def test_folder_changes_do_not_invalidate_existing_execution_scope(self):
        before = execution_inputs.capture(PID, [self.cases[0]["id"]], "playwright", persist=False)
        current, folder_id = self.create("整理")
        case_folders.move_cases(PID, [self.cases[0]["id"]], folder_id, expected_revision=current["revision"])
        after = execution_inputs.capture(PID, [self.cases[0]["id"]], "playwright", persist=False)
        self.assertEqual(before["scope_hashes"], after["scope_hashes"])
        self.assertEqual(before["sources"]["files"], after["sources"]["files"])

    def test_verified_result_remains_current_after_folder_creation_and_move(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                data = json.dumps({"ready": True}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        verified_pid = projects.create("Verified", f"http://127.0.0.1:{server.server_port}", project_id="verified")["id"]
        projects.save_cases(verified_pid, [builder.api_case("TC-API-001", "Ready", "GET", "/ready", {"status": 200, "json_equals": {"/ready": True}})])
        run = runner.execute(runner.create(verified_pid)["id"])
        self.assertEqual(run["status"], "Pass", run)
        result = db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],))
        case = projects.case(verified_pid, "TC-API-001")
        self.assertTrue(execution_inputs.result_current(verified_pid, case, result))
        current = case_folders.list_folders(verified_pid)
        current = case_folders.create(verified_pid, "已验证", expected_revision=current["revision"])
        target = next(folder["id"] for folder in current["folders"] if folder["name"] == "已验证")
        case_folders.move_cases(verified_pid, [case["id"]], target, expected_revision=current["revision"])
        self.assertTrue(execution_inputs.result_current(verified_pid, case, result))

    def test_export_folder_contains_descendants_without_external_prerequisites(self):
        projects.save_cases(PID, [builder.api_case("TC-API-003", "Nested unexecuted", "GET", "/nested", {"status": 200, "json_equals": {"/ok": True}})])
        _, parent = self.create("待导出")
        current, child = self.create("子目录", parent)
        current = case_folders.move_cases(PID, [self.cases[1]["id"]], parent, expected_revision=current["revision"])
        current = case_folders.move_cases(PID, ["TC-API-003"], child, expected_revision=current["revision"])
        selected = case_folders.selection(PID, parent)
        self.assertEqual(set(selected["case_ids"]), {self.cases[1]["id"], "TC-API-003"})
        self.assertEqual(case_folders.selection(PID, parent, recursive=False)["case_ids"], [self.cases[1]["id"]])
        receipt = reporting.export_cases(PID, selected["case_ids"], organization=selected)
        book = load_workbook(receipt["excel"], data_only=False)
        self.addCleanup(book.close)
        self.assertEqual(book.worksheets[0].max_column, 14)
        self.assertEqual({row[1] for row in book["编号映射"].iter_rows(min_row=2, values_only=True)}, {self.cases[1]["id"], "TC-API-003"})
        self.assertEqual({row[0] for row in book["用例与结果"].iter_rows(min_row=2, values_only=True)}, {self.cases[1]["id"], "TC-API-003"})
        self.assertEqual({row[8] for row in book["用例与结果"].iter_rows(min_row=2, values_only=True)}, {"未执行"})
        self.assertEqual(set(case_folders.selection(PID, "root")["case_ids"]), {case["id"] for case in projects.cases(PID)})

    def test_empty_folder_export_is_readable_and_root_http_exports_entire_project(self):
        current, empty = self.create("空表")
        client = TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787")
        path = f"/api/projects/{PID}/folders/export"
        response = client.get(path, params={"folder_id": empty, "expected_revision": current["revision"]})
        self.assertEqual(response.status_code, 200, response.text if response.status_code != 200 else "")
        self.assertEqual(response.headers["X-AutoSkill-Cases"], "0")
        book = load_workbook(io.BytesIO(response.content), data_only=False)
        self.addCleanup(book.close)
        self.assertEqual(book.worksheets[0].max_column, 14)
        self.assertIn("空范围不表示覆盖或测试通过", str(book.worksheets[0].cell(2, 13).value))
        entire = client.get(path)
        self.assertEqual(entire.status_code, 200)
        self.assertEqual(entire.headers["X-AutoSkill-Cases"], "2")
        self.assertEqual(client.get(path, params={"folder_id": "does-not-exist"}).status_code, 400)

    def test_http_mutations_require_revision_and_expose_conflicts(self):
        client = TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787")
        path = f"/api/projects/{PID}/folders"
        current = client.get(path).json()
        created = client.post(path, json={"name": "API", "expected_revision": current["revision"]})
        self.assertEqual(created.status_code, 200, created.text)
        stale = client.post(path, json={"name": "旧版本", "expected_revision": current["revision"]})
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["code"], "folder_conflict")
        self.assertEqual(client.post(path, json={"name": "没有版本"}).status_code, 400)
        latest = created.json()
        folder_id = next(folder["id"] for folder in latest["folders"] if folder["name"] == "API")
        moved = client.post(path + "/move-cases", json={"case_ids": [self.cases[0]["id"]], "folder_id": folder_id,
                                                       "expected_revision": latest["revision"]})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(moved.json()["assignments"][self.cases[0]["id"]], folder_id)


if __name__ == "__main__":
    unittest.main()
