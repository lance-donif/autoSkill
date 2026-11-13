"""Exercise nested folders and recursive Excel downloads against the running dashboard."""
import io
from pathlib import Path

import openpyxl
from playwright.sync_api import expect
from test_load_dashboard import DashboardWorkspace


class FeatureDashboardTests(DashboardWorkspace):
    def create_folder(self, name, parent=None):
        self.page.get_by_role("button", name="新建文件夹", exact=True).first.click()
        dialog = self.page.get_by_role("dialog", name="新建文件夹", exact=True)
        dialog.get_by_label("文件夹名称", exact=True).fill(name)
        if parent:
            dialog.get_by_label("上级文件夹", exact=True).select_option(parent)
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/folders")) as response:
            dialog.get_by_role("button", name="创建文件夹", exact=True).click()
        self.assertEqual(response.value.status, 200, response.value.text())
        expect(dialog).not_to_be_visible()
        tree = self.client.get(f"/api/projects/{self.pid}/folders").json()
        return next(folder["id"] for folder in tree["folders"] if folder["name"] == name)

    def test_nested_folders_move_and_recursive_excel_export_survive_reload(self):
        original = self.client.get(f"/api/projects/{self.pid}/cases/TC-CHECK").json()
        self.open("cases?range=all")
        parent = self.create_folder("订单测试")
        child = self.create_folder("接口回归", parent)
        self.page.get_by_role("button", name="移动用例 TC-CHECK", exact=True).click()
        dialog = self.page.get_by_role("dialog", name="移动用例", exact=True)
        dialog.get_by_label(f"「{self.pid}」目标文件夹", exact=True).select_option(child)
        dialog.get_by_role("button", name="移动用例", exact=True).click()
        expect(dialog).not_to_be_visible()
        tree = self.client.get(f"/api/projects/{self.pid}/folders").json()
        self.assertEqual(tree["assignments"]["TC-CHECK"], child)
        self.assertEqual(next(folder["parent_id"] for folder in tree["folders"] if folder["id"] == child), parent)
        self.page.reload()
        expect(self.page.get_by_role("button", name="接口回归，1 条用例", exact=True)).to_be_visible()
        self.page.get_by_role("button", name="接口回归，1 条用例", exact=True).click()
        expect(self.page.get_by_text("接口会话校验", exact=True)).to_be_visible()
        expect(self.page.get_by_text("可取消慢请求", exact=True)).not_to_be_visible()
        self.evidence("nested-folders.png")
        self.assertEqual(self.client.get(f"/api/projects/{self.pid}/cases/TC-CHECK").json(), original)
        self.page.get_by_role("button", name="订单测试，0 条用例", exact=True).click()
        with self.page.expect_download() as downloaded:
            self.page.get_by_role("link", name="导出文件夹 Excel", exact=True).click()
        workbook = openpyxl.load_workbook(io.BytesIO(Path(downloaded.value.path()).read_bytes()))
        self.addCleanup(workbook.close)
        mapped = list(workbook["编号映射"].values)
        self.assertEqual({row[1] for row in mapped[1:]}, {"TC-CHECK"})
        with self.page.expect_download() as downloaded_all:
            self.page.get_by_role("link", name="导出 Excel", exact=True).click()
        all_book = openpyxl.load_workbook(io.BytesIO(Path(downloaded_all.value.path()).read_bytes()))
        self.addCleanup(all_book.close)
        all_mapped = list(all_book["编号映射"].values)
        self.assertEqual({row[1] for row in all_mapped[1:]}, {"TC-BEGIN", "TC-CHECK", "TC-SLOW", "TC-CHANGING", "TC-UI"})
