"""Tests for format-3 sync packs, half-written deferral, conflict splitting and error-pack downloads."""
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, db, failure_archive, paths, projects, runner, share, sync_conflicts, sync_import
from autoskill.core import store
from autoskill.server.app import create_app


class _StatusHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_GET(self):
        code = 200 if self.path == "/ok" else 500
        body = json.dumps({"path": self.path, "ok": code == 200}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _write_env_config(project_id: str, environments: list[dict], *, active_environment_id: str = "", active_account_id: str = "") -> None:
    folder = projects.path(project_id)
    cfg = projects.config(project_id)
    cfg["environments"] = environments
    if active_environment_id:
        cfg["active_environment_id"] = active_environment_id
    if active_account_id:
        cfg["active_account_id"] = active_account_id
    store.write(folder / "qa-config.json", cfg)


class SyncPackAndDeferralTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dev_ws = self.root / "dev-ws"
        self.center_ws = self.root / "center-ws"
        self.sync_dir = self.root / "ftp-sync"
        self.sync_dir.mkdir()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _StatusHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def test_pack_sync_exports_only_passing_cases_with_closure_envs_and_plaintext_accounts(self):
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.dev_ws), "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_HEADED": "0"}):
            pid = projects.create("订单系统", self.base_url, project_id="order")["id"]
            prereq = builder.api_case("TC-API-001", "前置查单", "GET", "/ok", {"status": 200})
            passing = builder.api_case("TC-API-002", "退款成功", "GET", "/ok", {"status": 200}, depends_on=["TC-API-001"])
            failing = builder.api_case("TC-API-003", "失败用例", "GET", "/fail", {"status": 200})
            projects.save_cases(pid, [prereq, passing, failing])

            run_ok = runner.create(pid, ["TC-API-002"], api_only=True)
            self.assertEqual(runner.execute(run_ok["id"])["status"], "Pass")
            run_bad = runner.create(pid, ["TC-API-003"], api_only=True)
            self.assertEqual(runner.execute(run_bad["id"])["status"], "Fail")

            sync_conflicts.save_platform_entry("erp-main", {"id": "erp-main", "name": "业务平台", "login_url": f"{self.base_url}/login"})
            sync_conflicts.save_account_entry("acc-a", {
                "id": "acc-a", "platform_id": "erp-main", "environment_key": "test",
                "label": "订单管理员", "username": "order_admin", "password": "plain-dev-password", "variables": {},
            })
            sync_conflicts.save_account_entry("acc-unused", {
                "id": "acc-unused", "platform_id": "erp-main", "environment_key": "prod",
                "label": "未引用账号", "username": "unused", "password": "secret-unused", "variables": {},
            })
            _write_env_config(pid, [{
                "id": "test", "name": "测试", "base_url": self.base_url, "variables": {"tenant_id": "T01", "empty_key": ""},
                "is_default": True, "login_required": True, "auth_mode": "account_password",
                "platform_id": "erp-main", "platform_name": "业务平台", "account_ids": ["acc-a"], "default_account_id": "acc-a",
            }], active_environment_id="test", active_account_id="acc-a")

            packed = share.pack_sync([pid], out=str(self.root / "out"))
            self.assertRegex(packed["filename"], r"^autoskill-sync-order-\d{8}-\d{4}-[0-9a-f]{4}\.zip$")
            self.assertEqual(packed["case_count"], 2)
            self.assertTrue(packed["includes_environments"])
            self.assertTrue(packed["includes_platform_accounts"])

            with zipfile.ZipFile(packed["file"]) as zf:
                manifest = json.loads(zf.read(share.MANIFEST))
                self.assertEqual(manifest["format"], 3)
                self.assertEqual(manifest["pack_type"], "sync")
                self.assertEqual(manifest["status"], "pass")
                api_cases = json.loads(zf.read(f"projects/{pid}/testcases/api-cases.json"))
                self.assertEqual([item["id"] for item in api_cases], ["TC-API-001", "TC-API-002"])
                self.assertTrue(all(item["extensions"].get("internal_id", "").startswith("uid-") for item in api_cases))
                accounts = json.loads(zf.read(share.PLATFORM_ACCOUNTS))
                self.assertEqual(list(accounts["accounts"]), ["acc-a"])
                self.assertEqual(accounts["accounts"]["acc-a"]["password"], "plain-dev-password")
                self.assertNotIn("acc-unused", accounts["accounts"])

        # Multi-project naming uses "multi".
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.dev_ws), "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_HEADED": "0"}):
            pid2 = projects.create("库存系统", "http://127.0.0.1:9999", project_id="stock")["id"]
            projects.save_cases(pid2, [builder.api_case("TC-API-001", "查库存", "GET", "/stock", {"status": 200})])
            multi = share.pack_sync(["order", "stock"], out=str(self.root / "out"))
            self.assertRegex(multi["filename"], r"^autoskill-sync-multi-\d{8}-\d{4}-[0-9a-f]{4}\.zip$")

    def test_temp_suffixes_stability_window_truncated_zip_and_duplicate_hash(self):
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.dev_ws), "AUTOSKILL_BRIDGE": "off"}):
            pid = projects.create("同步包测试", self.base_url, project_id="demo")["id"]
            projects.save_cases(pid, [builder.api_case("TC-API-001", "用例一", "GET", "/ok", {"status": 200})])
            valid_zip = Path(share.pack_sync([pid], out=str(self.root / "out"))["file"])

        # Drop temp files and a truncated zip into ftp sync folder.
        (self.sync_dir / "uploading.zip.part").write_bytes(b"partial")
        (self.sync_dir / "temp.zip.tmp").write_bytes(b"partial")
        (self.sync_dir / "active.uploading").write_bytes(b"partial")
        half_zip = self.sync_dir / valid_zip.name
        half_zip.write_bytes(valid_zip.read_bytes()[:40])

        env = {
            "AUTOSKILL_WORKSPACE": str(self.center_ws),
            "AUTOSKILL_SYNC_DIR": str(self.sync_dir),
            "AUTOSKILL_SYNC_STABLE_SECONDS": "2.0",
            "AUTOSKILL_SYNC_GRACE_SECONDS": "30.0",
            "AUTOSKILL_BRIDGE": "off",
        }
        with patch.dict(os.environ, env):
            first = sync_import.scan()
            # Temp files are completely ignored; fresh half zip is pending, not failed.
            self.assertEqual([row["file"] for row in first["files"]], [valid_zip.name])
            self.assertEqual(first["files"][0]["status"], "pending")
            self.assertEqual(first["failed"], 0)
            self.assertEqual(first["pending"], 1)

            # Even after the stability window passes, truncated zip stays pending inside grace period.
            old_ts = time.time() - 5.0
            os.utime(half_zip, (old_ts, old_ts))
            pending_file = paths.local() / "sync" / "pending.json"
            pending_data = json.loads(pending_file.read_bytes())
            pending_data[valid_zip.name]["stable_since"] = old_ts
            pending_file.write_text(json.dumps(pending_data), encoding="utf-8")
            second = sync_import.scan()
            self.assertEqual(second["files"][0]["status"], "pending")
            self.assertEqual(second["failed"], 0)

            # Complete the upload and age past stability window -> imports cleanly.
            half_zip.write_bytes(valid_zip.read_bytes())
            os.utime(half_zip, (old_ts, old_ts))
            pending_data = json.loads(pending_file.read_bytes())
            pending_data[valid_zip.name]["size"] = half_zip.stat().st_size
            pending_data[valid_zip.name]["mtime_ns"] = half_zip.stat().st_mtime_ns
            pending_data[valid_zip.name]["stable_since"] = old_ts
            pending_file.write_text(json.dumps(pending_data), encoding="utf-8")
            third = sync_import.scan()
            self.assertEqual(third["imported"], 1)
            self.assertEqual(projects.case("demo", "TC-API-001")["title"], "用例一")

            # Uploading identical bytes under a new filename is skipped as duplicate.
            dup = self.sync_dir / "duplicate-copy.zip"
            dup.write_bytes(valid_zip.read_bytes())
            os.utime(dup, (old_ts, old_ts))
            fourth = sync_import.scan()
            by_file = {row["file"]: row for row in fourth["files"]}
            self.assertEqual(by_file["duplicate-copy.zip"]["status"], "skipped")
            self.assertTrue(by_file["duplicate-copy.zip"].get("duplicate"))

    def test_internal_id_collision_renumbers_display_id_and_updates_depends_on_without_deleting_old_cases(self):
        dev_a_ws, dev_b_ws = self.root / "dev-a", self.root / "dev-b"
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(dev_a_ws), "AUTOSKILL_BRIDGE": "off"}):
            projects.create("订单", self.base_url, project_id="order")
            case_a = builder.api_case("TC-API-001", "A的创建订单", "GET", "/ok", {"status": 200})
            case_a["extensions"]["internal_id"] = "uid-dev-a-001"
            case_keep = builder.api_case("TC-API-099", "中心保留旧用例", "GET", "/ok", {"status": 200})
            case_keep["extensions"]["internal_id"] = "uid-keep-099"
            projects.save_cases("order", [case_a, case_keep])
            pack_a = share.pack_sync(["order"], out=str(self.root / "out"))["file"]

        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(dev_b_ws), "AUTOSKILL_BRIDGE": "off"}):
            projects.create("订单", self.base_url, project_id="order")
            case_b1 = builder.api_case("TC-API-001", "B的优惠券前置", "GET", "/ok", {"status": 200})
            case_b1["extensions"]["internal_id"] = "uid-dev-b-001"
            case_b2 = builder.api_case("TC-API-002", "B的优惠券核销", "GET", "/ok", {"status": 200}, depends_on=["TC-API-001"])
            case_b2["extensions"]["internal_id"] = "uid-dev-b-002"
            projects.save_cases("order", [case_b1, case_b2])
            pack_b = share.pack_sync(["order"], out=str(self.root / "out"))["file"]

        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.center_ws), "AUTOSKILL_SYNC_STABLE_SECONDS": "0", "AUTOSKILL_BRIDGE": "off"}):
            res_a = sync_import.import_file(pack_a)
            self.assertEqual(res_a["status"], "imported")
            res_b = sync_import.import_file(pack_b)
            self.assertEqual(res_b["status"], "imported")
            self.assertIsNone(res_b["conflict_id"])
            remapped = res_b["renumbered"].get("TC-API-001")
            self.assertIsNotNone(remapped)
            self.assertNotEqual(remapped, "TC-API-001")
            # Original A case and omitted TC-API-099 stay intact, and B's dependent case points to remapped ID.
            all_cases = {item["id"]: item for item in projects.cases("order")}
            self.assertIn("TC-API-099", all_cases)
            self.assertEqual(all_cases["TC-API-001"]["title"], "A的创建订单")
            self.assertEqual(all_cases[remapped]["title"], "B的优惠券前置")
            self.assertEqual(all_cases["TC-API-002"]["extensions"]["depends_on"], [remapped])


class SyncConflictsAndApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dev_ws = self.root / "dev-ws"
        self.center_ws = self.root / "center-ws"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _StatusHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def _build_dev_pack(self) -> Path:
        with patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(self.dev_ws), "AUTOSKILL_BRIDGE": "off"}):
            projects.create("订单系统", "http://dev.local:8080", project_id="order")
            prereq = builder.api_case("TC-API-000", "登录前置", "GET", "/ok", {"status": 200})
            prereq["extensions"]["internal_id"] = "uid-prereq-000"
            case1 = builder.api_case("TC-API-001", "退款成功-开发新版", "GET", "/ok", {"status": 200}, depends_on=["TC-API-000"])
            case1["extensions"]["internal_id"] = "uid-case-001"
            case2 = builder.api_case("TC-API-002", "开发新增用例", "GET", "/ok", {"status": 200})
            case2["extensions"]["internal_id"] = "uid-case-002"
            projects.save_cases("order", [prereq, case1, case2])
            projects.set_auth("order", {"api_case": "TC-API-000"})
            sync_conflicts.save_platform_entry("erp-main", {"id": "erp-main", "name": "业务平台", "login_url": "http://dev.local:8080/login"})
            sync_conflicts.save_account_entry("acc-a", {
                "id": "acc-a", "platform_id": "erp-main", "environment_key": "test",
                "label": "管理员A", "username": "dev_user", "password": "dev-password", "variables": {},
            })
            sync_conflicts.save_account_entry("acc-b", {
                "id": "acc-b", "platform_id": "erp-main", "environment_key": "staging",
                "label": "管理员B", "username": "staging_user", "password": "staging-password", "variables": {},
            })
            _write_env_config("order", [
                {"id": "test", "name": "测试", "base_url": "http://dev.local:8080", "variables": {"tenant": "DEV"},
                 "is_default": True, "login_required": True, "platform_id": "erp-main", "account_ids": ["acc-a"], "default_account_id": "acc-a"},
                {"id": "staging", "name": "预发", "base_url": "http://staging.local:8080", "variables": {"tenant": "STG"},
                 "is_default": False, "login_required": True, "platform_id": "erp-main", "account_ids": ["acc-b"], "default_account_id": "acc-b"},
            ], active_environment_id="test", active_account_id="acc-a")
            return Path(share.pack_sync(["order"], out=str(self.root / "out"))["file"])

    def _seed_center(self) -> None:
        projects.create("订单系统", "http://center.test:9000", project_id="order")
        prereq = builder.api_case("TC-API-000", "登录前置", "GET", "/ok", {"status": 200})
        prereq["extensions"]["internal_id"] = "uid-prereq-000"
        case1 = builder.api_case("TC-API-001", "退款成功-中心修改版", "GET", "/ok", {"status": 200}, depends_on=["TC-API-000"])
        case1["extensions"]["internal_id"] = "uid-case-001"
        projects.save_cases("order", [prereq, case1])
        projects.set_auth("order", {"ui_case": "TC-UI-CENTER"})
        sync_conflicts.save_platform_entry("erp-main", {"id": "erp-main", "name": "业务平台", "login_url": "http://center.test:9000/login"})
        sync_conflicts.save_account_entry("acc-a", {
            "id": "acc-a", "platform_id": "erp-main", "environment_key": "test",
            "label": "管理员A", "username": "center_user", "password": "center-password", "variables": {},
        })
        _write_env_config("order", [
            {"id": "test", "name": "测试", "base_url": "http://center.test:9000", "variables": {"tenant": "CENTER"},
             "is_default": True, "login_required": True, "platform_id": "erp-main", "account_ids": ["acc-a"], "default_account_id": "acc-a"},
        ], active_environment_id="test", active_account_id="acc-a")

    def test_center_protection_split_resolution_export_both_and_http_routes(self):
        dev_pack = self._build_dev_pack()
        env = {"AUTOSKILL_WORKSPACE": str(self.center_ws), "AUTOSKILL_SYNC_STABLE_SECONDS": "0", "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_HEADED": "0"}
        with patch.dict(os.environ, env):
            self._seed_center()
            with TestClient(create_app(8787, start_worker=False), base_url="http://127.0.0.1:8787") as client:
                # Manual import via multipart/form-data upload.
                with dev_pack.open("rb") as stream:
                    imported = client.post("/api/sync/import", files={"file": (dev_pack.name, stream, "application/zip")})
                self.assertEqual(imported.status_code, 200, imported.text)
                body = imported.json()
                self.assertEqual(body["status"], "conflict")
                self.assertIn("TC-API-002", body["imported_cases"])
                conflict_id = body["conflict_id"]
                self.assertTrue(conflict_id)

                # Non-conflicting additions landed immediately; conflicting center configs were preserved.
                self.assertEqual(projects.case("order", "TC-API-001")["title"], "退款成功-中心修改版")
                self.assertEqual(projects.case("order", "TC-API-002")["title"], "开发新增用例")
                envs = {item["id"]: item for item in projects.config("order")["environments"]}
                self.assertEqual(envs["test"]["base_url"], "http://center.test:9000")
                self.assertIn("staging", envs)
                accounts = sync_conflicts.load_platform_store()["accounts"]
                self.assertEqual(accounts["acc-a"]["username"], "center_user")
                self.assertEqual(accounts["acc-a"]["password"], "center-password")
                self.assertIn("acc-b", accounts)

                # Conflict list and detail endpoints.
                status_resp = client.get("/api/sync").json()
                self.assertEqual(status_resp["conflicts_count"], 1)
                listed = client.get("/api/sync/conflicts").json()
                self.assertEqual(len(listed["items"]), 1)
                detail = client.get(f"/api/sync/conflicts/{conflict_id}").json()
                self.assertEqual(len(detail["cases"]), 1)
                self.assertEqual(len(detail["environments"]), 1)
                self.assertEqual(len(detail["accounts"]), 1)
                self.assertTrue(detail["auth"]["changed"])
                self.assertIn("title", detail["cases"][0]["diff_fields"])
                self.assertIn("base_url", detail["environments"][0]["diff_fields"])
                self.assertIn("password", detail["accounts"][0]["diff_fields"])

                # Export-both single ZIP contains current/, incoming/, conflict.json, diff.md and prerequisites.
                both_resp = client.get(f"/api/sync/conflicts/{conflict_id}/export-both")
                self.assertEqual(both_resp.status_code, 200)
                self.assertEqual(both_resp.headers["content-type"], "application/zip")
                both_zip = self.root / "both.zip"
                both_zip.write_bytes(both_resp.content)
                with zipfile.ZipFile(both_zip) as zf:
                    names = set(zf.namelist())
                    self.assertTrue({"conflict.json", "diff.md", "current/environments.json", "incoming/environments.json",
                                     "current/accounts.json", "incoming/accounts.json", "current/auth.json", "incoming/auth.json",
                                     "current/cases/TC-API-001.json", "current/cases/TC-API-000.json",
                                     "incoming/cases/TC-API-001.json", "incoming/cases/TC-API-000.json"}.issubset(names))

                # Split resolution: accept incoming cases, keep center environments/accounts/auth!
                resolved = client.post(
                    f"/api/sync/conflicts/{conflict_id}/resolve",
                    json={
                        "cases": {"mode": "use_incoming"},
                        "environments": {"mode": "keep_local"},
                        "accounts": {"mode": "keep_local"},
                        "auth": "keep_local",
                    },
                )
                self.assertEqual(resolved.status_code, 200, resolved.text)
                res_data = resolved.json()
                self.assertEqual(res_data["status"], "resolved")
                self.assertEqual(res_data["applied"]["cases"], ["TC-API-001"])
                self.assertEqual(res_data["applied"]["environments"], [])
                self.assertEqual(res_data["applied"]["accounts"], [])
                self.assertFalse(res_data["applied"]["auth"])

                # Verify applied split outcome.
                self.assertEqual(projects.case("order", "TC-API-001")["title"], "退款成功-开发新版")
                envs_after = {item["id"]: item for item in projects.config("order")["environments"]}
                self.assertEqual(envs_after["test"]["base_url"], "http://center.test:9000")
                self.assertEqual(sync_conflicts.load_platform_store()["accounts"]["acc-a"]["username"], "center_user")
                self.assertEqual(projects.auth("order"), {"ui_case": "TC-UI-CENTER"})
                self.assertEqual(client.get("/api/sync").json()["conflicts_count"], 0)

                # Export via HTTP API and download via download_url, then import into a fresh workspace via JSON file_path.
                exported = client.post("/api/sync/export", json={"project_id": "order"})
                self.assertEqual(exported.status_code, 200, exported.text)
                exp_data = exported.json()
                self.assertRegex(exp_data["filename"], r"^autoskill-sync-order-\d{8}-\d{4}-[0-9a-f]{4}\.zip$")
                dl = client.get(exp_data["download_url"])
                self.assertEqual(dl.status_code, 200)
                self.assertEqual(len(dl.content), exp_data["bytes"])
                self.assertEqual(client.get("/api/sync/conflicts/nonexistent-id").status_code, 404)
                self.assertEqual(client.post(f"/api/sync/conflicts/{conflict_id}/resolve", json={}).status_code, 400)

        fresh_ws = self.root / "fresh-ws"
        with (
            patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(fresh_ws), "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_HEADED": "0"}),
            TestClient(create_app(8787, start_worker=False), base_url="http://127.0.0.1:8787") as fresh_client,
        ):
            by_path = fresh_client.post("/api/sync/import", json={"file_path": exp_data["file"]})
            self.assertEqual(by_path.status_code, 200, by_path.text)
            self.assertEqual(by_path.json()["status"], "imported")
            self.assertEqual(projects.case("order", "TC-API-001")["title"], "退款成功-开发新版")

    def test_error_pack_route_serves_same_zip_with_prerequisites_and_strips_passwords(self):
        errors_dir = self.root / "errors"
        env = {
            "AUTOSKILL_WORKSPACE": str(self.center_ws),
            "AUTOSKILL_ERROR_DIR": str(errors_dir),
            "AUTOSKILL_BRIDGE": "off",
            "AUTOSKILL_HEADED": "0",
        }
        with patch.dict(os.environ, env):
            pid = projects.create("错误包项目", self.base_url, project_id="err-proj")["id"]
            prereq = builder.api_case("TC-API-001", "前置步骤", "GET", "/ok", {"status": 200})
            failing = builder.api_case("TC-API-002", "失败目标", "GET", "/fail", {"status": 200}, depends_on=["TC-API-001"])
            passing = builder.api_case("TC-API-003", "通过目标", "GET", "/ok", {"status": 200})
            projects.save_cases(pid, [prereq, failing, passing])

            sync_conflicts.save_account_entry("acc-secret", {
                "id": "acc-secret", "platform_id": "erp", "environment_key": "test",
                "label": "保密账号", "username": "tester", "password": "SUPER_SECRET_PASSWORD_999", "variables": {},
            })

            failed_run = runner.create(pid, ["TC-API-002"], api_only=True)
            self.assertEqual(runner.execute(failed_run["id"])["status"], "Fail")
            # Attach screenshot and account detail to verify zip packaging and password stripping.
            evidence_dir = runner.evidence_dir(failed_run["id"])
            evidence_dir.mkdir(parents=True, exist_ok=True)
            (evidence_dir / "failure.png").write_bytes(b"\x89PNG\r\n\x1a\nfake-png")
            row = db.one("SELECT id, detail FROM results WHERE run_id = ? AND case_id = ?", (failed_run["id"], "TC-API-002"))
            detail = json.loads(row["detail"])
            detail["screenshots"] = ["failure.png"]
            detail["account"] = {"account_id": "acc-secret", "label": "保密账号", "username": "tester", "password": "SUPER_SECRET_PASSWORD_999"}
            db.execute("UPDATE results SET detail = ? WHERE id = ?", (json.dumps(detail, ensure_ascii=False), row["id"]))

            passed_run = runner.create(pid, ["TC-API-003"], api_only=True)
            self.assertEqual(runner.execute(passed_run["id"])["status"], "Pass")

            # Rebuild zip so updated screenshot and account detail are included.
            cached_zip = failure_archive.build_zip(failed_run["id"], force=True)

            with TestClient(create_app(8787, start_worker=False), base_url="http://127.0.0.1:8787") as client:
                resp = client.get(f"/api/runs/{failed_run['id']}/error-pack")
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.headers["content-type"], "application/zip")
                self.assertEqual(resp.content, cached_zip.read_bytes())

                # Passing run returns 400; missing run returns 404.
                self.assertEqual(client.get(f"/api/runs/{passed_run['id']}/error-pack").status_code, 400)
                self.assertEqual(client.get("/api/runs/run-does-not-exist/error-pack").status_code, 404)

            with zipfile.ZipFile(cached_zip) as zf:
                names = set(zf.namelist())
                self.assertIn("manifest.json", names)
                self.assertIn("errors.md", names)
                self.assertIn("cases/TC-API-002.json", names)
                self.assertIn("cases/TC-API-001.json", names)
                self.assertIn("evidence/screenshots/failure.png", names)
                self.assertIn("evidence/logs/TC-API-002.json", names)
                self.assertTrue(any(name.startswith("evidence/attempts/") for name in names))
                manifest = json.loads(zf.read("manifest.json"))
                self.assertEqual(manifest["cases"][0]["prerequisite_files"], ["cases/TC-API-001.json"])
                self.assertEqual(manifest["cases"][0]["screenshots"], ["evidence/screenshots/failure.png"])
                self.assertEqual(manifest["account"]["username"], "tester")
                self.assertNotIn("password", manifest["account"])
                # Verify plaintext password never leaks in ANY member of the error pack.
                for name in names:
                    if not name.endswith(".png"):
                        content = zf.read(name).decode("utf-8", errors="replace")
                        self.assertNotIn("SUPER_SECRET_PASSWORD_999", content, f"Password leaked in {name}")
                self.assertIsNotNone(re.match(rf"^err-proj-error-\d{{8}}-\d{{4}}-{failed_run['id'][-6:]}\.zip$", cached_zip.name))


if __name__ == "__main__":
    unittest.main()
