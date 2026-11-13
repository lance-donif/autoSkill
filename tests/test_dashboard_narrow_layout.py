"""Narrow embedded dashboards keep actions reachable and wide tables scroll inside their cards."""
from playwright.sync_api import expect
from test_load_dashboard import DashboardWorkspace

from autoskill import runner


class NarrowDashboardTests(DashboardWorkspace):
    def embedded(self, route, width, theme):
        self.page.set_viewport_size({"width": width + 56, "height": 1000})
        self.page.set_content('<iframe title="autoSkill fixture" style="width:' + str(width) + 'px;height:960px;border:0" src="'
                              + self.origin + '/?embed=1#/' + route + '"></iframe>')
        frame = self.page.locator('iframe[title="autoSkill fixture"]').content_frame
        expect(frame.locator("#view h1")).to_be_visible()
        frame.locator("html").evaluate("(root, theme) => { root.dataset.theme = theme; }", theme)
        return frame

    def contained(self, frame):
        geometry = frame.locator("html").evaluate("root => ({width: innerWidth, scroll: root.scrollWidth})")
        self.assertLessEqual(geometry["scroll"], geometry["width"] + 1, geometry)

    def rect(self, frame, selector):
        return frame.locator("html").evaluate("(root, selector) => root.querySelector(selector).getBoundingClientRect().toJSON()", selector)

    def reachable(self, element, frame, selector, text=None):
        expect(element).to_be_visible()
        bounds = frame.locator("html").evaluate("""(root, query) => {
            const matches = [...root.querySelectorAll(query.selector)].filter(node => query.text === null || node.textContent.trim() === query.text);
            if (matches.length !== 1) throw new Error('Expected one current action, found ' + matches.length);
            const r = matches[0].getBoundingClientRect(); return {left:r.left,right:r.right,width:innerWidth};
        }""", {"selector": selector, "text": text})
        self.assertGreaterEqual(bounds["left"], -1, bounds)
        self.assertLessEqual(bounds["right"], bounds["width"] + 1, bounds)

    def test_project_actions_and_case_titles_remain_usable_in_a_narrow_iframe(self):
        for width in (712, 600):
            for theme in ("light", "dark"):
                with self.subTest(width=width, theme=theme):
                    frame = self.embedded("cases?range=all", width, theme)
                    self.contained(frame)
                    header = frame.locator(".project-head").first
                    for action in ("压测", "导出 Excel", "运行全部"):
                        self.reachable(header.get_by_role("button" if action != "导出 Excel" else "link", name=action, exact=True),
                                       frame, ".project-head .btn", action)
                    region = frame.get_by_role("region", name="页面压测验收用例表格", exact=True)
                    self.assertGreater(region.evaluate("element => element.scrollWidth"), region.evaluate("element => element.clientWidth"))
                    self.assertGreaterEqual(self.rect(frame, ".table-scroll tbody .title")["width"], 179)
                    folders = self.rect(frame, ".folder-tree")
                    content = self.rect(frame, ".folder-content")
                    self.assertGreaterEqual(content["y"], folders["y"] + folders["height"] - 1)
                    region.evaluate("element => { element.scrollLeft = element.scrollWidth; }")
                    button = region.get_by_role("button", name="删除用例 TC-BEGIN", exact=True)
                    self.reachable(button, frame, '#view button[aria-label="删除用例 TC-BEGIN"]')
                    opacity = frame.locator("html").evaluate("""root => getComputedStyle(
                        root.querySelector('#view button[aria-label="删除用例 TC-BEGIN"]')).opacity""")
                    self.assertEqual(opacity, "1")
                    self.contained(frame)

    def test_run_history_preserves_columns_and_scrolls_to_its_delete_action(self):
        run = runner.create(self.pid, ["TC-SLOW"], headed=False)
        self.assertEqual(runner.execute(run["id"])["status"], "Pass")
        for width in (712, 600):
            for theme in ("light", "dark"):
                with self.subTest(width=width, theme=theme):
                    frame = self.embedded("runs?range=all", width, theme)
                    self.contained(frame)
                    region = frame.get_by_role("region", name="执行记录表格", exact=True)
                    expect(region.locator("tbody tr")).to_have_count(1)
                    self.assertGreater(region.evaluate("element => element.scrollWidth"), region.evaluate("element => element.clientWidth"))
                    self.assertGreaterEqual(self.rect(frame, ".table-scroll tbody .title")["width"], 179)
                    region.evaluate("element => { element.scrollLeft = element.scrollWidth; }")
                    self.reachable(region.locator("tbody td.actions button").first, frame, ".table-scroll tbody td.actions button")
                    self.contained(frame)
