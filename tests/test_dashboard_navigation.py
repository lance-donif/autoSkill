"""Real navigation keeps saved routes, readable labels and keyboard context during updates."""
from dashboard_revision_fixture import change_project_and_wait_for_ui
from playwright.sync_api import expect
from test_load_dashboard import DashboardWorkspace

from autoskill import builder, projects, runner

MENU = ["总览", "测试用例", "执行记录", "定时任务", "数据管理"]


class NavigationDashboardTests(DashboardWorkspace):
    def test_menu_height_stays_fixed_across_destinations(self):
        measure = """() => ['#side', '#side-panel'].map(selector =>
            document.querySelector(selector).getBoundingClientRect().height)"""
        for width, height, embedded in ((656, 1000, True), (390, 550, False), (225, 700, True)):
            with self.subTest(width=width, height=height, embedded=embedded):
                self.page.set_viewport_size({"width": width, "height": height})
                self.page.goto(self.origin + ("/?embed=1" if embedded else "/") + "#/dashboard")
                expect(self.page.locator("#view h1")).to_have_text("总览")
                toggle = self.page.get_by_role("button", name="功能导航", exact=True)
                toggle.click()
                original = self.page.evaluate(measure)
                self.assertGreater(original[1], 0)
                nav = self.page.get_by_role("navigation", name="主导航")
                for page_name in MENU[1:]:
                    nav.get_by_role("link", name=page_name, exact=True).click()
                    expect(self.page.locator("#view h1")).to_have_text(page_name)
                    self.assertEqual(self.page.evaluate(measure), [original[0], 0])
                    toggle.click()
                    self.assertEqual(self.page.evaluate(measure), original)

    def test_all_destinations_remain_readable_and_keyboard_reachable_in_host_panels(self):
        for width, embedded in ((390, False), (620, False), (390, True)):
            with self.subTest(width=width, embedded=embedded):
                self.page.set_viewport_size({"width": width, "height": 500})
                self.page.goto(self.origin + ("/?embed=1" if embedded else "/") + "#/diagnostics")
                expect(self.page.locator("#view h1")).to_have_text("运行诊断")
                toggle = self.page.get_by_role("button", name="功能导航", exact=True)
                toggle.focus()
                toggle.press("Space")
                expect(toggle).to_have_attribute("aria-expanded", "true")
                nav = self.page.get_by_role("navigation", name="主导航")
                expect(nav.get_by_role("link")).to_have_count(len(MENU))
                for name in MENU:
                    self.page.keyboard.press("Tab")
                    link = nav.get_by_role("link", name=name, exact=True)
                    expect(link).to_be_visible()
                    expect(link).to_be_focused()
                    link.scroll_into_view_if_needed()
                    bounds = link.bounding_box()
                    self.assertGreaterEqual(bounds["x"], 0)
                    self.assertLessEqual(bounds["x"] + bounds["width"], width)
                    self.assertLessEqual(bounds["y"] + bounds["height"], 500)
                focused = nav.get_by_role("link", name="数据管理", exact=True).element_handle()
                change_project_and_wait_for_ui(self, self.pid)
                self.assertTrue(focused.evaluate("node => node.isConnected && node === document.activeElement"))
                self.page.keyboard.press("Escape")
                expect(toggle).to_be_focused()
                expect(toggle).to_have_attribute("aria-expanded", "false")
                toggle.press("Enter")
                self.page.get_by_role("link", name="测试用例", exact=True).click()
                expect(self.page.locator("#view h1")).to_have_text("测试用例")
                expect(toggle).to_be_focused()
                expect(toggle).to_have_attribute("aria-expanded", "false")
                expect(self.page.locator(".nav-location")).to_have_text("当前位置：测试用例")
                self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), width)

    def test_bookmarks_keep_parameters_and_only_offer_effective_scope_controls(self):
        query = f"project={self.pid}&projects={self.pid}&range=custom&from=2025-01-01&to=2025-01-05"
        for route in ("project-materials", "native-assets", "conversations", "diagnostics", "data"):
            with self.subTest(route=route):
                self.open(route + "?" + query)
                expect(self.page.locator("#scope").get_by_role("button", name="筛选项目", exact=True)).to_have_count(0)
                expect(self.page.locator("#scope").get_by_role("button", name="筛选时间范围", exact=True)).to_have_count(0)
                self.assertEqual(self.page.evaluate("location.hash"), "#/" + route + "?" + query)
                active = self.page.locator("nav a[aria-current='page']")
                if route == "data":
                    expect(active).to_have_attribute("data-page", route)
                else:
                    expect(active).to_have_count(0)
        for route in ("workflows", "schedules"):
            self.open(route + "?" + query)
            expect(self.page.get_by_role("button", name="筛选项目", exact=True)).to_be_visible()
            expect(self.page.get_by_role("button", name="筛选时间范围", exact=True)).to_have_count(0)
        self.open('case-plans?' + query)
        expect(self.page.locator('#scope')).to_be_hidden()
        options = self.page.locator('.case-plan-options > summary')
        options.focus()
        options.press('Enter')
        expect(self.page.get_by_role('combobox', name='用例表项目', exact=True)).to_have_value(self.pid)
        expect(self.page.get_by_role('button', name='回放设置', exact=True)).to_be_visible()
        self.assertEqual(self.page.evaluate('location.hash'), '#/case-plans?' + query)
        self.open("cases?range=all")
        expect(self.page.get_by_role("button", name="功能导航", exact=True)).not_to_be_visible()
        expect(self.page.get_by_role("button", name="筛选时间范围", exact=True)).to_be_visible()
        links = self.page.get_by_role("navigation", name="主导航").get_by_role("link")
        self.assertEqual(links.evaluate_all("nodes => nodes.map(node => node.getAttribute('aria-label'))"), MENU)

    def test_scope_focus_and_date_draft_survive_external_revision(self):
        self.open("cases?range=all")
        project = self.page.get_by_role("button", name="筛选项目", exact=True)
        project.focus()
        change_project_and_wait_for_ui(self, self.pid)
        expect(project).to_be_focused()
        self.page.get_by_role("button", name="筛选时间范围", exact=True).click()
        start = self.page.get_by_label("开始日期", exact=True)
        start.fill("2025-01-07")
        start.focus()
        original = start.element_handle()
        change_project_and_wait_for_ui(self, self.pid)
        expect(start).to_have_value("2025-01-07")
        self.assertTrue(original.evaluate("node => node.isConnected && node === document.activeElement"))

    def test_historical_report_keeps_summary_focus_and_collapsed_evidence_in_real_iframe(self):
        projects.save_cases(self.pid, [builder.api_case("TC-READ", "读取失败证据", "GET", "/slow",
                                                       {"status": 200, "json_equals": {"/ok": False}})])
        historical = runner.create(self.pid, ["TC-READ"], headed=False)
        self.assertEqual(runner.execute(historical["id"])["status"], "Fail")
        destination = self.origin + "/?embed=1#/runs/" + historical["id"] + "?range=all"
        for embedded in (False, True):
            with self.subTest(embedded=embedded):
                if embedded:
                    # A real local host page embeds the dashboard without leaving its old poller alive.
                    self.page.goto(f"http://127.0.0.1:{self.target.server_port}/ui")
                    self.page.set_viewport_size({"width": 700, "height": 700})
                    self.page.set_content('<iframe name="host" style="width:390px;height:500px;border:0" src="' + destination + '"></iframe>')
                    reader = self.page.frame(name="host")
                else:
                    self.page.set_viewport_size({"width": 620, "height": 800})
                    self.page.goto(destination)
                    reader = self.page
                request = reader.locator('summary').filter(has_text="请求与响应详情")
                expect(request).to_be_visible()
                request.click()
                expect(request.locator('..')).not_to_have_attribute('open', '')
                request.press("Tab")
                expect(request).not_to_be_focused()
                self.page.keyboard.press("Shift+Tab")
                expect(request).to_be_focused()
                position = reader.evaluate("scrollY")
                with self.page.expect_response(lambda response: response.url.split('?')[0] == self.origin + '/api/runs/' + historical['id']):
                    other = runner.create(self.pid, ["TC-SLOW"], headed=False)
                    self.assertEqual(runner.execute(other["id"])["status"], "Pass")
                expect(reader.locator('#view')).not_to_have_attribute('aria-busy', 'true')
                expect(request).to_be_focused()
                self.assertFalse(request.locator('..').evaluate('node => node.open'))
                self.assertEqual(reader.evaluate("scrollY"), position)
                self.evidence('historical-report-' + ('iframe390' if embedded else 'width620') + '.png')
                geometry = reader.evaluate("""() => ({width:innerWidth, document:document.documentElement.scrollWidth,
                    overflow:[...document.querySelectorAll('.result-body, .result-body *')].filter(node => {
                        const box = node.getBoundingClientRect(); return box.width && box.right > innerWidth;
                    }).map(node => ({tag:node.tagName, className:node.className, right:node.getBoundingClientRect().right}))})""")
                self.assertLessEqual(geometry['document'], geometry['width'], geometry)
                request.press("Enter")
                self.assertTrue(request.locator('..').evaluate('node => node.open'))
