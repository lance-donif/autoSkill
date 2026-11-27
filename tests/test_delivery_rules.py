"""Delivery directory layout, manual-edit protection, requirement updates and ISTQB report metrics."""
import contextlib
import datetime
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))

from autoskill import builder, cli, deliverables, delivery_layout, plan, projects, runner, task_bugs, task_markdown, task_report_data


def _write_cases(folder: Path, filename: str, rows: list[dict]) -> Path:
    file = folder / filename
    file.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return file


class DeliveryRulesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.workspace = self.folder / "data"
        self.user_project = self.folder / "dev-repo"
        self.user_project.mkdir()
        self.env = patch.dict(os.environ, {
            "AUTOSKILL_WORKSPACE": str(self.workspace),
            "AUTOSKILL_HEADED": "0",
            "AUTOSKILL_BRIDGE": "off",
            "AUTOSKILL_SESSION": "delivery-rules-test",
            "AUTOSKILL_CASE_SKILL": "",
            "AUTOSKILL_DELIVERY_DIR": str(self.user_project),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def _run_cli(self, argv: list[str], expected_code: int = 0) -> dict:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(argv)
        self.assertEqual(code, expected_code, stdout.getvalue())
        return json.loads(stdout.getvalue())

    def test_single_requirement_layout_paired_minute_reports_and_same_minute_suffix(self):
        pid = projects.create("订单系统", project_id="order-sys")["id"]
        design = _write_cases(self.folder, "refund.json", [
            {"id": "TC-001", "title": "未发货订单允许退款", "module": "退款", "priority": "高",
             "business_path": ["订单系统", "退款"], "preconditions": "订单处于待发货状态",
             "steps": ["进入订单详情", "点击【申请退款】"], "expected": ["2. 提示退款申请成功。"], "source": "退款需求"},
            {"id": "TC-002", "title": "已发货订单拒绝退款", "module": "退款", "priority": "P0",
             "business_path": ["订单系统", "退款"], "preconditions": "订单已发货",
             "steps": ["进入订单详情", "点击【申请退款】"], "expected": ["2. 提示已发货不可退款。"], "source": "退款需求"},
        ])
        written = self._run_cli(["plan", "write", pid, "--file", str(design), "--requirement", "退款需求"])
        root = self.user_project / "autoSkill交付"
        self.assertEqual(written["requirement"], "退款需求")
        self.assertEqual(Path(written["delivery"]["folder"]), root.resolve())
        self.assertEqual({p.name for p in root.iterdir()}, {"功能测试用例.xlsx"})
        # Internal tracking manifests stay under .local/deliveries, never inside the user project.
        self.assertEqual({p.name for p in self.user_project.iterdir()}, {"autoSkill交付"})
        self.assertTrue(delivery_layout.manifest_path(root).is_file())
        self.assertTrue(delivery_layout.manifest_path(root).resolve().is_relative_to((self.workspace / ".local/deliveries").resolve()))

        # Deliver paired reports at the same minute twice (simulating two distinct runs in the same minute).
        t1 = datetime.datetime(2026, 11, 26, 14, 30, 12)  # noqa: DTZ001  # Local wall-clock fixture for filename formatting.
        t2 = datetime.datetime(2026, 11, 26, 14, 30, 48)  # noqa: DTZ001
        t3 = datetime.datetime(2026, 11, 26, 17, 0, 5)  # noqa: DTZ001
        report_a = written["test_report"]
        first = deliverables.report_receipt(report_a, self.user_project, requirement="退款需求", now=t1)
        self.assertFalse(first["errors"])
        # Re-delivering the exact same report_id reuses the existing minute files without creating -2.
        repeat = deliverables.report_receipt(report_a, self.user_project, requirement="退款需求", now=t2)
        self.assertTrue(all(f.get("unchanged") for f in repeat["files"]))

        # A second distinct report in the same minute appends -2 to both files so they stay paired.
        report_b = {**report_a, "report_id": report_a["report_id"] + "_run2"}
        second = deliverables.report_receipt(report_b, self.user_project, requirement="退款需求", now=t2)
        self.assertFalse(second["errors"])
        report_c = {**report_a, "report_id": report_a["report_id"] + "_run3"}
        third = deliverables.report_receipt(report_c, self.user_project, requirement="退款需求", now=t3)
        self.assertFalse(third["errors"])

        self.assertEqual({p.name for p in root.iterdir()}, {
            "功能测试用例.xlsx",
            "测试报告-20261126-1430.md", "Bug清单-20261126-1430.md",
            "测试报告-20261126-1430-2.md", "Bug清单-20261126-1430-2.md",
            "测试报告-20261126-1700.md", "Bug清单-20261126-1700.md",
        })

    def test_second_requirement_migrates_existing_workbook_and_all_reports_into_subfolders(self):
        pid = projects.create("订单系统", project_id="order-multi")["id"]
        req1 = _write_cases(self.folder, "req1.json", [
            {"id": "TC-001", "title": "未发货退款", "business_path": ["订单系统", "退款"],
             "steps": ["申请退款"], "expected": ["1. 退款成功。"], "source": "退款需求"},
        ])
        written1 = self._run_cli(["plan", "write", pid, "--file", str(req1), "--requirement", "退款需求"])
        t1 = datetime.datetime(2026, 11, 26, 14, 30)  # noqa: DTZ001
        deliverables.report_receipt(written1["test_report"], self.user_project, requirement="退款需求", now=t1)

        req2 = _write_cases(self.folder, "req2.json", [
            {"id": "TC-101", "title": "满减优惠券核销", "business_path": ["订单系统", "优惠券"],
             "steps": ["下单选择优惠券"], "expected": ["1. 订单金额扣减。"], "source": "优惠券需求"},
        ])
        written2 = self._run_cli(["plan", "write", pid, "--file", str(req2), "--requirement", "优惠券需求"])
        root = self.user_project / "autoSkill交付"
        self.assertEqual(written2["delivery"]["mode"], "multi")
        self.assertEqual(written2["delivery"]["migrated"]["requirement"], "退款需求")
        self.assertEqual({p.name for p in root.iterdir()}, {"退款需求", "优惠券需求"})
        self.assertEqual({p.name for p in (root / "退款需求").iterdir()}, {
            "功能测试用例.xlsx", "测试报告-20261126-1430.md", "Bug清单-20261126-1430.md",
        })
        self.assertEqual({p.name for p in (root / "优惠券需求").iterdir()}, {"功能测试用例.xlsx"})

    def test_windows_file_in_use_returns_actionable_code_and_rolls_back_migration(self):
        pid = projects.create("占用测试", project_id="lock-test")["id"]
        req1 = _write_cases(self.folder, "r1.json", [
            {"id": "TC-001", "title": "需求一", "steps": ["步骤一"], "expected": ["1. 结果一。"], "source": "文档一"},
        ])
        written1 = self._run_cli(["plan", "write", pid, "--file", str(req1), "--requirement", "需求一"])
        deliverables.report_receipt(written1["test_report"], self.user_project, requirement="需求一",
                                    now=datetime.datetime(2026, 11, 26, 15, 0))  # noqa: DTZ001
        root = self.user_project / "autoSkill交付"
        before_names = {p.name for p in root.iterdir()}

        # Simulate Windows sharing violation (WinError 32) on 功能测试用例.xlsx during migration.
        real_replace = Path.replace

        def locked_replace(path_self, target):
            if path_self.name == "功能测试用例.xlsx":
                err = PermissionError(13, "The process cannot access the file because it is being used by another process")
                err.winerror = 32
                raise err
            return real_replace(path_self, target)

        req2 = _write_cases(self.folder, "r2.json", [
            {"id": "TC-002", "title": "需求二", "steps": ["步骤二"], "expected": ["1. 结果二。"], "source": "文档二"},
        ])
        with patch.object(Path, "replace", locked_replace):
            blocked = self._run_cli(["plan", "write", pid, "--file", str(req2), "--requirement", "需求二"])
        self.assertEqual(blocked["delivery"]["code"], "file_in_use")
        self.assertIn("功能测试用例.xlsx", blocked["delivery"]["errors"][0]["reason"])
        self.assertIn("关闭", blocked["delivery"]["errors"][0]["reason"])
        # Migration rolled back cleanly; original files stay at root.
        self.assertEqual({p.name for p in root.iterdir()}, before_names)

        # Simulate Windows lock during direct overwrite of 功能测试用例.xlsx with updated content.
        def locked_copy(_src, dst):
            err = PermissionError(13, "locked")
            err.winerror = 33
            raise err

        r1_updated = _write_cases(self.folder, "r1-updated.json", [
            {"id": "TC-001", "title": "需求一更新", "steps": ["步骤一更新"], "expected": ["1. 结果一更新。"], "source": "文档一v2"},
        ])
        written_updated = plan.write(pid, str(r1_updated), requirement="需求一")
        with patch.object(delivery_layout, "atomic_copy", side_effect=locked_copy):
            write_err = deliverables.plan_receipt(
                projects.get(pid), written_updated, self.user_project, requirement="需求一", overwrite=True,
            )
        self.assertEqual(write_err["code"], "file_in_use")
        self.assertIn("功能测试用例.xlsx", write_err["errors"][0]["reason"])

    def test_manual_excel_edit_is_preserved_with_conversational_diff_and_merge_preview(self):
        pid = projects.create("改表保护", project_id="manual-edit")["id"]
        v1 = _write_cases(self.folder, "v1.json", [
            {"id": "TC-001", "title": "未发货允许退款", "business_path": ["订单系统", "退款"], "priority": "P0",
             "steps": ["点击【申请退款】"], "expected": ["1. 退款申请成功。"], "source": "退款需求"},
        ])
        self._run_cli(["plan", "write", pid, "--file", str(v1), "--requirement", "退款需求"])
        delivered = self.user_project / "autoSkill交付" / "功能测试用例.xlsx"

        # User manually edits the delivered workbook's remarks column.
        book = load_workbook(delivered)
        sheet = book["功能测试用例"]
        case_row = next(r for r in range(2, sheet.max_row + 1) if str(sheet.cell(r, 1).value or "").startswith("FT-"))
        sheet.cell(case_row, 13, "人工补充：财务需要同时核对退款凭证号")
        book.save(delivered)
        book.close()
        hand_edited_bytes = delivered.read_bytes()

        # Updated design arrives with a rule edit + a new case.
        v2 = _write_cases(self.folder, "v2.json", [
            {"id": "TC-001", "title": "未发货允许退款", "business_path": ["订单系统", "退款"], "priority": "P0",
             "steps": ["点击【申请退款】"], "expected": ["1. 退款申请成功并生成退款单号。"], "source": "退款需求"},
            {"id": "TC-002", "title": "已发货拒绝退款", "business_path": ["订单系统", "退款"], "priority": "P0",
             "steps": ["点击【申请退款】"], "expected": ["1. 提示已发货不可退款。"], "source": "退款需求"},
        ])
        blocked = self._run_cli(["plan", "write", pid, "--file", str(v2), "--requirement", "退款需求"])
        delivery = blocked["delivery"]
        self.assertEqual(delivery["code"], "manual_edit_conflict")
        self.assertTrue(delivery["requires_confirmation"])
        self.assertEqual(delivered.read_bytes(), hand_edited_bytes)
        self.assertIn("手工修改", delivery["diff_summary"])
        self.assertIn("财务需要同时核对退款凭证号", delivery["diff_summary"])
        self.assertIn("已发货拒绝退款", delivery["diff_summary"])
        self.assertIn("--overwrite-delivery", delivery["diff_summary"])
        self.assertIn("保留人工修改", delivery["merge_preview"])
        self.assertIn("本次新增", delivery["merge_preview"])

        # After user confirms, --overwrite-delivery updates the workbook.
        confirmed = self._run_cli(["plan", "write", pid, "--file", str(v2), "--requirement", "退款需求", "--overwrite-delivery"])
        self.assertFalse(confirmed["delivery"].get("requires_confirmation"))
        self.assertNotEqual(delivered.read_bytes(), hand_edited_bytes)

    def test_priority_normalization_and_rule_updates_mark_reverify(self):
        pid = projects.create("规则变更", project_id="rule-change")["id"]
        v1 = _write_cases(self.folder, "rule-v1.json", [
            {"id": "TC-001", "title": "核心下单", "priority": "高", "business_path": ["商城", "下单"],
             "steps": ["提交订单"], "expected": ["1. 显示下单成功。"], "source": "需求v1"},
            {"id": "TC-002", "title": "查看帮助", "priority": "低", "business_path": ["商城", "帮助"],
             "steps": ["打开帮助"], "expected": ["1. 显示帮助页。"], "source": "需求v1"},
        ])
        first = plan.write(pid, str(v1), requirement="商城下单")
        shown1 = plan.show()
        self.assertEqual({row["id"]: row["priority"] for row in shown1["cases"]}, {"TC-001": "P0", "TC-002": "P2"})
        book1 = load_workbook(first["delivery_excel"], read_only=True)
        try:
            remarks = "\n".join(str(row[12] or "") for row in book1["功能测试用例"].iter_rows(min_row=2, values_only=True))
            self.assertIn("优先级：P0（高）", remarks)
            self.assertIn("优先级：P2（低）", remarks)
        finally:
            book1.close()

        # Modify TC-001 copy/expected and add TC-003; TC-002 stays unchanged.
        v2 = _write_cases(self.folder, "rule-v2.json", [
            {"id": "TC-001", "title": "核心下单", "priority": "高", "business_path": ["商城", "下单"],
             "steps": ["提交订单"], "expected": ["1. 显示“订单已受理”。"], "source": "需求v2"},
            {"id": "TC-002", "title": "查看帮助", "priority": "低", "business_path": ["商城", "帮助"],
             "steps": ["打开帮助"], "expected": ["1. 显示帮助页。"], "source": "需求v1"},
            {"id": "TC-003", "title": "余额不足提示", "priority": "中", "business_path": ["商城", "下单"],
             "steps": ["提交超额订单"], "expected": ["1. 提示余额不足。"], "source": "需求v2"},
        ])
        updated = plan.write(pid, str(v2), requirement="商城下单")
        self.assertEqual(updated["changes"]["modified"], ["TC-001"])
        self.assertEqual(updated["changes"]["added"], ["TC-003"])
        self.assertEqual(updated["changes"]["unchanged"], ["TC-002"])
        self.assertEqual(updated["changes"]["needs_reverify"], ["TC-001", "TC-003"])
        book2 = load_workbook(updated["delivery_excel"], read_only=True)
        try:
            rows = [row for row in book2["功能测试用例"].iter_rows(min_row=2, values_only=True) if str(row[0] or "").startswith("FT-")]
            by_desc = {row[3]: row for row in rows}
            self.assertIn("需重验", by_desc["核心下单"][12])
            self.assertIn("需重验", by_desc["余额不足提示"][12])
            self.assertNotIn("需重验", by_desc["查看帮助"][12])
        finally:
            book2.close()

    def test_istqb_counts_account_without_password_and_never_claims_all_passed_on_failure_or_block(self):
        snapshot = {
            "schema": 1, "kind": "run", "task_id": "run-istqb", "project_id": "order-sys",
            "project": {"id": "order-sys", "name": "订单系统", "base_url": "http://10.0.0.8:8080"},
            "title": "退款回归", "status": "Fail", "run_ids": ["run-istqb"],
            "environment": ["环境：测试", "被测地址：http://10.0.0.8:8080"],
            "metadata": {
                "started_at": "2026-11-26T14:30:00Z", "finished_at": "2026-11-26T14:31:00Z",
                "account": {"account_id": "acc-a", "label": "订单管理员 A", "username": "admin_a", "password": "SECRET_DO_NOT_LEAK"},
            },
            "sources": {}, "notes": [], "results": [],
            "cases": [
                {"definition": {"id": "TC-1", "title": "通过用例"}, "result": {"status": "Pass", "detail": {"business_status": "Pass"}},
                 "evidence_valid": True, "run_id": "run-istqb"},
                {"definition": {"id": "TC-2", "title": "失败用例"}, "result": {"status": "Fail", "reason": "金额不符", "detail": {"business_status": "Fail"}},
                 "evidence_valid": True, "run_id": "run-istqb"},
                {"definition": {"id": "TC-3", "title": "受阻用例"}, "result": {"status": "Blocked", "reason": "前置失败", "detail": {"target_started": False}},
                 "evidence_valid": False, "run_id": "run-istqb"},
                {"definition": {"id": "TC-4", "title": "未执行用例"}, "result": None, "evidence_valid": False, "run_id": "run-istqb"},
            ],
        }
        summary = task_report_data.task_summary(snapshot)
        self.assertEqual(summary["istqb"], {
            "total": 4, "executed": 3, "passed": 1, "failed": 1, "blocked": 1, "not_run": 1,
            "all_passed": False, "conclusion": "存在失败与受阻用例",
        })
        md_text, _ = task_markdown.render(snapshot, excel_link="功能测试用例.xlsx")
        bugs_text, _ = task_bugs.render(snapshot, excel_link="功能测试用例.xlsx")
        for text in (md_text, bugs_text):
            self.assertIn("总用例 4 ｜ 本次执行 3 ｜ 通过 1 ｜ 失败 1 ｜ 受阻 1 ｜ 未执行 1", text)
            self.assertIn("订单管理员 A", text)
            self.assertNotIn("SECRET_DO_NOT_LEAK", text)
            self.assertNotIn("全部通过", text)

        # Blocked-only run must also never say 全部通过.
        blocked_only = {**snapshot, "status": "Blocked", "cases": [snapshot["cases"][0], snapshot["cases"][2]]}
        md_blocked, blocked_summary = task_markdown.render(blocked_only, excel_link="功能测试用例.xlsx")
        self.assertEqual(blocked_summary["istqb"]["blocked"], 1)
        self.assertFalse(blocked_summary["istqb"]["all_passed"])
        self.assertNotIn("全部通过", md_blocked)

    def test_cli_run_account_passthrough_and_skill_contract_clauses(self):
        pid = projects.create("账号参数测试", "http://127.0.0.1:9999", project_id="cli-account")["id"]
        projects.save_cases(pid, [builder.api_case("API-1", "健康检查", "GET", "/health", {"status": 200})])
        captured: list[dict] = []

        def fake_create(project_id, case_ids, engine, **kwargs):
            captured.append({"project_id": project_id, "case_ids": case_ids, "engine": engine, **kwargs})
            return {"id": "run-mock", "project_id": project_id, "status": "Pass", "engine": engine, "total": 1, "reason": ""}

        def fake_execute(run_id, **_kwargs):
            return {"id": run_id, "project_id": pid, "status": "Pass", "engine": "playwright", "total": 1, "done": 1,
                    "passed": 1, "failed": 0, "blocked": 0, "reason": "", "input_hash": "h",
                    "environment_id": "test", "environment_name": "测试", "account_id": "acc-a", "account_label": "管理员A"}

        with patch.object(runner, "create", side_effect=fake_create), \
                patch.object(runner, "execute", side_effect=fake_execute), \
                patch.object(plan, "sync", return_value=None):
            res_with = self._run_cli(["run", pid, "--case", "API-1", "--account", "acc-a"])
            self._run_cli(["run", pid, "--case", "API-1"])
        self.assertEqual(captured[0].get("account_id"), "acc-a")
        self.assertNotIn("account_id", captured[1])
        self.assertEqual((res_with["total"], res_with["executed"], res_with["blocked"]), (1, 0, 0))
        self.assertEqual(res_with["account_label"], "管理员A")

        skill_text = (ROOT / "skills/autoskill/SKILL.md").read_text(encoding="utf-8")
        commands_text = (ROOT / "skills/autoskill/references/commands.md").read_text(encoding="utf-8")
        for expected in ("P0", "autoSkill交付/", "绝不修改预期迎合代码报错", "询问用户是否补充回归用例",
                         "--requirement", "--overwrite-delivery", "--account", "diff_summary", "merge_preview"):
            self.assertTrue(expected in skill_text or expected in commands_text, f"Missing contract clause: {expected}")
        self.assertTrue(re.search(r"总用例.*本次执行.*通过.*失败.*受阻.*未执行", skill_text))


if __name__ == "__main__":
    unittest.main()
