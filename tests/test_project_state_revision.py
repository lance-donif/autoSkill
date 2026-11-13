"""Project reads refresh real file changes without creating state-revision feedback."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from fastapi.testclient import TestClient

from autoskill import builder, db, projects
from autoskill.server.app import create_app


class ProjectStateRevisionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": temporary.name, "AUTOSKILL_HEADED": "0", "AUTOSKILL_BRIDGE": "off"})
        environment.start()
        self.addCleanup(environment.stop)
        self.pid = projects.create("项目状态", "http://project-state.example", project_id="project-state")["id"]
        case = builder.api_case("TC-API-001", "原始用例", "GET", "/business", {"status": 200, "json_equals": {"/accepted": True}})
        projects.save_cases(self.pid, [case])
        self.client = TestClient(create_app(start_worker=False), base_url="http://127.0.0.1:8787")
        self.addCleanup(self.client.close)

    def revision(self):
        return self.client.get("/api/state-revision").json()["revision"]

    def facts(self):
        return {"projects": db.rows("SELECT rowid,* FROM projects ORDER BY id"),
                "cases": db.rows("SELECT rowid,* FROM cases ORDER BY project_id,case_id")}

    def assert_stable_reads(self):
        before, revision = self.facts(), self.revision()
        for _ in range(4):
            time.sleep(0.003)
            response = self.client.get("/api/projects")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.facts(), before)
            self.assertEqual(self.revision(), revision)

    def test_repeated_project_gets_preserve_facts_revision_and_case_row_identity(self):
        self.assert_stable_reads()
        original = self.revision()
        projects.update(self.pid, name="真实变更")
        self.assertNotEqual(self.revision(), original)
        self.assertEqual(db.one("SELECT name FROM projects WHERE id=?", (self.pid,))["name"], "真实变更")
        self.assert_stable_reads()

    def test_external_assertion_edit_changes_public_revision_without_rewriting_index_facts(self):
        before, revision = self.facts(), self.revision()
        stored_revision = db.one("SELECT revision FROM state_revision WHERE id=1")["revision"]
        for file in (projects.path(self.pid) / "testcases").glob("*.json"):
            value = json.loads(file.read_text())
            definitions = value if isinstance(value, list) else [value]
            case = next((item for item in definitions if item.get("id") == "TC-API-001"), None)
            if case:
                case["assertions"]["json_equals"]["/accepted"] = False
                file.write_text(json.dumps(value, ensure_ascii=False))
                break
        else:
            self.fail("The fixture case must have an authoritative file to edit")
        self.assertEqual(self.client.get("/api/projects").status_code, 200)
        self.assertEqual(self.facts(), before)
        self.assertEqual(db.one("SELECT revision FROM state_revision WHERE id=1")["revision"], stored_revision)
        self.assertNotEqual(self.revision(), revision)
        self.assertFalse(projects.case(self.pid, "TC-API-001")["assertions"]["json_equals"]["/accepted"])
        self.assert_stable_reads()

    def test_reindex_keeps_archive_time_and_detects_actual_case_edits_and_removal(self):
        first = self.revision()
        case = projects.case(self.pid, "TC-API-001")
        case["title"] = "新用例标题"
        projects.save_cases(self.pid, [case])
        self.assertNotEqual(self.revision(), first)
        self.assertEqual(db.one("SELECT title FROM cases WHERE project_id=?", (self.pid,))["title"], "新用例标题")
        self.assert_stable_reads()
        case = projects.case(self.pid, "TC-API-001")
        case["lifecycle"] = "archived"
        projects.save_cases(self.pid, [case])
        archived = db.one("SELECT * FROM cases WHERE project_id=?", (self.pid,))
        self.assertEqual(archived["lifecycle"], "archived")
        self.assertIsNotNone(archived["archived_at"])
        self.assert_stable_reads()
        revision = self.revision()
        files = (projects.path(self.pid) / "testcases").glob("*.json")
        for file in files:
            value = json.loads(file.read_text())
            definitions = value if isinstance(value, list) else [value]
            if any(item.get("id") == "TC-API-001" for item in definitions):
                remaining = [item for item in definitions if item.get("id") != "TC-API-001"]
                file.write_text(json.dumps(remaining))
                break
        else:
            self.fail("The fixture case must have an authoritative file to remove")
        self.assertEqual(self.client.get("/api/projects").status_code, 200)
        self.assertNotEqual(self.revision(), revision)
        self.assertEqual(db.rows("SELECT * FROM cases WHERE project_id=?", (self.pid,)), [])
        self.assert_stable_reads()
