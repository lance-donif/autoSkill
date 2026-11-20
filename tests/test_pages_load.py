"""Launch both load modes in Pages at two widths and check standalone JMeter reports offline."""
import re

from playwright.sync_api import expect
from test_pages_browser import WIDTHS, PagesWorkspace

PERFORMANCE = ("并发数", "持续时间（秒）", "目标请求数")
PRESSURE = ("初始并发数", "每阶段增加并发数", "最大并发数", "每阶段持续时间（秒）")
REMOVED = ("本次报告", "执行尝试", "详见本次尝试的执行记录", "查看本次记录", "范围内", "已结束", "阻塞", "未验证", "已取消", "执行与技术详情", "综合识别", "业务识别", "逐次执行结果")


class PagesLoadTests(PagesWorkspace):
    def editor(self, *, mixed=False):
        self.open("cases?range=all&projects=stock-room")
        if mixed:
            ui = self.page.get_by_role("row").filter(has=self.page.get_by_role("checkbox", name="选择 ST-UI-001", exact=True))
            expect(ui.get_by_role("button", name="运行", exact=True)).to_be_enabled()
            expect(ui.get_by_role("button", name="压测", exact=True)).to_have_count(0)
            ui.get_by_role("checkbox").check()
            expect(self.page.get_by_role("button", name="压测所选", exact=True).and_(self.page.locator(":enabled"))).to_have_count(0)
        api = self.page.get_by_role("row").filter(has=self.page.get_by_role("checkbox", name="选择 ST-API-001", exact=True))
        expect(api.get_by_role("button", name="压测", exact=True)).to_be_enabled()
        api.get_by_role("checkbox").check()
        self.page.get_by_role("button", name="压测所选", exact=True).click()
        return self.page.get_by_role("dialog", name="新建压测", exact=True)

    def check_picker(self, dialog):
        expect(dialog.get_by_role("checkbox", name=re.compile("ST-API-001"))).to_be_checked()
        expect(dialog.get_by_role("checkbox", name=re.compile("ST-UI"))).to_have_count(0)
        self.assertNotRegex(dialog.inner_text(), "Playwright|Selenium|有头模式|无头模式|浏览器引擎|迭代次数|渐增|自定义阈值")
        expect(dialog.get_by_role("button", name=re.compile("回放|运行这条用例"))).to_have_count(0)

    def fill(self, dialog, labels, values):
        expect(dialog.get_by_role("spinbutton")).to_have_count(len(labels))
        for label, value in zip(labels, values, strict=True):
            dialog.get_by_role("spinbutton", name=label, exact=True).fill(value)

    def start(self, dialog):
        dialog.get_by_role("button", name="开始压测", exact=True).click()
        expect(self.page).to_have_url(re.compile(r"/#/load-tests/[^?]+"))
        expect(self.page.get_by_role("button", name="再次压测", exact=True)).to_be_visible(timeout=20000)
        run_id = self.page.url.split("#/load-tests/")[1].split("?")[0]
        return self.page.evaluate("async id => (await fetch('/api/load-tests/' + encodeURIComponent(id))).json()", run_id)

    def stats_total(self, document):
        table = document.get_by_role("table", name="统计", exact=True)
        return table.get_by_role("row").filter(has=document.get_by_role("rowheader", name="总计", exact=True)).locator("th, td")

    def check_report(self, document):
        dashboard = document.locator(".load-dashboard")
        for label in ("统计", "APDEX", "请求汇总", "错误汇总", "每个采样器的前五项错误"):
            expect(dashboard.get_by_role("table", name=label, exact=True)).to_be_visible()
        expect(dashboard.get_by_role("columnheader", name="P95 (ms)", exact=True)).to_be_visible()
        self.assertNotRegex(document.locator(".load-report").inner_text(), "|".join(REMOVED))
        expect(document.locator(".load-metric, .load-metrics")).to_have_count(0)
        self.assertLessEqual(document.locator("html").evaluate("root => root.scrollWidth - innerWidth"), 1)

    def test_performance_limits_and_first_cutoff(self):
        for width in WIDTHS:
            for duration, target, reason, sample_count in [("1", "", "duration", 1), ("", "3", "target_requests", 3),
                                                            ("1", "999", "duration", 1), ("99", "2", "target_requests", 2)]:
                with self.subTest(width=width, duration=duration, target=target), self.session(width, f"{duration}s-{target}req"):
                    dialog = self.editor()
                    expect(dialog.get_by_role("tab", name="性能测试", exact=True)).to_have_attribute("aria-selected", "true")
                    self.check_picker(dialog)
                    self.fill(dialog, PERFORMANCE, ("1", "", ""))
                    dialog.get_by_role("button", name="开始压测", exact=True).click()
                    expect(dialog.get_by_role("alert")).to_contain_text(re.compile("持续时间|目标请求数"))
                    expect(dialog.get_by_role("checkbox", name=re.compile("ST-API-001"))).to_be_checked()
                    self.fill(dialog, PERFORMANCE, ("1", duration, target))
                    run = self.start(dialog)
                    self.assertEqual(run["load"]["mode"], "performance")
                    self.assertEqual(run["load_report"]["stop_reason"], reason)
                    self.assertEqual(run["engine"], "locust")
                    self.assertTrue(run["load_report"]["simulated"])
                    self.assertEqual(len(run["load_report"]["samples"]), sample_count)
                    expect(self.stats_total(self.page).nth(1)).to_have_text(str(sample_count))
                    expect(self.page.get_by_role("combobox", name="阶段", exact=True)).to_have_count(0)

    def test_pressure_settings_and_saved_history(self):
        for width in WIDTHS:
            with self.subTest(width=width), self.session(width):
                dialog = self.editor(mixed=True)
                self.check_picker(dialog)
                self.fill(dialog, PERFORMANCE, ("5", "17", "23"))
                pressure = dialog.get_by_role("tab", name="压力测试", exact=True)
                pressure.click()
                self.check_picker(dialog)
                self.fill(dialog, PRESSURE, ("2", "2", "6", "1"))
                dialog.get_by_role("tab", name="性能测试", exact=True).click()
                for label, value in zip(PERFORMANCE, ("5", "17", "23"), strict=True):
                    expect(dialog.get_by_role("spinbutton", name=label, exact=True)).to_have_value(value)
                pressure.click()
                for label, value in zip(PRESSURE, ("2", "2", "6", "1"), strict=True):
                    expect(dialog.get_by_role("spinbutton", name=label, exact=True)).to_have_value(value)
                expect(dialog.get_by_role("checkbox", name=re.compile("ST-API-001"))).to_be_checked()
                run = self.start(dialog)
                self.assertEqual((run["load"]["mode"], run["engine"]), ("stress", "locust"))
                self.assertEqual(run["case_ids"], ["ST-API-001"])
                self.assertEqual([sum(s["stage"] == n for s in run["load_report"]["samples"]) for n in (1, 2, 3)], [2, 4, 6])
                self.check_stages(self.page, [2, 4, 6])
                self.page.get_by_role("link", name="压测记录", exact=True).click()
                pressure_table = self.page.get_by_role("table", name="压力测试记录", exact=True)
                expect(pressure_table).to_contain_text(run["id"])
                expect(pressure_table).not_to_contain_text("load-demo-performance")
                self.page.get_by_role("tab", name="性能测试", exact=True).click()
                performance_table = self.page.get_by_role("table", name="性能测试记录", exact=True)
                expect(performance_table).to_contain_text("load-demo-performance")
                expect(performance_table).not_to_contain_text(run["id"])
                self.open("load-tests/" + run["id"])
                self.check_stages(self.page, [2, 4, 6])

    def check_stages(self, document, counts):
        selector = document.get_by_role("combobox", name="阶段", exact=True)
        expect(selector.locator("option")).to_have_count(len(counts) + 1)
        series = []
        for stage, count in enumerate(counts, start=1):
            selector.select_option(index=stage)
            expect(document.locator(".load-dashboard")).to_have_attribute("data-stage", str(stage))
            expect(self.stats_total(document).nth(1)).to_have_text(str(count))
            threads = document.locator('[data-chart="threads"]')
            expect(threads.locator('svg[role="img"]')).to_be_visible()
            expect(threads.locator("table")).to_have_count(0)
            summary = threads.locator("summary")
            summary.focus()
            summary.press("Enter")
            data = threads.get_by_role("table", name="活动线程数数据")
            expect(data).to_be_visible()
            series.append(data.locator("tbody").text_content())
            summary.press("Space")
            expect(data).to_be_hidden()
            summary.press("Enter")
            expect(data).to_be_visible()
            expect(threads.locator("table")).to_have_count(1)
        self.assertEqual(len(set(series)), len(counts), series)
        selector.select_option(index=0)
        expect(self.stats_total(document).nth(1)).to_have_text(str(sum(counts)))

    def test_frozen_charts_stage_changes_and_offline_html(self):
        for width in WIDTHS:
            for mode in ("stress", "performance"):
                run_id = "load-demo-" + mode
                with self.subTest(width=width, mode=mode), self.session(width, mode):
                    self.open("runs?range=all")
                    self.page.get_by_role("link", name="压测记录", exact=True).click()
                    tab_name = "压力测试" if mode == "stress" else "性能测试"
                    self.page.get_by_role("tab", name=tab_name, exact=True).click()
                    row = self.page.get_by_role("row").filter(has_text=run_id)
                    row.get_by_role("link", name="查看结果", exact=True).click()
                    view = self.page.locator("#view")
                    expect(view.get_by_role("button", name=re.compile("下载|导出"))).to_have_text(["下载 HTML 报告"])
                    expect(view.get_by_role("link", name=re.compile("下载|导出"))).to_have_count(0)
                    self.check_report(self.page)
                    total = 72 if mode == "stress" else 54
                    expect(self.stats_total(self.page).nth(1)).to_have_text(str(total))
                    expect(self.stats_total(self.page).nth(2)).to_have_text(str(total // 2))
                    if mode == "stress":
                        self.check_stages(self.page, [12, 24, 36])
                    chart = "threads" if mode == "stress" else "percentiles"
                    image = f'[data-chart="{chart}"] svg[role="img"]'
                    expect(self.page.locator(image)).to_be_visible()
                    charts = self.page.locator("[data-chart]").evaluate_all("nodes => nodes.map(node => node.dataset.chart)")
                    with self.offline(run_id, report_selector=".load-dashboard") as reader:
                        self.check_report(reader)
                        expect(self.stats_total(reader).nth(1)).to_have_text(str(total))
                        expect(self.stats_total(reader).nth(2)).to_have_text(str(total // 2))
                        expect(reader.locator(image)).to_be_visible()
                        self.assertEqual(reader.locator("[data-chart]").evaluate_all("nodes => nodes.map(node => node.dataset.chart)"), charts)
                        if mode == "stress":
                            self.check_stages(reader, [12, 24, 36])
                        else:
                            expect(reader.get_by_role("combobox", name="阶段", exact=True)).to_have_count(0)
