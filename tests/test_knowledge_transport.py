"""Real CLI, bridge and HTTP calls share knowledge revisions and frozen failure evidence."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "skills/autoskill/app"
sys.path.insert(0, str(APP))

from autoskill import builder, cli, command_input, context_service, db, domain, knowledge_entries, paths, projects, runner, workflow
from autoskill.server import jobs
from autoskill.server.app import create_app


class FailedBusinessSite(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"approved":false}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


class KnowledgeTransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.caller = self.root / "caller"
        self.caller.mkdir()
        environment = patch.dict(os.environ, {
            "AUTOSKILL_WORKSPACE": str(self.root / "workspace"), "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_HEADED": "0",
            "AUTOSKILL_CALLER_CWD": "", "AUTOSKILL_INPUT_MAP": "", "AUTOSKILL_RUNTIME_CONTEXT": "", "AUTOSKILL_WORKFLOW_BINDING": "",
            "AUTOSKILL_CASE_SKILL": str(self.root / "sheet"), "AUTOSKILL_QUEUE_IDLE_SECONDS": "0.1", "PYTHONPATH": str(APP),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.pid = projects.create("知识来源", "http://127.0.0.1:9", project_id="transport-source")["id"]
        self.target = projects.create("知识接收", "http://127.0.0.1:10", project_id="transport-target")["id"]
        self.client = TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787")
        self.addCleanup(self.client.close)
        self.dispatchers = set(jobs._dispatchers)
        self.addCleanup(self.wait_dispatcher)

    def wait_dispatcher(self):
        deadline = time.monotonic() + 10
        while (paths.local() / "queue-worker.json").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        for pid, child in jobs._dispatchers.items():
            if pid not in self.dispatchers:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))

    def command(self, *argv, payload=None):
        result = subprocess.run([sys.executable, "-m", "autoskill", *argv], cwd=self.caller, env=dict(os.environ),
                                input=None if payload is None else json.dumps(payload, ensure_ascii=False), capture_output=True,
                                text=True, encoding="utf-8", check=False, timeout=30)
        self.assertTrue(result.stdout, result.stderr)
        return result.returncode, json.loads(result.stdout)

    def seed(self, project=None, title="业务知识"):
        project = project or self.pid
        prepared = knowledge_entries.prepare_mutations(project, "wf-fixture", "cycle-fixture", {"schema_version": 2, "mutations": [{
            "op": "create", "client_key": uuid.uuid4().hex, "entry": {
                "title": title, "text": "需要保留事实，仍待本机验证。", "kind": "observation",
                "scope": {"project_id": project, "team_id": None, "business_scope": None, "source_version_hashes": {}},
                "evidence_refs": [], "validation_refs": [],
            },
        }]})
        domain.commit(project, prepared["changes"], domain.read(project)["head"])
        return prepared["entries"][0]

    def bridge_file(self, argv, source):
        args = cli.parser().parse_args(["--input-base", str(self.caller), *argv])
        forwarded, stdin, attachments = command_input.for_bridge(args, argv)
        source.unlink()
        response = self.client.post("/api/bridge", headers={"X-Autoskill-Bridge": "1"}, json={
            "argv": forwarded, "stdin": stdin, "attachments": attachments, "caller_cwd": str(self.caller), "session_id": "knowledge-bridge",
        })
        self.assertEqual(response.status_code, 200, response.text)
        reply = response.json()
        self.assertEqual(reply["exit"], 0, reply)
        return json.loads(reply["stdout"])

    def test_settings_file_snapshot_and_cas_share_cli_http_state(self):
        code, initial = self.command("knowledge", "settings", self.pid)
        self.assertEqual(code, 0)
        self.assertIsNone(initial["content_hash"])
        self.assertEqual(initial, self.client.get(f"/api/knowledge/{self.pid}/settings").json())
        current = initial
        variants = (["--from-file", self.pid], ["--from-file=" + self.pid], ["--from-file", "ignored.json", "--from-file=" + self.pid],
                    ["--from-f", self.pid], ["--from-f=" + self.pid])
        for index, file_args in enumerate(variants):
            source = self.caller / self.pid
            source.write_text(json.dumps({"expected_hash": current["content_hash"], "team_id": "团队", "business_scope": f"业务{index}"}), encoding="utf-8")
            current = self.bridge_file(["knowledge", "settings", self.pid, *file_args], source)
            self.assertEqual(current, self.client.get(f"/api/knowledge/{self.pid}/settings").json())
        stale = {"expected_hash": initial["content_hash"], "team_id": "其他团队", "business_scope": "其他业务"}
        rejected = self.client.put(f"/api/knowledge/{self.pid}/settings", json=stale)
        self.assertEqual((rejected.status_code, rejected.json()["code"]), (409, "revision_conflict"))
        code, error = self.command("knowledge", "settings", self.pid, "--from-file", "-", payload=stale)
        self.assertEqual((code, error["code"]), (2, "revision_conflict"))
        code, error = self.command("knowledge", "settings", self.pid, "--from-file", "-", payload=[])
        self.assertEqual((code, error["code"]), (2, "validation_error"))
        self.assertEqual(current, self.client.get(f"/api/knowledge/{self.pid}/settings").json())

    def test_cli_export_http_import_and_http_export_bridge_import_roundtrip(self):
        entry = self.seed()
        output = self.caller / "知识包.json"
        argv = ("knowledge", "export", self.pid, "--entry", f"{entry['id']}@1", "--output", output.name)
        code, exported = self.command(*argv)
        self.assertEqual(code, 0, exported)
        self.assertEqual((exported["path"], exported["entry_count"]), (str(output), 1))
        original = output.read_bytes()
        package = json.loads(original)
        self.assertEqual(exported["package_hash"], package["package_hash"])
        code, _error = self.command(*argv)
        self.assertEqual(code, 2)
        self.assertEqual(output.read_bytes(), original)
        body = {"package": package, "idempotency_key": "http-import"}
        response = self.client.post(f"/api/knowledge/{self.target}/import", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        receipt = response.json()
        imported = receipt["imported"][0]
        self.assertEqual(imported["evidence_status"], "imported_unverified")
        self.assertEqual(self.client.post(f"/api/knowledge/{self.target}/import", json=body).json(), receipt)
        conflicting = self.client.post(f"/api/knowledge/{self.target}/import", json={**body, "as_new": True})
        self.assertEqual((conflicting.status_code, conflicting.json()["code"]), (409, "idempotency_conflict"))
        code, fetched = self.command("knowledge", "get", self.target, imported["entry_id"], "--revision", str(imported["revision"]))
        self.assertEqual(code, 0, fetched)
        response = self.client.get(f"/api/knowledge/{self.target}/entries/{imported['entry_id']}", params={"revision": imported["revision"]})
        self.assertEqual(fetched, response.json())
        self.assertEqual(fetched["verification"], "hypothesis")
        second = self.client.post(f"/api/knowledge/{self.target}/export", json={"all": True})
        self.assertEqual(second.status_code, 200, second.text)
        destination = projects.create("二次接收", "http://127.0.0.1:11", project_id="transport-third")["id"]
        source = self.caller / "second.json"
        source.write_text(json.dumps(second.json(), ensure_ascii=False), encoding="utf-8")
        result = self.bridge_file(["knowledge", "import", destination, "--file=" + source.name, "--idempotency-key", "bridge-import"], source)
        code, duplicate = self.command("knowledge", "import", destination, "--file", "-", "--idempotency-key", "bridge-import", payload=second.json())
        self.assertEqual((code, duplicate), (0, result))

    def test_search_pages_team_scope_and_legacy_read_remain_read_only(self):
        for title in ("业务甲", "业务乙"):
            self.seed(title=title)
        self.seed(self.target, "业务丙")
        raw = projects.path(self.pid) / "knowledge" / "facts.md"
        raw.write_text("旧文件中的业务事实", encoding="utf-8")
        original_list = context_service.knowledge(self.pid)
        original_read = context_service.read_knowledge(self.pid, "facts.md")
        for project in (self.pid, self.target):
            knowledge_entries.settings(project, {"expected_hash": None, "team_id": "相同团队", "business_scope": "相同范围"})
        heads = {project: domain.read(project)["head"] for project in (self.pid, self.target)}
        code, page = self.command("knowledge", "search", self.pid, "--query", "业务", "--team", "--limit", "2")
        self.assertEqual(code, 0, page)
        response = self.client.get(f"/api/knowledge/{self.pid}/search", params={"q": "业务", "team": "true", "limit": 2})
        self.assertEqual(page, response.json())
        cursor = page["pagination"]["next_cursor"]
        self.assertTrue(cursor)
        other = self.client.get(f"/api/knowledge/{self.pid}/search", params={"q": "业务", "team": "true", "limit": 2, "cursor": cursor}).json()
        entries = [*page["entries"], *other["entries"]]
        self.assertEqual({entry["owner_project_id"] for entry in entries}, {self.pid, self.target})
        self.assertEqual(len({(entry["owner_project_id"], entry["id"]) for entry in entries}), 4)
        self.assertEqual(self.command("knowledge", "list", self.pid)[1], original_list)
        self.assertEqual(self.command("knowledge", "read", self.pid, "facts.md")[1], original_read)
        self.assertEqual({project: domain.read(project)["head"] for project in heads}, heads)
        self.assertEqual(db.rows("SELECT id FROM runs"), [])
        raw.write_text("旧文件事实已变化", encoding="utf-8")
        expired = self.client.get(f"/api/knowledge/{self.pid}/search", params={"q": "业务", "team": "true", "limit": 2, "cursor": cursor})
        self.assertEqual((expired.status_code, expired.json()["code"]), (409, "cursor_expired"))

    def test_invalid_transport_shapes_are_400_and_missing_revision_is_404(self):
        head = domain.read(self.pid)["head"]
        for path, method, payload in (
            ("settings", "put", []), ("settings", "put", {}), ("settings", "put", {"expected_hash": None, "team_id": "孤立团队", "business_scope": None}),
            ("export", "post", {"all": True, "entries": []}), ("export", "post", {"all": 1}), ("export", "post", {"entries": None}),
            ("import", "post", {"package": {}, "idempotency_key": "invalid", "as_new": "false"}), ("import", "post", []),
        ):
            with self.subTest(path=path, payload=payload):
                response = getattr(self.client, method)(f"/api/knowledge/{self.pid}/{path}", json=payload)
                self.assertEqual(response.status_code, 400, response.text)
        response = self.client.put(f"/api/knowledge/{self.pid}/settings", content="null", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400)
        missing = self.client.get(f"/api/knowledge/{self.pid}/entries/{uuid.uuid4()}", params={"revision": 1})
        self.assertEqual((missing.status_code, missing.json()["code"]), (404, "not_found"))
        project = self.client.get("/api/knowledge/missing-project/settings")
        self.assertEqual((project.status_code, project.json()["code"]), (404, "not_found"))
        for route, body in (("export", {"all": True}), ("import", {"package": {}, "idempotency_key": "missing"})):
            project = self.client.post("/api/knowledge/missing-project/" + route, json=body)
            self.assertEqual((project.status_code, project.json()["code"]), (404, "not_found"))
        project = self.client.get("/api/knowledge/INVALID/settings")
        self.assertEqual((project.status_code, project.json()["code"]), (400, "validation_error"))
        self.assertEqual(domain.read(self.pid)["head"], head)

    def test_failure_context_cli_http_preserve_real_failed_receipt(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FailedBusinessSite)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        project = projects.create("真实失败", f"http://127.0.0.1:{server.server_port}", project_id="transport-failure")["id"]
        case = builder.api_case("TC-API-001", "应当批准", "GET", "/", {"status": 200, "json_equals": {"/approved": True}})
        projects.save_cases(project, [case])
        run = runner.create(project, [case["id"]])
        self.assertEqual(runner.execute(run["id"])["status"], "Fail")
        row = db.one("SELECT * FROM results WHERE run_id = ?", (run["id"],))
        attempt = json.loads(row["detail"])["attempts"][0]["id"]
        folder = paths.local() / "runs" / run["id"]
        before = {str(file.relative_to(folder)): file.read_bytes() for file in folder.rglob("*") if file.is_file()}
        code, context = self.command("failure-context", project, "--run", run["id"], "--execution-attempt", attempt, "--case", case["id"])
        self.assertEqual(code, 0, context)
        params = {"run_id": run["id"], "execution_attempt_id": attempt, "case_id": case["id"]}
        with patch.object(runner, "execute", side_effect=AssertionError("read attempted execution")):
            response = self.client.get(f"/api/failure-context/{project}", params=params)
        self.assertEqual((response.status_code, response.json()), (200, context))
        self.assertEqual(context["outcome"]["status"], "Fail")
        self.assertTrue(any(assertion.get("passed") is False for assertion in context["assertions"]))
        after = {str(file.relative_to(folder)): file.read_bytes() for file in folder.rglob("*") if file.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(db.one("SELECT * FROM results WHERE id = ?", (row["id"],)), row)
        mismatch = self.client.get(f"/api/failure-context/{self.pid}", params=params)
        self.assertEqual((mismatch.status_code, mismatch.json()["code"]), (400, "evidence_scope_mismatch"))

    def test_workflow_submit_optional_envelope_matches_http_idempotency(self):
        entry = self.seed()
        created = workflow.create(self.pid, {"title": "知识引用", "sources": [{"id": "source-1", "text": "批准结果必须为 true"}]}, idempotency_key="workflow")
        before = db.rows("SELECT * FROM workflow_items WHERE workflow_id = ?", (created["workflow_id"],))
        diagnostic = self.client.get(f"/api/workflows/{created['workflow_id']}/diagnostic-context")
        self.assertEqual((diagnostic.status_code, diagnostic.json()["code"]), (404, "not_found"))
        self.assertEqual(db.rows("SELECT * FROM workflow_items WHERE workflow_id = ?", (created["workflow_id"],)), before)
        task = workflow.next_task(created["workflow_id"], idempotency_key="next")["task"]
        payload = {"requirements": [{"id": "r1", "text": "批准结果必须为 true", "source_ids": ["source-1"]}]}
        used = [{**{key: entry[key] for key in ("owner_project_id", "id", "revision", "content_hash")}, "purpose": "核对需求语义"}]
        code, receipt = self.command("workflow", "submit", created["workflow_id"], task["id"], "--attempt-id", task["attempt_id"],
                                     "--lease-token", task["lease_token"], "--idempotency-key", "submitted", "--file", "-",
                                     payload={"payload": payload, "knowledge_used": used})
        self.assertEqual(code, 0, receipt)
        body = {"item_id": task["id"], "attempt_id": task["attempt_id"], "lease_token": task["lease_token"], "idempotency_key": "submitted",
                "payload": payload, "knowledge_used": used}
        response = self.client.post(f"/api/workflows/{created['workflow_id']}/submit", json=body)
        self.assertEqual((response.status_code, response.json()), (200, receipt))
        conflict = self.client.post(f"/api/workflows/{created['workflow_id']}/submit", json={**body, "knowledge_used": []})
        self.assertEqual((conflict.status_code, conflict.json()["code"]), (400, "idempotency_conflict"))


if __name__ == "__main__":
    unittest.main()
